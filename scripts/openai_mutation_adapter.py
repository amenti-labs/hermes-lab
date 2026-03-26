#!/usr/bin/env python3
"""OpenAI-backed mutation adapter for Hermes Lab code experiments.

This script is designed to run as the `mutation_command` inside
`scripts/reference_executor.py`. It reads the lab env contract, gathers a
bounded set of workspace files, asks an OpenAI model for a strict JSON edit
plan, and applies the edits inside the sandbox workspace only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "gpt-5.3-codex"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_MAX_FILES = 40
DEFAULT_MAX_FILE_BYTES = 20_000
DEFAULT_MAX_TOTAL_BYTES = 120_000
DEFAULT_MAX_OUTPUT_TOKENS = 16_000
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
}
AUTO_REFERENCE_PATHS = [
    "README.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "setup.py",
]


@dataclass
class Budget:
    remaining_files: int
    remaining_bytes: int


@dataclass
class ContextFile:
    path: str
    source: str
    content: str
    truncated: bool = False


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def split_env_lines(name: str) -> list[str]:
    return [line.strip() for line in env(name).splitlines() if line.strip()]


def require_env_path(name: str) -> Path:
    value = env(name).strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return Path(value).expanduser().resolve()


def path_to_posix(path: Path) -> str:
    return path.as_posix()


def normalize_relative_path(raw: str) -> Path:
    candidate = Path(raw.strip())
    if not str(candidate):
        raise RuntimeError("Edit path cannot be empty")
    if candidate.is_absolute():
        raise RuntimeError(f"Absolute paths are not allowed: {raw}")
    normalized = Path(*[part for part in candidate.parts if part not in {"", "."}])
    if not normalized.parts:
        raise RuntimeError(f"Invalid relative path: {raw}")
    if any(part == ".." for part in normalized.parts):
        raise RuntimeError(f"Path traversal is not allowed: {raw}")
    return normalized


def resolve_workspace_path(workspace_root: Path, raw: str) -> tuple[Path, str]:
    relative = normalize_relative_path(raw)
    absolute = (workspace_root / relative).resolve()
    try:
        absolute.relative_to(workspace_root)
    except ValueError as error:
        raise RuntimeError(f"Path escapes workspace root: {raw}") from error
    return absolute, path_to_posix(relative)


def path_matches_any(relative_path: str, allowed_roots: list[str]) -> bool:
    if not allowed_roots:
        return False
    for root in allowed_roots:
        normalized_root = path_to_posix(normalize_relative_path(root))
        if relative_path == normalized_root or relative_path.startswith(f"{normalized_root}/"):
            return True
    return False


def is_probably_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def iter_target_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    results: list[Path] = []
    for child in sorted(path.rglob("*")):
        if child.is_dir():
            continue
        if any(part in SKIP_DIR_NAMES for part in child.relative_to(path).parts):
            continue
        results.append(child)
    return results


def collect_files(
    *,
    workspace_root: Path,
    declared_paths: list[str],
    source: str,
    budget: Budget,
    seen: set[str],
    allow_truncate: bool,
    max_file_bytes: int,
) -> tuple[list[ContextFile], list[str], Budget]:
    files: list[ContextFile] = []
    notes: list[str] = []
    remaining = Budget(budget.remaining_files, budget.remaining_bytes)

    for raw_path in declared_paths:
        if remaining.remaining_files <= 0 or remaining.remaining_bytes <= 0:
            notes.append(f"{source}: budget exhausted before `{raw_path}`")
            break

        absolute_path, normalized_path = resolve_workspace_path(workspace_root, raw_path)
        if not absolute_path.exists():
            notes.append(f"{source}: `{normalized_path}` missing")
            continue

        for candidate in iter_target_files(absolute_path):
            if remaining.remaining_files <= 0 or remaining.remaining_bytes <= 0:
                notes.append(f"{source}: budget exhausted while reading `{normalized_path}`")
                break

            relative_path = path_to_posix(candidate.relative_to(workspace_root))
            if relative_path in seen:
                continue

            raw_bytes = candidate.read_bytes()
            if not is_probably_text(raw_bytes):
                notes.append(f"{source}: skipped binary `{relative_path}`")
                continue

            if len(raw_bytes) > max_file_bytes and not allow_truncate:
                notes.append(
                    f"{source}: omitted `{relative_path}` because editable files must fit within {max_file_bytes} bytes"
                )
                continue

            text = raw_bytes.decode("utf-8")
            truncated = False
            if len(raw_bytes) > max_file_bytes and allow_truncate:
                text = raw_bytes[:max_file_bytes].decode("utf-8", errors="replace")
                truncated = True

            encoded_size = len(text.encode("utf-8"))
            if encoded_size > remaining.remaining_bytes:
                notes.append(f"{source}: omitted `{relative_path}` because context byte budget is exhausted")
                continue

            files.append(
                ContextFile(
                    path=relative_path,
                    source=source,
                    content=text,
                    truncated=truncated,
                )
            )
            seen.add(relative_path)
            remaining.remaining_files -= 1
            remaining.remaining_bytes -= encoded_size

    return files, notes, remaining


def render_context_block(title: str, content: str) -> str:
    stripped = content.strip()
    if not stripped:
        return f"## {title}\n\n<empty>\n"
    return f"## {title}\n\n```\n{stripped}\n```\n"


def read_optional_file(path_value: str, *, limit_bytes: int = 12_000) -> tuple[str, bool]:
    if not path_value.strip():
        return "", False
    path = Path(path_value).expanduser()
    if not path.exists():
        return "", False
    raw = path.read_bytes()
    truncated = len(raw) > limit_bytes
    body = raw[:limit_bytes].decode("utf-8", errors="replace")
    return body, truncated


def extract_spec_goal(spec_text: str) -> str:
    for line in spec_text.splitlines():
        if line.startswith("goal:"):
            return line.split(":", 1)[1].strip().strip("'").strip('"')
    return ""


def build_developer_prompt() -> str:
    return textwrap.dedent(
        """
        You are a bounded code-mutation worker inside Hermes Lab.

        You are operating in a keep-or-reject experiment loop with an external validator.
        Your job is to propose the smallest coherent code change that could improve the metric.

        Rules:
        - Output only valid JSON matching the provided schema.
        - If you do not have enough information for a safe, concrete change, return `"outcome": "noop"` with no edits.
        - Existing files may only be edited if their full contents were provided in the prompt.
        - Never edit paths outside the declared mutable paths.
        - Do not ask for more files, do not leave TODOs, and do not return partial diffs.
        - For each write edit, return the complete final file content.
        - Prefer small, targeted changes that preserve behavior outside the intended improvement.
        """
    ).strip()


def build_user_prompt(
    *,
    goal: str,
    metric_name: str,
    metric_direction: str,
    best_metric_value: str,
    acceptance_rule: str,
    mutable_paths: list[str],
    read_only_paths: list[str],
    instruction_text: str,
    spec_excerpt: str,
    summary_text: str,
    next_text: str,
    program_text: str,
    editable_files: list[ContextFile],
    reference_files: list[ContextFile],
    context_notes: list[str],
) -> str:
    lines = [
        "# Hermes Lab Mutation Task",
        "",
        "## Goal",
        goal or "<unspecified>",
        "",
        "## Metric",
        f"- Name: `{metric_name}`",
        f"- Direction: `{metric_direction}`",
        f"- Best-so-far value: `{best_metric_value or 'unknown'}`",
        "",
        "## Acceptance Rule",
        acceptance_rule or "Strict external validation decides whether the patch is kept.",
        "",
        "## Mutable Paths",
        *[f"- `{item}`" for item in mutable_paths],
        "",
        "## Read-Only Paths",
        *([f"- `{item}`" for item in read_only_paths] or ["- <none declared>"]),
        "",
    ]

    if instruction_text.strip():
        lines.extend(["## Operator Instruction", instruction_text.strip(), ""])

    if spec_excerpt.strip():
        lines.append(render_context_block("SPEC Excerpt", spec_excerpt).rstrip())
        lines.append("")
    if summary_text.strip():
        lines.append(render_context_block("Experiment Summary", summary_text).rstrip())
        lines.append("")
    if next_text.strip():
        lines.append(render_context_block("NEXT Baton", next_text).rstrip())
        lines.append("")
    if program_text.strip():
        lines.append(render_context_block("PROGRAM Excerpt", program_text).rstrip())
        lines.append("")

    lines.extend(
        [
            "## Editable File Contents",
            "Only these existing files may be modified directly because their full contents are present below.",
            "",
        ]
    )
    for item in editable_files:
        lines.extend(
            [
                f"### `{item.path}`",
                "```",
                item.content.rstrip("\n"),
                "```",
                "",
            ]
        )

    lines.extend(
        [
            "## Reference File Contents",
            "These files are read-only context. Some may be truncated.",
            "",
        ]
    )
    for item in reference_files:
        label = " (truncated)" if item.truncated else ""
        lines.extend(
            [
                f"### `{item.path}`{label}",
                "```",
                item.content.rstrip("\n"),
                "```",
                "",
            ]
        )

    if context_notes:
        lines.extend(["## Context Notes", *[f"- {note}" for note in context_notes], ""])

    lines.extend(
        [
            "## Output Expectations",
            "- Return `outcome: apply` only if you have a concrete patch.",
            "- `edits` must be a complete list of desired file writes/deletes.",
            "- If you create a new file, keep it inside a declared mutable path.",
            "- `files_considered` should list the files that informed your decision.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def mutation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["apply", "noop"],
            },
            "summary": {"type": "string"},
            "rationale": {"type": "string"},
            "files_considered": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 40,
            },
            "tests_or_followups": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
            "edits": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": ["write", "delete"],
                        },
                        "content": {"type": "string"},
                    },
                    "required": ["path", "action", "content"],
                },
            },
        },
        "required": [
            "outcome",
            "summary",
            "rationale",
            "files_considered",
            "tests_or_followups",
            "edits",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=env("OPENAI_MUTATION_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--reasoning-effort",
        default=env("OPENAI_MUTATION_REASONING_EFFORT", DEFAULT_REASONING_EFFORT),
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=int(env("OPENAI_MUTATION_MAX_FILES", str(DEFAULT_MAX_FILES))),
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=int(env("OPENAI_MUTATION_MAX_FILE_BYTES", str(DEFAULT_MAX_FILE_BYTES))),
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=int(env("OPENAI_MUTATION_MAX_TOTAL_BYTES", str(DEFAULT_MAX_TOTAL_BYTES))),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=int(env("OPENAI_MUTATION_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(env("OPENAI_MUTATION_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=float(env("OPENAI_MUTATION_POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS))),
    )
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--instruction", default="")
    parser.add_argument("--instruction-file", default="")
    parser.add_argument("--extra-path", action="append", default=[])
    parser.add_argument("--base-url", default=env("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    return parser.parse_args()


def build_headers(api_key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    organization = env("OPENAI_ORGANIZATION").strip()
    project = env("OPENAI_PROJECT").strip()
    if organization:
        headers["OpenAI-Organization"] = organization
    if project:
        headers["OpenAI-Project"] = project
    return headers


def decode_error_body(error: urllib.error.HTTPError) -> str:
    try:
        payload = error.read().decode("utf-8", errors="replace")
    except Exception:
        payload = ""
    return payload.strip()


def request_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)

    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload_text = decode_error_body(error)
            if error.code in RETRIABLE_STATUS_CODES and attempt < 3:
                retry_after = error.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2**attempt
                time.sleep(delay)
                last_error = error
                continue
            raise RuntimeError(f"OpenAI API request failed ({error.code}): {payload_text}") from error
        except urllib.error.URLError as error:
            if attempt < 3:
                time.sleep(2**attempt)
                last_error = error
                continue
            raise RuntimeError(f"OpenAI API request failed: {error}") from error
    raise RuntimeError(f"OpenAI API request failed: {last_error}")


def extract_output_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    chunks: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            if content.get("type") == "refusal":
                refusal = content.get("refusal", "")
                raise RuntimeError(f"Model refused mutation task: {refusal}")
    return "".join(chunks).strip()


def submit_response(
    *,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
    background: bool,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    response = request_json(
        method="POST",
        url=f"{base_url.rstrip('/')}/responses",
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    if not background:
        return response

    response_id = response.get("id")
    status = response.get("status")
    if not response_id:
        raise RuntimeError("Background response did not include an id")

    while status in {"queued", "in_progress"}:
        time.sleep(poll_interval_seconds)
        response = request_json(
            method="GET",
            url=f"{base_url.rstrip('/')}/responses/{response_id}",
            headers=headers,
            payload=None,
            timeout_seconds=timeout_seconds,
        )
        status = response.get("status")
    return response


def validate_plan_shape(plan: dict[str, Any]) -> None:
    if plan.get("outcome") not in {"apply", "noop"}:
        raise RuntimeError("Plan outcome must be `apply` or `noop`")
    if not isinstance(plan.get("summary"), str) or not isinstance(plan.get("rationale"), str):
        raise RuntimeError("Plan summary and rationale must be strings")
    edits = plan.get("edits")
    if not isinstance(edits, list):
        raise RuntimeError("Plan edits must be a list")
    if not isinstance(plan.get("files_considered"), list):
        raise RuntimeError("Plan files_considered must be a list")
    if not isinstance(plan.get("tests_or_followups"), list):
        raise RuntimeError("Plan tests_or_followups must be a list")
    if not all(isinstance(item, str) for item in plan["files_considered"]):
        raise RuntimeError("Plan files_considered entries must be strings")
    if not all(isinstance(item, str) for item in plan["tests_or_followups"]):
        raise RuntimeError("Plan tests_or_followups entries must be strings")
    if plan["outcome"] == "noop" and edits:
        raise RuntimeError("No-op plans must not include edits")
    if plan["outcome"] == "apply" and not edits:
        raise RuntimeError("Apply plans must include at least one edit")
    for edit in edits:
        if not isinstance(edit, dict):
            raise RuntimeError("Each edit must be an object")
        if edit.get("action") not in {"write", "delete"}:
            raise RuntimeError("Each edit action must be `write` or `delete`")
        if not isinstance(edit.get("path"), str) or not edit["path"].strip():
            raise RuntimeError("Each edit path must be a non-empty string")
        if not isinstance(edit.get("content"), str):
            raise RuntimeError("Each edit content must be a string")


def apply_plan(
    *,
    workspace_root: Path,
    plan: dict[str, Any],
    mutable_paths: list[str],
    editable_existing_files: set[str],
) -> list[str]:
    applied_files: list[str] = []

    for raw_edit in plan["edits"]:
        edit = dict(raw_edit)
        absolute_path, relative_path = resolve_workspace_path(workspace_root, str(edit.get("path", "")))
        if not path_matches_any(relative_path, mutable_paths):
            raise RuntimeError(f"Edit escapes mutable surface: {relative_path}")

        action = str(edit.get("action", "")).strip()
        content = str(edit.get("content", ""))
        file_exists = absolute_path.exists()

        if file_exists and relative_path not in editable_existing_files:
            raise RuntimeError(
                f"Existing file `{relative_path}` was not provided in full editable context and cannot be rewritten safely"
            )

        if action == "write":
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_path.write_text(content)
        elif action == "delete":
            if absolute_path.is_dir():
                raise RuntimeError(f"Directory deletes are not supported: {relative_path}")
            if absolute_path.exists():
                absolute_path.unlink()
        else:
            raise RuntimeError(f"Unsupported edit action for `{relative_path}`: {action}")
        applied_files.append(relative_path)

    return applied_files


def load_instruction_text(workspace_root: Path, args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.instruction.strip():
        parts.append(args.instruction.strip())
    if args.instruction_file.strip():
        instruction_path = Path(args.instruction_file)
        if not instruction_path.is_absolute():
            instruction_path = (workspace_root / instruction_path).resolve()
        if not instruction_path.exists():
            raise RuntimeError(f"Instruction file does not exist: {instruction_path}")
        parts.append(instruction_path.read_text().strip())
    return "\n\n".join(part for part in parts if part.strip())


def main() -> int:
    args = parse_args()

    api_key = env("OPENAI_API_KEY").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for the OpenAI mutation adapter")

    workspace_root = Path(env("LAB_EFFECTIVE_WORKSPACE_ROOT") or os.getcwd()).expanduser().resolve()
    if not workspace_root.exists():
        raise RuntimeError(f"Workspace does not exist: {workspace_root}")

    mutable_paths = split_env_lines("LAB_MUTABLE_PATHS")
    read_only_paths = split_env_lines("LAB_READ_ONLY_PATHS")
    if not mutable_paths:
        raise RuntimeError("LAB_MUTABLE_PATHS must be set for the OpenAI mutation adapter")

    artifacts_root = Path(env("LAB_RUN_ARTIFACTS_DIR") or env("LAB_RUN_DIR") or workspace_root).expanduser().resolve()
    openai_artifacts = artifacts_root / "openai"
    openai_artifacts.mkdir(parents=True, exist_ok=True)

    instruction_text = load_instruction_text(workspace_root, args)
    spec_excerpt, _ = read_optional_file(env("LAB_SPEC_PATH"))
    goal = extract_spec_goal(spec_excerpt)
    summary_text, _ = read_optional_file(env("LAB_SUMMARY_PATH"))
    next_text, _ = read_optional_file(env("LAB_NEXT_PATH"))
    program_text, _ = read_optional_file(env("LAB_PROGRAM_PATH"))

    seen_paths: set[str] = set()
    budget = Budget(
        remaining_files=args.max_files,
        remaining_bytes=args.max_total_bytes,
    )
    editable_files, editable_notes, budget = collect_files(
        workspace_root=workspace_root,
        declared_paths=mutable_paths,
        source="editable",
        budget=budget,
        seen=seen_paths,
        allow_truncate=False,
        max_file_bytes=args.max_file_bytes,
    )
    extra_reference_paths = [item for item in AUTO_REFERENCE_PATHS if item not in read_only_paths]
    extra_reference_paths.extend(args.extra_path)
    reference_files, reference_notes, budget = collect_files(
        workspace_root=workspace_root,
        declared_paths=read_only_paths + extra_reference_paths,
        source="reference",
        budget=budget,
        seen=seen_paths,
        allow_truncate=True,
        max_file_bytes=args.max_file_bytes,
    )

    if not editable_files:
        raise RuntimeError("No editable files were available within the declared mutable surface and context budget")

    prompt = build_user_prompt(
        goal=goal,
        metric_name=env("LAB_PRIMARY_METRIC", "metric"),
        metric_direction=env("LAB_METRIC_DIRECTION", "maximize"),
        best_metric_value=env("LAB_BEST_METRIC_VALUE", ""),
        acceptance_rule=env("LAB_ACCEPTANCE_RULE", ""),
        mutable_paths=mutable_paths,
        read_only_paths=read_only_paths,
        instruction_text=instruction_text,
        spec_excerpt=spec_excerpt,
        summary_text=summary_text,
        next_text=next_text,
        program_text=program_text,
        editable_files=editable_files,
        reference_files=reference_files,
        context_notes=editable_notes + reference_notes,
    )

    request_payload = {
        "model": args.model,
        "input": [
            {"role": "system", "content": build_developer_prompt()},
            {"role": "user", "content": prompt},
        ],
        "reasoning": {"effort": args.reasoning_effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "hermes_lab_mutation_plan",
                "schema": mutation_schema(),
                "strict": True,
            }
        },
        "max_output_tokens": args.max_output_tokens,
        "store": False,
        "background": args.background,
    }

    (openai_artifacts / "prompt.md").write_text(prompt)
    (openai_artifacts / "request.json").write_text(json.dumps(request_payload, indent=2) + "\n")

    response = submit_response(
        base_url=args.base_url,
        headers=build_headers(api_key),
        payload=request_payload,
        timeout_seconds=args.timeout_seconds,
        background=args.background,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    (openai_artifacts / "response.json").write_text(json.dumps(response, indent=2) + "\n")

    status = response.get("status")
    if status not in {None, "completed"}:
        raise RuntimeError(f"OpenAI response did not complete successfully: {status}")

    response_text = extract_output_text(response)
    if not response_text:
        raise RuntimeError("OpenAI response did not include output_text")

    plan = json.loads(response_text)
    validate_plan_shape(plan)
    (openai_artifacts / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    editable_existing_files = {item.path for item in editable_files}
    applied_files: list[str] = []
    if plan["outcome"] == "apply":
        applied_files = apply_plan(
            workspace_root=workspace_root,
            plan=plan,
            mutable_paths=mutable_paths,
            editable_existing_files=editable_existing_files,
        )

    (openai_artifacts / "applied-files.txt").write_text(
        "\n".join(applied_files) + ("\n" if applied_files else "")
    )

    summary = {
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "outcome": plan["outcome"],
        "summary": plan.get("summary", ""),
        "rationale": plan.get("rationale", ""),
        "applied_files": applied_files,
        "files_considered": plan.get("files_considered", []),
        "tests_or_followups": plan.get("tests_or_followups", []),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise
