#!/usr/bin/env python3
"""Reference executor for generalized autoresearch experiments.

This script bridges the Hermes Lab scheduler contract to concrete workspace runs.
It can:

- run setup, baseline, mutation, and validation commands
- use a sandboxed git clone or plain copied workspace
- compare the candidate metric against the previous best or baseline
- save patches and command logs into the run bundle
- optionally apply the winning patch back to the original git workspace
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CommandResult:
    command: str
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name, str(default).lower()).strip().lower()
    return value in {"1", "true", "yes", "on"}


def env_float(name: str) -> float | None:
    raw = env(name).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def write_text(path: Path, text: str) -> None:
    path.write_text(text)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def require_env_path(name: str) -> Path:
    value = env(name).strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return Path(value).expanduser().resolve()


def resolve_command_tokens(command: str, *, base_dir: Path | None) -> list[str]:
    tokens = shlex.split(command)
    if not tokens or base_dir is None:
        return tokens
    indices = [0]
    if tokens[0] in {"python", "python3", "bash", "sh", "zsh"} and len(tokens) > 1:
        indices.append(1)
    for index in indices:
        candidate = Path(tokens[index])
        if candidate.is_absolute():
            continue
        resolved = (base_dir / candidate).resolve()
        if resolved.exists():
            tokens[index] = str(resolved)
    return tokens


def run_command(
    command: str,
    *,
    cwd: Path,
    command_env: dict[str, str],
    log_dir: Path,
    label: str,
    repo_root: Path | None,
) -> CommandResult:
    completed = subprocess.run(
        resolve_command_tokens(command, base_dir=repo_root),
        cwd=cwd,
        env=command_env,
        capture_output=True,
        text=True,
        check=False,
    )
    write_text(log_dir / f"{label}.stdout.log", completed.stdout)
    write_text(log_dir / f"{label}.stderr.log", completed.stderr)
    return CommandResult(
        command=command,
        cwd=cwd,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def parse_metric_output(output: str, metric_name: str) -> dict[str, Any]:
    stripped = output.strip()
    if not stripped:
        raise RuntimeError("Metric command produced no stdout")

    parsed = None
    for candidate in (stripped, stripped.splitlines()[-1].strip()):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue

    if isinstance(parsed, dict):
        if "value" not in parsed:
            raise RuntimeError("Metric JSON must include a `value` field")
        metric = dict(parsed)
        metric.setdefault("metric", metric_name)
        return metric

    if isinstance(parsed, (int, float)):
        return {"metric": metric_name, "value": parsed}

    last_line = ""
    for line in stripped.splitlines():
        candidate = line.strip()
        if candidate:
            last_line = candidate
    try:
        value = float(last_line)
    except ValueError as error:
        raise RuntimeError(
            "Metric output must be JSON or end with a numeric value"
        ) from error
    return {"metric": metric_name, "value": value}


def git_available() -> bool:
    return shutil.which("git") is not None


def is_git_repo(path: Path) -> bool:
    if not git_available():
        return False
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def ensure_clean_git_workspace(path: Path) -> None:
    completed = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Could not inspect git workspace: {path}")
    if completed.stdout.strip():
        raise RuntimeError(
            "Workspace has uncommitted changes. "
            "Use a clean repo for git-backed promotion."
        )


def clone_git_workspace(src: Path, dst: Path) -> None:
    completed = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(src), str(dst)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git clone failed: {completed.stderr.strip()}")


def copy_workspace(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst)


def git_patch(src_repo: Path, patch_path: Path, diffstat_path: Path, files_path: Path) -> list[str]:
    diff = subprocess.run(
        ["git", "-C", str(src_repo), "diff", "--binary", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode != 0:
        raise RuntimeError(f"git diff failed: {diff.stderr.strip()}")
    write_text(patch_path, diff.stdout)

    diffstat = subprocess.run(
        ["git", "-C", str(src_repo), "diff", "--stat", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if diffstat.returncode != 0:
        raise RuntimeError(f"git diff --stat failed: {diffstat.stderr.strip()}")
    write_text(diffstat_path, diffstat.stdout)

    name_only = subprocess.run(
        ["git", "-C", str(src_repo), "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if name_only.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {name_only.stderr.strip()}")
    changed_files = [line.strip() for line in name_only.stdout.splitlines() if line.strip()]
    write_text(files_path, "\n".join(changed_files) + ("\n" if changed_files else ""))
    return changed_files


def apply_git_patch(workspace_root: Path, patch_path: Path) -> None:
    completed = subprocess.run(
        ["git", "-C", str(workspace_root), "apply", "--reject", "--whitespace=nowarn", str(patch_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git apply failed: {completed.stderr.strip()}")


def compare(candidate: float, reference: float, direction: str) -> bool:
    if direction == "minimize":
        return candidate < reference
    return candidate > reference


def build_result_md(
    *,
    experiment_id: str,
    iteration: str,
    baseline_value: float | None,
    previous_best: float | None,
    candidate_value: float | None,
    accepted: bool,
    promotion_strategy: str,
    workspace_mode: str,
    applied: bool,
    changed_files: list[str],
    mutation_command: str,
    validation_command: str,
    reference_source: str,
) -> str:
    result_lines = [
        f"# {experiment_id} - Iteration {iteration}",
        "",
        "## Hypothesis",
        "A bounded mutation can improve the primary metric without escaping the experiment box.",
        "",
        "## Method",
        f"Mutation command: `{mutation_command}`",
        f"Validation command: `{validation_command}`",
        f"Workspace mode: `{workspace_mode}`",
        f"Promotion strategy: `{promotion_strategy}`",
        "",
        "## Result",
        f"Candidate metric: {candidate_value}",
        f"Baseline metric: {baseline_value}",
        f"Previous best metric: {previous_best}",
        f"Reference used for comparison: {reference_source}",
        f"Accepted: {accepted}",
        f"Applied to original workspace: {applied}",
    ]
    if changed_files:
        result_lines.extend(["Changed files:", *[f"- `{item}`" for item in changed_files]])
    result_lines.extend(
        [
            "",
            "## Interpretation",
            (
                "The candidate improved enough to be kept."
                if accepted
                else "The candidate did not improve enough to be promoted."
            ),
            "",
            "## Next",
            (
                "Deepen the current direction or tighten the mutation target while preserving the gain."
                if accepted
                else "Change the mutation strategy, narrow the editable surface, or improve the validator."
            ),
            "",
        ]
    )
    return "\n".join(result_lines)


def main() -> int:
    run_dir = require_env_path("LAB_RUN_DIR")
    artifacts_dir = run_dir / "artifacts"
    commands_dir = artifacts_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    experiment_id = env("LAB_EXPERIMENT_ID")
    iteration = env("LAB_RUN_ITERATION", "0")
    metric_name = env("LAB_PRIMARY_METRIC", "metric")
    metric_direction = env("LAB_METRIC_DIRECTION", "maximize").strip().lower() or "maximize"
    workspace_root = require_env_path("LAB_WORKSPACE_ROOT")
    setup_command = env("LAB_SETUP_COMMAND").strip()
    baseline_command = env("LAB_BASELINE_COMMAND").strip()
    mutation_command = env("LAB_MUTATION_COMMAND").strip()
    validation_command = env("LAB_VALIDATION_COMMAND").strip()
    promotion_strategy = env("LAB_PROMOTION_STRATEGY", "patch-only").strip() or "patch-only"
    workspace_mode = env("LAB_WORKSPACE_MODE", "auto").strip() or "auto"
    require_clean_workspace = env_bool("LAB_REQUIRE_CLEAN_WORKSPACE", default=False)
    previous_best = env_float("LAB_BEST_METRIC_VALUE")
    repo_root = Path(env("LAB_REPO_ROOT")).expanduser().resolve() if env("LAB_REPO_ROOT").strip() else None

    if not mutation_command:
        raise RuntimeError("LAB_MUTATION_COMMAND is required for the reference executor")
    if not validation_command:
        raise RuntimeError("LAB_VALIDATION_COMMAND is required for the reference executor")

    repo_mode = is_git_repo(workspace_root)
    if workspace_mode == "auto":
        workspace_mode = "git-clone" if repo_mode else "copy"

    if workspace_mode.startswith("git") and not repo_mode:
        raise RuntimeError("Git workspace mode requested, but workspace_root is not a git repository")

    if repo_mode and (require_clean_workspace or workspace_mode.startswith("git") or "apply" in promotion_strategy):
        ensure_clean_git_workspace(workspace_root)

    sandbox_root = artifacts_dir / "workspace"
    if workspace_mode.startswith("git"):
        clone_git_workspace(workspace_root, sandbox_root)
    else:
        copy_workspace(workspace_root, sandbox_root)

    command_env = os.environ.copy()
    command_env.update(
        {
            "LAB_EFFECTIVE_WORKSPACE_ROOT": str(sandbox_root),
            "LAB_ORIGINAL_WORKSPACE_ROOT": str(workspace_root),
            "LAB_RUN_ARTIFACTS_DIR": str(artifacts_dir),
        }
    )

    if setup_command:
        setup_result = run_command(
            setup_command,
            cwd=sandbox_root,
            command_env=command_env,
            log_dir=commands_dir,
            label="setup",
            repo_root=repo_root,
        )
        if setup_result.returncode != 0:
            raise RuntimeError(f"Setup command failed: {setup_result.stderr.strip()}")

    baseline_value: float | None = None
    if baseline_command:
        baseline_result = run_command(
            baseline_command,
            cwd=sandbox_root,
            command_env=command_env,
            log_dir=commands_dir,
            label="baseline",
            repo_root=repo_root,
        )
        if baseline_result.returncode != 0:
            raise RuntimeError(f"Baseline command failed: {baseline_result.stderr.strip()}")
        baseline_metric = parse_metric_output(baseline_result.stdout, metric_name)
        baseline_value = float(baseline_metric["value"])

    mutation_result = run_command(
        mutation_command,
        cwd=sandbox_root,
        command_env=command_env,
        log_dir=commands_dir,
        label="mutation",
        repo_root=repo_root,
    )
    if mutation_result.returncode != 0:
        raise RuntimeError(f"Mutation command failed: {mutation_result.stderr.strip()}")

    validation_result = run_command(
        validation_command,
        cwd=sandbox_root,
        command_env=command_env,
        log_dir=commands_dir,
        label="validation",
        repo_root=repo_root,
    )
    if validation_result.returncode != 0:
        raise RuntimeError(f"Validation command failed: {validation_result.stderr.strip()}")

    candidate_metric = parse_metric_output(validation_result.stdout, metric_name)
    candidate_value = float(candidate_metric["value"])

    reference_source = "none"
    reference_value: float | None = None
    if previous_best is not None:
        reference_source = "best-so-far"
        reference_value = previous_best
    elif baseline_value is not None:
        reference_source = "baseline"
        reference_value = baseline_value

    if reference_value is None:
        accepted = True
    else:
        accepted = compare(candidate_value, reference_value, metric_direction)

    changed_files: list[str] = []
    applied_to_original = False
    patch_path = artifacts_dir / "diff.patch"
    diffstat_path = artifacts_dir / "diffstat.txt"
    changed_files_path = artifacts_dir / "changed-files.txt"

    if repo_mode and workspace_mode.startswith("git"):
        changed_files = git_patch(sandbox_root, patch_path, diffstat_path, changed_files_path)
        if accepted and promotion_strategy in {"apply-on-accept", "git-apply-on-accept"} and patch_path.read_text():
            ensure_clean_git_workspace(workspace_root)
            apply_git_patch(workspace_root, patch_path)
            applied_to_original = True

    decision = {
        "accepted": accepted,
        "promotion_strategy": promotion_strategy,
        "workspace_mode": workspace_mode,
        "reference_source": reference_source,
        "baseline_value": baseline_value,
        "previous_best_value": previous_best,
        "candidate_value": candidate_value,
        "applied_to_original": applied_to_original,
        "changed_files": changed_files,
    }
    write_json(artifacts_dir / "decision.json", decision)

    result_md = build_result_md(
        experiment_id=experiment_id,
        iteration=iteration,
        baseline_value=baseline_value,
        previous_best=previous_best,
        candidate_value=candidate_value,
        accepted=accepted,
        promotion_strategy=promotion_strategy,
        workspace_mode=workspace_mode,
        applied=applied_to_original,
        changed_files=changed_files,
        mutation_command=mutation_command,
        validation_command=validation_command,
        reference_source=reference_source,
    )
    metrics = {
        "iteration": int(iteration),
        "metric": candidate_metric.get("metric", metric_name),
        "value": candidate_value,
        "accepted": accepted,
        "baseline_value": baseline_value,
        "previous_best_value": previous_best,
        "reference_source": reference_source,
        "promotion_strategy": promotion_strategy,
        "workspace_mode": workspace_mode,
        "applied_to_original": applied_to_original,
        "changed_file_count": len(changed_files),
        "source": "reference-executor",
    }

    write_text(run_dir / "RESULT.md", result_md)
    write_json(run_dir / "metrics.json", metrics)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise
