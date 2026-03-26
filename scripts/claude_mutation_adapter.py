#!/usr/bin/env python3
"""Anthropic Claude-backed mutation adapter for Hermes Lab code experiments.

This script is designed to run as the `mutation_command` inside
`scripts/reference_executor.py`. It reads the lab env contract, gathers a
bounded set of workspace files, asks Claude for a strict JSON edit plan via the
Messages API, and applies the edits inside the sandbox workspace only.
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


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_FILES = 40
DEFAULT_MAX_FILE_BYTES = 20_000
DEFAULT_MAX_TOTAL_BYTES = 120_000
DEFAULT_MAX_TOKENS = 8_000
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_API_VERSION = "2023-06-01"
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
        return f"<{title}><empty></{title}>"
    return f"<{title}>\n{stripped}\n</{title}>"


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


def build_system_prompt() -> str:
    return textwrap.dedent(
        """
        You are a bounded code-mutation worker inside Hermes Lab.

        You are operating in a keep-or-reject experiment loop with an external validator.
        Your job is to propose the smallest coherent code change that could improve the metric.

        Requirements:
        - Return only valid JSON matching the provided schema.
        - If there is not enough information for a safe concrete change, return "outcome": "noop" with no edits.
        - Existing files may only be edited if their full contents are provided in the prompt.
        - Never edit paths outside the declared mutable paths.
        - Avoid over-engineering, net-new scratch files, and test-only hacks.
        - Do not optimize merely to pass tests through hard-coded shortcuts.
        - For each write edit, return the complete final file content.
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
    parts = [
        "<task>",
        render_context_block("goal", goal or "<unspecified>"),
        render_context_block(
            "metric",
            "\n".join(
                [
                    f"name: {metric_name}",
                    f"direction: {metric_direction}",
                    f"best_so_far: {best_metric_value or 'unknown'}",
                ]
            ),
        ),
        render_context_block(
            "acceptance_rule",
            acceptance_rule or "Strict external validation decides whether the patch is kept.",
        ),
        render_context_block("mutable_paths", "\n".join(mutable_paths)),
        render_context_block("read_only_paths", "\n".join(read_only_paths) or "<none declared>"),
    ]

    if instruction_text.strip():
        parts.append(render_context_block("operator_instruction", instruction_text.strip()))
    if spec_excerpt.strip():
        parts.append(render_context_block("spec_excerpt", spec_excerpt))
    if summary_text.strip():
        parts.append(render_context_block("experiment_summary", summary_text))
    if next_text.strip():
        parts.append(render_context_block("next_baton", next_text))
    if program_text.strip():
        parts.append(render_context_block("program_excerpt", program_text))

    editable_blocks = []
    for item in editable_files:
        editable_blocks.append(
            "\n".join(
                [
                    f'<file path="{item.path}" source="{item.source}">',
                    item.content.rstrip("\n"),
                    "</file>",
                ]
            )
        )
    parts.append(render_context_block("editable_files", "\n\n".join(editable_blocks)))

    reference_blocks = []
    for item in reference_files:
        truncated = ' truncated="true"' if item.truncated else ""
        reference_blocks.append(
            "\n".join(
                [
                    f'<file path="{item.path}" source="{item.source}"{truncated}>',
                    item.content.rstrip("\n"),
                    "</file>",
                ]
            )
        )
    if reference_blocks:
        parts.append(render_context_block("reference_files", "\n\n".join(reference_blocks)))

    if context_notes:
        parts.append(render_context_block("context_notes", "\n".join(f"- {note}" for note in context_notes)))

    parts.extend(
        [
            render_context_block(
                "output_expectations",
                "\n".join(
                    [
                        "- Return outcome=apply only if you have a concrete patch.",
                        "- edits must be the complete list of desired writes/deletes.",
                        "- New files are allowed only inside mutable paths.",
                        "- files_considered should list the inputs that informed the decision.",
                    ]
                ),
            ),
            "</task>",
        ]
    )
    return "\n\n".join(parts).strip() + "\n"


def mutation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "outcome": {"type": "string", "enum": ["apply", "noop"]},
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
                        "action": {"type": "string", "enum": ["write", "delete"]},
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
    parser.add_argument("--model", default=env("CLAUDE_MUTATION_MODEL", DEFAULT_MODEL))
    parser.add_argument("--effort", default=env("CLAUDE_MUTATION_EFFORT", DEFAULT_EFFORT))
    parser.add_argument(
        "--max-files",
        type=int,
        default=int(env("CLAUDE_MUTATION_MAX_FILES", str(DEFAULT_MAX_FILES))),
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=int(env("CLAUDE_MUTATION_MAX_FILE_BYTES", str(DEFAULT_MAX_FILE_BYTES))),
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=int(env("CLAUDE_MUTATION_MAX_TOTAL_BYTES", str(DEFAULT_MAX_TOTAL_BYTES))),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(env("CLAUDE_MUTATION_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(env("CLAUDE_MUTATION_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
    )
    parser.add_argument("--instruction", default="")
    parser.add_argument("--instruction-file", default="")
    parser.add_argument("--extra-path", action="append", default=[])
    parser.add_argument("--base-url", default=env("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    parser.add_argument(
        "--anthropic-version",
        default=env("ANTHROPIC_VERSION", DEFAULT_API_VERSION),
    )
    return parser.parse_args()


def build_headers(api_key: str, anthropic_version: str) -> dict[str, str]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": anthropic_version,
        "content-type": "application/json",
    }
    beta = env("ANTHROPIC_BETA").strip()
    if beta:
        headers["anthropic-beta"] = beta
    return headers


def decode_error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def request_json(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
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
            raise RuntimeError(f"Anthropic API request failed ({error.code}): {payload_text}") from error
        except urllib.error.URLError as error:
            if attempt < 3:
                time.sleep(2**attempt)
                last_error = error
                continue
            raise RuntimeError(f"Anthropic API request failed: {error}") from error
    raise RuntimeError(f"Anthropic API request failed: {last_error}")


def extract_output_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for content in response.get("content", []):
        if content.get("type") == "text":
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks).strip()


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

    api_key = env("ANTHROPIC_API_KEY").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required for the Claude mutation adapter")

    workspace_root = Path(env("LAB_EFFECTIVE_WORKSPACE_ROOT") or os.getcwd()).expanduser().resolve()
    if not workspace_root.exists():
        raise RuntimeError(f"Workspace does not exist: {workspace_root}")

    mutable_paths = split_env_lines("LAB_MUTABLE_PATHS")
    read_only_paths = split_env_lines("LAB_READ_ONLY_PATHS")
    if not mutable_paths:
        raise RuntimeError("LAB_MUTABLE_PATHS must be set for the Claude mutation adapter")

    artifacts_root = Path(env("LAB_RUN_ARTIFACTS_DIR") or env("LAB_RUN_DIR") or workspace_root).expanduser().resolve()
    claude_artifacts = artifacts_root / "claude"
    claude_artifacts.mkdir(parents=True, exist_ok=True)

    instruction_text = load_instruction_text(workspace_root, args)
    spec_excerpt, _ = read_optional_file(env("LAB_SPEC_PATH"))
    goal = extract_spec_goal(spec_excerpt)
    summary_text, _ = read_optional_file(env("LAB_SUMMARY_PATH"))
    next_text, _ = read_optional_file(env("LAB_NEXT_PATH"))
    program_text, _ = read_optional_file(env("LAB_PROGRAM_PATH"))

    seen_paths: set[str] = set()
    budget = Budget(remaining_files=args.max_files, remaining_bytes=args.max_total_bytes)
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
        "max_tokens": args.max_tokens,
        "system": build_system_prompt(),
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {
            "effort": args.effort,
            "format": {
                "type": "json_schema",
                "schema": mutation_schema(),
            },
        },
    }

    (claude_artifacts / "prompt.md").write_text(prompt)
    (claude_artifacts / "request.json").write_text(json.dumps(request_payload, indent=2) + "\n")

    response = request_json(
        url=f"{args.base_url.rstrip('/')}/v1/messages",
        headers=build_headers(api_key, args.anthropic_version),
        payload=request_payload,
        timeout_seconds=args.timeout_seconds,
    )
    (claude_artifacts / "response.json").write_text(json.dumps(response, indent=2) + "\n")

    stop_reason = response.get("stop_reason")
    if stop_reason == "max_tokens":
        raise RuntimeError("Claude response hit max_tokens before completing the JSON plan")

    response_text = extract_output_text(response)
    if not response_text:
        raise RuntimeError("Claude response did not include text output")

    plan = json.loads(response_text)
    validate_plan_shape(plan)
    (claude_artifacts / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    editable_existing_files = {item.path for item in editable_files}
    applied_files: list[str] = []
    if plan["outcome"] == "apply":
        applied_files = apply_plan(
            workspace_root=workspace_root,
            plan=plan,
            mutable_paths=mutable_paths,
            editable_existing_files=editable_existing_files,
        )

    (claude_artifacts / "applied-files.txt").write_text(
        "\n".join(applied_files) + ("\n" if applied_files else "")
    )

    summary = {
        "model": args.model,
        "effort": args.effort,
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
