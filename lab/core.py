"""Hermes Lab core.

Stable experiment folders, immutable run bundles, derived projections, and a
small append-only event ledger. The repo provides the code and templates. The
data root holds canonical lab state.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_DATA_ROOT = Path("./lab-data")
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = REPO_ROOT / "templates"

DEFAULT_ROLE_ORDER = ["scout", "researcher", "critic", "synthesizer"]
DEFAULT_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
TIER_OVERRIDABLE_FIELDS = [
    "goal",
    "metric",
    "metric_direction",
    "priority",
    "cadence",
    "time_budget_minutes",
    "workspace_root",
    "setup_command",
    "baseline_command",
    "executor_command",
    "mutation_command",
    "agent_provider",
    "agent_model",
    "agent_effort",
    "agent_instruction_file",
    "agent_base_url",
    "agent_background",
    "validation_command",
    "acceptance_rule",
    "promotion_strategy",
    "workspace_mode",
    "require_clean_workspace",
    "mutable_paths",
    "read_only_paths",
    "ingress_files",
    "egress_files",
    "artifacts_expected",
    "worker_roles",
    "constraints",
    "executor_class",
]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def now_ts() -> str:
    return now_utc().strftime("%Y%m%d-%H%M%S-%f")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def load_text(path: Path, fallback: str = "") -> str:
    if not path.exists():
        return fallback
    return path.read_text()


def save_text(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def load_json(path: Path, fallback: Any = None) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text())


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=False) + "\n")


def render_template(name: str) -> str:
    return (TEMPLATE_ROOT / name).read_text()


def coerce_scalar(raw: str) -> Any:
    value = raw.strip()
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the repo's simple SPEC files without external dependencies."""
    result: dict[str, Any] = {}
    current_list_key: str | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("  - ") and current_list_key:
            result.setdefault(current_list_key, []).append(coerce_scalar(line[4:]))
            continue
        if ":" in line and not line.startswith("  "):
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value == "":
                current_list_key = key
                result[key] = []
            else:
                current_list_key = None
                result[key] = coerce_scalar(value)
    return result


def validate_data_root(root: Path, *, create: bool) -> Path:
    root = root.expanduser().resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    elif not root.exists():
        raise RuntimeError(
            f"Data root does not exist: {root}. Run `python3 scripts/labctl.py init` first."
        )
    return root


def cadence_to_timedelta(value: Any) -> timedelta:
    if isinstance(value, (int, float)):
        return timedelta(minutes=int(value))
    if value is None:
        return timedelta(minutes=30)
    text = str(value).strip().lower()
    if text in {"hourly", "every-hour"}:
        return timedelta(hours=1)
    if text in {"daily", "every-day"}:
        return timedelta(days=1)
    if text in {"weekly", "every-week"}:
        return timedelta(days=7)

    match = re.fullmatch(r"every-(\d+)-(minute|minutes|hour|hours|day|days)", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("minute"):
            return timedelta(minutes=amount)
        if unit.startswith("hour"):
            return timedelta(hours=amount)
        return timedelta(days=amount)

    return timedelta(minutes=30)


def next_due_iso(cadence: Any, *, base: datetime | None = None) -> str:
    return ((base or now_utc()) + cadence_to_timedelta(cadence)).isoformat()


def first_nonempty_line(text: str, fallback: str = "") -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return fallback


def first_content_line(text: str, fallback: str = "") -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return fallback


def extract_markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    header = heading.strip().lower()
    collecting = False
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if collecting:
                break
            collecting = stripped[3:].strip().lower() == header
            continue
        if collecting:
            collected.append(line)
    return "\n".join(collected).strip()


def truncate(text: str, limit: int = 140) -> str:
    compact = re.sub(r"\s+", " ", text.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def metric_value(metrics: dict[str, Any]) -> float | None:
    value = metrics.get("value")
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return None


def executor_profile() -> str:
    return os.environ.get("HERMES_LAB_EXECUTOR_PROFILE", "default")


def spec_list(spec: dict[str, Any], key: str) -> list[str]:
    value = spec.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def fidelity_tiers(spec: dict[str, Any]) -> list[str]:
    tiers = spec_list(spec, "fidelity_tiers")
    if not tiers:
        return ["default"]
    seen: set[str] = set()
    ordered: list[str] = []
    for tier in tiers:
        cleaned = str(tier).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered or ["default"]


def default_fidelity_tier(spec: dict[str, Any]) -> str:
    tiers = fidelity_tiers(spec)
    candidate = str(spec.get("initial_fidelity_tier", "")).strip()
    if candidate and candidate in tiers:
        return candidate
    return tiers[0]


def normalize_fidelity_tier(spec: dict[str, Any], candidate: str | None) -> str:
    tiers = fidelity_tiers(spec)
    if candidate and candidate in tiers:
        return candidate
    return default_fidelity_tier(spec)


def next_fidelity_tier(spec: dict[str, Any], current_tier: str) -> str | None:
    tiers = fidelity_tiers(spec)
    if current_tier not in tiers:
        return None
    index = tiers.index(current_tier)
    if index + 1 >= len(tiers):
        return None
    return tiers[index + 1]


def fidelity_override_key(tier: str, key: str) -> str:
    return f"{tier}_{key}"


def resolved_spec_for_tier(spec: dict[str, Any], tier: str | None = None) -> dict[str, Any]:
    current_tier = normalize_fidelity_tier(spec, tier)
    resolved = dict(spec)
    for key in TIER_OVERRIDABLE_FIELDS:
        override_key = fidelity_override_key(current_tier, key)
        if key in {
            "mutable_paths",
            "read_only_paths",
            "ingress_files",
            "egress_files",
            "artifacts_expected",
            "worker_roles",
            "constraints",
        }:
            if override_key in spec:
                resolved[key] = spec_list(spec, override_key)
        elif override_key in spec:
            resolved[key] = spec.get(override_key)
    resolved["fidelity_tiers"] = fidelity_tiers(spec)
    resolved["current_fidelity_tier"] = current_tier
    resolved["next_fidelity_tier"] = next_fidelity_tier(spec, current_tier)
    resolved["executor_class"] = str(resolved.get("executor_class", spec.get("executor_class", "default")) or "default")
    return resolved


def resolved_mutation_command(spec: dict[str, Any]) -> str:
    explicit = str(spec.get("mutation_command", "") or "").strip()
    if explicit:
        return explicit
    provider = str(spec.get("agent_provider", "") or "").strip().lower()
    if provider in {"dispatch", "external", "agent"}:
        return ""
    if provider:
        return "python3 scripts/local_agent_mutation.py"
    return ""


def per_tier_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        tier = str(run["manifest"].get("fidelity_tier", "default") or "default")
        counts[tier] = counts.get(tier, 0) + 1
    return counts


def choose_best_run_by_tier(
    runs: list[dict[str, Any]],
    metric_direction: str,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        tier = str(run["manifest"].get("fidelity_tier", "default") or "default")
        grouped.setdefault(tier, []).append(run)
    resolved: dict[str, dict[str, Any]] = {}
    for tier, group_runs in grouped.items():
        best = choose_best_run(group_runs, metric_direction)
        if best is not None:
            resolved[tier] = best
    return resolved


def should_auto_promote_fidelity(status: dict[str, Any], spec: dict[str, Any]) -> bool:
    rule = str(spec.get("fidelity_promotion_rule", "manual") or "manual").strip().lower()
    if rule != "after-success-streak":
        return False
    current_tier = normalize_fidelity_tier(spec, str(status.get("current_fidelity_tier", "")).strip() or None)
    if next_fidelity_tier(spec, current_tier) is None:
        return False
    required = int(spec.get("promote_after_successes", 1) or 1)
    success_streak = int(status.get("success_streak_by_tier", {}).get(current_tier, 0))
    return success_streak >= required


def get_repo_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    return result.stdout.strip() or None


def resolve_command_tokens(command: str, *, base_dir: Path) -> list[str]:
    tokens = shlex.split(str(command))
    if not tokens:
        return tokens
    indices = [0]
    if tokens[0] in {"python", "python3", "bash", "sh"} and len(tokens) > 1:
        indices.append(1)
    for index in indices:
        candidate = Path(tokens[index])
        if candidate.is_absolute():
            continue
        base_candidate = (base_dir / candidate).resolve()
        if base_candidate.exists():
            tokens[index] = str(base_candidate)
    return tokens


# ---------------------------------------------------------------------------
# Lab paths
# ---------------------------------------------------------------------------


@dataclass
class LabPaths:
    root: Path
    registry: Path = field(init=False)
    events_jsonl: Path = field(init=False)
    locks: Path = field(init=False)
    experiments: Path = field(init=False)
    digests_daily: Path = field(init=False)
    digests_weekly: Path = field(init=False)
    control_inbox: Path = field(init=False)
    control_approvals: Path = field(init=False)
    control_outbox: Path = field(init=False)
    dispatch_ready: Path = field(init=False)
    dispatch_running: Path = field(init=False)
    dispatch_complete: Path = field(init=False)
    backups: Path = field(init=False)
    scratch: Path = field(init=False)
    readme_first: Path = field(init=False)
    agent_entry: Path = field(init=False)
    program_md: Path = field(init=False)
    lab_status_md: Path = field(init=False)
    lab_index_json: Path = field(init=False)
    changelog_md: Path = field(init=False)

    def __post_init__(self) -> None:
        root = self.root
        self.registry = root / "registry"
        self.events_jsonl = self.registry / "events.jsonl"
        self.locks = self.registry / "locks"
        self.experiments = root / "experiments"
        self.digests_daily = root / "digests" / "daily"
        self.digests_weekly = root / "digests" / "weekly"
        self.control_inbox = root / "control" / "inbox"
        self.control_approvals = root / "control" / "approvals"
        self.control_outbox = root / "control" / "outbox"
        self.dispatch_ready = root / "dispatch" / "ready"
        self.dispatch_running = root / "dispatch" / "running"
        self.dispatch_complete = root / "dispatch" / "complete"
        self.backups = root / "backups"
        self.scratch = root / "scratch"
        self.readme_first = root / "README-FIRST.md"
        self.agent_entry = root / "AGENT_ENTRY.md"
        self.program_md = root / "PROGRAM.md"
        self.lab_status_md = root / "LAB-STATUS.md"
        self.lab_index_json = root / "LAB-INDEX.json"
        self.changelog_md = root / "CHANGELOG.md"

    def ensure(self) -> None:
        for directory in [
            self.registry,
            self.locks,
            self.experiments,
            self.digests_daily,
            self.digests_weekly,
            self.control_inbox,
            self.control_approvals,
            self.control_outbox,
            self.dispatch_ready,
            self.dispatch_running,
            self.dispatch_complete,
            self.backups,
            self.scratch,
        ]:
            directory.mkdir(parents=True, exist_ok=True)
        if not self.events_jsonl.exists():
            self.events_jsonl.touch()


def bootstrap_root_docs(paths: LabPaths) -> None:
    if not paths.readme_first.exists():
        save_text(paths.readme_first, render_template("README-FIRST.md"))
    if not paths.agent_entry.exists():
        save_text(paths.agent_entry, render_template("AGENT_ENTRY.md"))
    if not paths.program_md.exists():
        save_text(paths.program_md, render_template("PROGRAM.md"))
    if not paths.changelog_md.exists():
        save_text(paths.changelog_md, render_template("CHANGELOG.md"))
    if not paths.lab_status_md.exists():
        save_text(paths.lab_status_md, render_template("LAB-STATUS.md"))


def get_paths(data_root: str | Path | None = None, *, create: bool = False) -> LabPaths:
    raw_root = Path(data_root or os.environ.get("HERMES_LAB_DATA_ROOT", str(DEFAULT_DATA_ROOT)))
    root = validate_data_root(raw_root, create=create)
    paths = LabPaths(root=root)
    paths.ensure()
    bootstrap_root_docs(paths)
    return paths


# ---------------------------------------------------------------------------
# Event ledger and control records
# ---------------------------------------------------------------------------


def emit_event(paths: LabPaths, event_type: str, data: dict[str, Any]) -> None:
    append_jsonl(
        paths.events_jsonl,
        {
            "schema_version": SCHEMA_VERSION,
            "type": event_type,
            "ts": now_iso(),
            **data,
        },
    )


def record_command(
    paths: LabPaths,
    intent: str,
    *,
    actor: str = "labctl",
    target: str | None = None,
    parameters: dict[str, Any] | None = None,
    approval_class: str = "low",
) -> Path:
    command_id = f"CMD-{now_ts()}-{intent}"
    record = {
        "schema_version": SCHEMA_VERSION,
        "command_id": command_id,
        "ts": now_iso(),
        "actor": actor,
        "target": target,
        "intent": intent,
        "parameters": parameters or {},
        "approval_class": approval_class,
        "idempotency_key": command_id,
    }
    out = paths.control_inbox / f"{command_id}.json"
    save_json(out, record)
    emit_event(paths, "command_recorded", {"command_id": command_id, "intent": intent, "target": target})
    return out


# ---------------------------------------------------------------------------
# Experiment helpers
# ---------------------------------------------------------------------------


def experiment_dir(paths: LabPaths, exp_id: str) -> Path:
    return paths.experiments / exp_id


def load_spec(exp_dir: Path) -> dict[str, Any]:
    return parse_simple_yaml((exp_dir / "SPEC.yaml").read_text())


def list_experiments(paths: LabPaths) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not paths.experiments.exists():
        return results
    for directory in sorted(paths.experiments.iterdir()):
        status_file = directory / "STATUS.json"
        if status_file.exists():
            results.append(load_json(status_file, {}))
    return results


def create_experiment(paths: LabPaths, spec_path: Path, *, actor: str = "labctl") -> dict[str, Any]:
    raw = spec_path.read_text()
    spec = parse_simple_yaml(raw)
    exp_id = spec.get("id")
    if not exp_id:
        raise ValueError("SPEC must have an id field")
    tier = default_fidelity_tier(spec)
    resolved = resolved_spec_for_tier(spec, tier)

    exp_dir = experiment_dir(paths, exp_id)
    if exp_dir.exists():
        raise ValueError(f"Experiment already exists: {exp_id}")

    (exp_dir / "runs").mkdir(parents=True)
    (exp_dir / "checkpoints").mkdir(parents=True)
    save_text(exp_dir / "SPEC.yaml", raw)
    (exp_dir / "metrics.jsonl").touch()

    status = {
        "schema_version": SCHEMA_VERSION,
        "id": exp_id,
        "phase": "queued",
        "created_at": now_iso(),
        "created_by": actor,
        "goal": spec.get("goal", ""),
        "metric": spec.get("metric", ""),
        "metric_direction": spec.get("metric_direction", "maximize"),
        "priority": spec.get("priority", "normal"),
        "autonomous": bool(spec.get("autonomous", True)),
        "fidelity_tiers": fidelity_tiers(spec),
        "current_fidelity_tier": tier,
        "next_fidelity_tier": next_fidelity_tier(spec, tier),
        "executor_class": str(resolved.get("executor_class", "default") or "default"),
        "run_count_by_tier": {},
        "success_streak_by_tier": {},
        "best_run_by_tier": {},
        "best_metric_by_tier": {},
        "run_count": 0,
        "last_run": None,
        "last_run_at": None,
        "last_outcome": None,
        "failure_streak": 0,
        "best_run": None,
        "best_metric_value": None,
        "next_due_at": now_iso(),
        "current_lease": None,
        "current_dispatch": None,
        "blocked_reason": None,
        # Optional metadata (from SPEC or defaults)
        "tags": spec_list(spec, "tags"),
        "estimated_runtime_minutes": int(spec.get("estimated_runtime_minutes", 0) or 0),
        "notify": str(spec.get("notify", "silent") or "silent"),
        "parent_experiment": str(spec.get("parent_experiment", "") or ""),
        "known_good_config": str(spec.get("known_good_config", "") or ""),
    }
    save_json(exp_dir / "STATUS.json", status)

    save_text(
        exp_dir / "SUMMARY.md",
        "\n".join(
            [
                f"# {exp_id}",
                "",
                f"Goal: {spec.get('goal', '(missing goal)')}",
                f"Metric: {spec.get('metric', '(missing metric)')}",
                "Phase: queued",
                "",
                "No runs yet.",
                "",
            ]
        ),
    )
    save_text(
        exp_dir / "NEXT.md",
        "\n".join(
            [
                f"# Next Action for {exp_id}",
                "",
                "Begin the first bounded iteration.",
                "Start with the role order in SPEC.yaml and stay within the time budget.",
                "",
            ]
        ),
    )
    save_text(
        exp_dir / "context.md",
        "\n".join(
            [
                f"# Context - {exp_id}",
                "",
                "No runs yet.",
                "",
            ]
        ),
    )
    save_text(
        exp_dir / "best.md",
        "\n".join(
            [
                f"# Best Result - {exp_id}",
                "",
                "No results yet.",
                "",
            ]
        ),
    )
    save_text(exp_dir / "RUNBOOK.md", build_runbook_md(exp_id, resolved, status))

    emit_event(
        paths,
        "experiment_created",
        {
            "experiment": exp_id,
            "goal": spec.get("goal", ""),
            "source_spec": str(spec_path),
            "fidelity_tiers": fidelity_tiers(spec),
            "initial_fidelity_tier": tier,
        },
    )
    return status


def get_status(paths: LabPaths, exp_id: str) -> dict[str, Any] | None:
    status_path = experiment_dir(paths, exp_id) / "STATUS.json"
    if not status_path.exists():
        return None
    return load_json(status_path, None)


def save_status(paths: LabPaths, exp_id: str, status: dict[str, Any]) -> None:
    # Guard: clamp next_due_at to max 30 days in the future.
    # Prevents poisoned dates (e.g. 2099) from blocking experiments.
    next_due = status.get("next_due_at")
    if next_due:
        try:
            due_dt = parse_iso(str(next_due))
            max_future = now_utc() + timedelta(days=30)
            if due_dt and due_dt > max_future:
                status["next_due_at"] = max_future.isoformat()
        except Exception:
            pass
    save_json(experiment_dir(paths, exp_id) / "STATUS.json", status)


def set_current_dispatch(
    paths: LabPaths,
    exp_id: str,
    dispatch_state: dict[str, Any] | None,
) -> dict[str, Any]:
    status = get_status(paths, exp_id)
    if status is None:
        raise ValueError(f"Experiment not found: {exp_id}")
    status["current_dispatch"] = dispatch_state
    save_status(paths, exp_id, status)
    return status


def set_phase(paths: LabPaths, exp_id: str, phase: str, reason: str = "") -> dict[str, Any]:
    status = get_status(paths, exp_id)
    if status is None:
        raise ValueError(f"Experiment not found: {exp_id}")
    previous = status.get("phase")
    status["phase"] = phase
    status["phase_changed_at"] = now_iso()
    status["blocked_reason"] = reason or None
    save_status(paths, exp_id, status)
    emit_event(
        paths,
        "phase_changed",
        {"experiment": exp_id, "from": previous, "to": phase, "reason": reason},
    )
    rebuild_experiment(paths, exp_id)
    return status


def set_fidelity_tier(
    paths: LabPaths,
    exp_id: str,
    tier: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    exp_dir = experiment_dir(paths, exp_id)
    spec = load_spec(exp_dir)
    tiers = fidelity_tiers(spec)
    if tier not in tiers:
        raise ValueError(f"Unknown fidelity tier for {exp_id}: {tier}. Expected one of: {', '.join(tiers)}")

    status = get_status(paths, exp_id)
    if status is None:
        raise ValueError(f"Experiment not found: {exp_id}")

    previous = normalize_fidelity_tier(spec, str(status.get("current_fidelity_tier", "")).strip() or None)
    status["fidelity_tiers"] = tiers
    status["current_fidelity_tier"] = tier
    status["next_fidelity_tier"] = next_fidelity_tier(spec, tier)
    status["executor_class"] = str(
        resolved_spec_for_tier(spec, tier).get("executor_class", status.get("executor_class", "default")) or "default"
    )
    save_status(paths, exp_id, status)
    emit_event(
        paths,
        "fidelity_tier_changed",
        {
            "experiment": exp_id,
            "from": previous,
            "to": tier,
            "reason": reason,
        },
    )
    rebuild_experiment(paths, exp_id)
    return status


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


def lease_dir(paths: LabPaths, exp_id: str) -> Path:
    return paths.locks / f"{exp_id}.lock"


def read_lease(paths: LabPaths, exp_id: str) -> dict[str, Any] | None:
    info_path = lease_dir(paths, exp_id) / "info.json"
    if not info_path.exists():
        return None
    return load_json(info_path, None)


def is_lease_expired(lease: dict[str, Any]) -> bool:
    expires_at = parse_iso(lease.get("expires_at"))
    return expires_at is not None and expires_at <= now_utc()


def clear_lease(paths: LabPaths, exp_id: str, *, event_type: str | None = None, reason: str = "") -> None:
    lock_dir = lease_dir(paths, exp_id)
    lease = read_lease(paths, exp_id)
    if lock_dir.exists():
        shutil.rmtree(lock_dir)
    status = get_status(paths, exp_id)
    if status is not None and status.get("current_lease"):
        status["current_lease"] = None
        save_status(paths, exp_id, status)
    if lease and event_type:
        emit_event(
            paths,
            event_type,
            {
                "experiment": exp_id,
                "lease_id": lease.get("lease_id"),
                "reason": reason,
            },
        )


def acquire_lease(
    paths: LabPaths,
    exp_id: str,
    *,
    owner: str,
    ttl_seconds: int,
    run_id: str | None = None,
) -> dict[str, Any]:
    lock_dir = lease_dir(paths, exp_id)
    if lock_dir.exists():
        lease = read_lease(paths, exp_id)
        if lease and is_lease_expired(lease):
            clear_lease(paths, exp_id, event_type="lease_reclaimed", reason="expired")
        else:
            raise RuntimeError(f"Experiment already leased: {exp_id}")

    lock_dir.mkdir()
    lease = {
        "schema_version": SCHEMA_VERSION,
        "lease_id": f"LEASE-{now_ts()}-{exp_id}",
        "experiment": exp_id,
        "owner": owner,
        "run_id": run_id,
        "acquired_at": now_iso(),
        "expires_at": (now_utc() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    save_json(lock_dir / "info.json", lease)

    status = get_status(paths, exp_id)
    if status is None:
        raise ValueError(f"Experiment not found: {exp_id}")
    status["current_lease"] = lease
    save_status(paths, exp_id, status)
    emit_event(paths, "lease_acquired", {"experiment": exp_id, "lease_id": lease["lease_id"]})
    return lease


def release_lease(paths: LabPaths, exp_id: str, *, lease_id: str | None = None) -> None:
    lease = read_lease(paths, exp_id)
    if lease_id and lease and lease.get("lease_id") != lease_id:
        raise RuntimeError(f"Lease mismatch for {exp_id}")
    clear_lease(paths, exp_id, event_type="lease_released", reason="normal")


# ---------------------------------------------------------------------------
# Run execution and reduction
# ---------------------------------------------------------------------------


def make_run_id(role: str) -> str:
    return f"RUN-{now_ts()}-{role}"


def build_run_manifest(
    exp_id: str,
    *,
    run_id: str,
    role: str,
    lease: dict[str, Any],
    spec: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "experiment": exp_id,
        "role": role,
        "started_at": now_iso(),
        "executor_profile": executor_profile(),
        "repo_root": str(REPO_ROOT),
        "repo_revision": get_repo_revision(),
        "lease_id": lease["lease_id"],
        "fidelity_tier": str(spec.get("current_fidelity_tier", "default") or "default"),
        "executor_class": str(spec.get("executor_class", "default") or "default"),
        "time_budget_minutes": int(spec.get("time_budget_minutes", 10)),
        "status": status,
    }


def create_run_bundle_at(
    run_dir: Path,
    exp_id: str,
    *,
    run_id: str,
    role: str,
    lease: dict[str, Any],
    spec: dict[str, Any],
    initial_status: str = "running",
) -> Path:
    run_dir.mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    save_json(
        run_dir / "manifest.json",
        build_run_manifest(
            exp_id,
            run_id=run_id,
            role=role,
            lease=lease,
            spec=spec,
            status=initial_status,
        ),
    )
    return run_dir


def create_run_bundle(
    paths: LabPaths,
    exp_id: str,
    *,
    role: str,
    lease: dict[str, Any],
    spec: dict[str, Any],
) -> Path:
    run_id = make_run_id(role)
    run_dir = experiment_dir(paths, exp_id) / "runs" / run_id
    return create_run_bundle_at(
        run_dir,
        exp_id,
        run_id=run_id,
        role=role,
        lease=lease,
        spec=spec,
        initial_status="running",
    )


def dispatch_stage_dir(paths: LabPaths, stage: str) -> Path:
    mapping = {
        "ready": paths.dispatch_ready,
        "running": paths.dispatch_running,
        "complete": paths.dispatch_complete,
    }
    if stage not in mapping:
        raise ValueError(f"Unknown dispatch stage: {stage}")
    return mapping[stage]


def list_dispatch_packages(paths: LabPaths, *, stage: str | None = None) -> list[dict[str, Any]]:
    stages = [stage] if stage else ["ready", "running", "complete"]
    packages: list[dict[str, Any]] = []
    for current_stage in stages:
        base_dir = dispatch_stage_dir(paths, current_stage)
        for package_dir in sorted(base_dir.iterdir()):
            if not package_dir.is_dir():
                continue
            record = load_json(package_dir / "dispatch.json", None)
            if not isinstance(record, dict):
                continue
            packages.append(
                {
                    "dir": package_dir,
                    "stage_dir": current_stage,
                    "record": record,
                }
            )
    packages.sort(
        key=lambda item: (
            str(item["record"].get("queued_at", "")),
            str(item["record"].get("dispatch_id", "")),
        )
    )
    return packages


def find_dispatch_package(paths: LabPaths, dispatch_id: str) -> dict[str, Any]:
    for package in list_dispatch_packages(paths):
        if str(package["record"].get("dispatch_id")) == dispatch_id:
            return package
    raise ValueError(f"Dispatch package not found: {dispatch_id}")


def dispatch_view(record: dict[str, Any], package_dir: Path | None = None) -> dict[str, Any]:
    return {
        "dispatch_id": record.get("dispatch_id"),
        "stage": record.get("stage"),
        "worker": record.get("worker"),
        "run_id": record.get("run_id"),
        "role": record.get("role"),
        "fidelity_tier": record.get("fidelity_tier"),
        "executor_class": record.get("executor_class"),
        "queued_at": record.get("queued_at"),
        "claimed_at": record.get("claimed_at"),
        "completed_at": record.get("completed_at"),
        "ingested_at": record.get("ingested_at"),
        "path": str(package_dir) if package_dir is not None else record.get("path"),
    }


def write_dispatch_package_readme(dispatch_dir: Path, record: dict[str, Any]) -> None:
    lines = [
        f"# Dispatch Package - {record.get('dispatch_id')}",
        "",
        f"Experiment: {record.get('experiment')}",
        f"Run ID: {record.get('run_id')}",
        f"Role: {record.get('role')}",
        f"Fidelity tier: {record.get('fidelity_tier', 'default')}",
        f"Executor class: {record.get('executor_class', 'default')}",
        f"Stage: {record.get('stage')}",
        "",
        "## Read First",
        "- `dispatch.json` for machine-readable metadata",
        "- `input/RUNBOOK.md` for experiment rules",
        "- `run/plan.md` for the current bounded task",
        "",
        "## Write Rules",
        "- Only write inside `run/` while executing this dispatch package.",
        "- `run/RESULT.md` and `run/metrics.json` are required before completion.",
        "- Do not edit canonical experiment folders directly from a dispatch package.",
        "",
        "## Control",
        "- Claim a package: `python3 scripts/labctl.py dispatch-claim --max-runs 1`",
        "- Execute a claimed package locally: `python3 scripts/labctl.py dispatch-work --max-runs 1`",
        f"- Mark complete after external work: `python3 scripts/labctl.py dispatch-complete {record.get('dispatch_id')}`",
        f"- Ingest into canonical history: `python3 scripts/labctl.py dispatch-ingest {record.get('dispatch_id')}`",
        "",
    ]
    save_text(dispatch_dir / "README.md", "\n".join(lines))


def snapshot_dispatch_inputs(paths: LabPaths, exp_id: str, dispatch_dir: Path) -> None:
    exp_dir = experiment_dir(paths, exp_id)
    inputs = dispatch_dir / "input"
    inputs.mkdir(parents=True, exist_ok=True)
    copies = [
        (REPO_ROOT / "AGENTS.md", inputs / "AGENTS.md"),
        (REPO_ROOT / "LAB_MANIFEST.json", inputs / "LAB_MANIFEST.json"),
        (paths.readme_first, inputs / "README-FIRST.md"),
        (paths.lab_status_md, inputs / "LAB-STATUS.md"),
        (paths.lab_index_json, inputs / "LAB-INDEX.json"),
        (paths.program_md, inputs / "PROGRAM.md"),
        (exp_dir / "SPEC.yaml", inputs / "SPEC.yaml"),
        (exp_dir / "STATUS.json", inputs / "STATUS.json"),
        (exp_dir / "RUNBOOK.md", inputs / "RUNBOOK.md"),
        (exp_dir / "SUMMARY.md", inputs / "SUMMARY.md"),
        (exp_dir / "NEXT.md", inputs / "NEXT.md"),
        (exp_dir / "context.md", inputs / "context.md"),
        (exp_dir / "best.md", inputs / "best.md"),
    ]
    for source, target in copies:
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def eligible_experiments(
    paths: LabPaths,
    *,
    allowed_executor_classes: list[str] | None = None,
) -> list[dict[str, Any]]:
    experiments = list_experiments(paths)
    allowed = {
        str(item).strip()
        for item in (allowed_executor_classes or [])
        if str(item).strip()
    }
    eligible = [
        experiment
        for experiment in experiments
        if experiment.get("phase") in {"queued", "ready", "active"}
        and is_due(experiment)
        and not experiment.get("current_lease")
        and (
            not allowed
            or str(experiment.get("executor_class", "default") or "default") in allowed
        )
    ]
    eligible.sort(key=lambda item: DEFAULT_PRIORITY_ORDER.get(item.get("priority", "normal"), 2))
    return eligible


def build_run_plan(paths: LabPaths, exp_id: str, role: str, spec: dict[str, Any]) -> str:
    exp_dir = experiment_dir(paths, exp_id)
    next_text = load_text(exp_dir / "NEXT.md", "")
    summary = load_text(exp_dir / "SUMMARY.md", "")
    lines = [
        f"# Plan - {exp_id}",
        "",
        f"Timestamp: {now_iso()}",
        f"Role: {role}",
        f"Goal: {spec.get('goal', '')}",
        f"Metric: {spec.get('metric', '')}",
        f"Metric direction: {spec.get('metric_direction', 'maximize')}",
        f"Fidelity tier: {spec.get('current_fidelity_tier', 'default')}",
        f"Executor class: {spec.get('executor_class', 'default')}",
        f"Time budget (minutes): {int(spec.get('time_budget_minutes', 10))}",
        f"Workspace root: {spec.get('workspace_root', '(not set)')}",
        "",
    ]

    mutable_paths = spec_list(spec, "mutable_paths")
    read_only_paths = spec_list(spec, "read_only_paths")
    artifacts_expected = spec_list(spec, "artifacts_expected")
    ingress_files = spec_list(spec, "ingress_files")
    mutable_command = resolved_mutation_command(spec)
    promotion_strategy = spec.get("promotion_strategy")
    workspace_mode = spec.get("workspace_mode")
    agent_provider = str(spec.get("agent_provider", "") or "").strip()
    agent_model = str(spec.get("agent_model", "") or "").strip()
    agent_effort = str(spec.get("agent_effort", "") or "").strip()

    if mutable_paths:
        lines.extend(["## Mutable Surfaces", *[f"- `{item}`" for item in mutable_paths], ""])
    if read_only_paths:
        lines.extend(["## Read-only Surfaces", *[f"- `{item}`" for item in read_only_paths], ""])
    if ingress_files:
        lines.extend(["## Ingress Files", *[f"- `{item}`" for item in ingress_files], ""])
    if spec.get("setup_command"):
        lines.extend(["## Setup Command", f"`{spec.get('setup_command')}`", ""])
    if spec.get("validation_command"):
        lines.extend(["## Validation Command", f"`{spec.get('validation_command')}`", ""])
    if mutable_command:
        lines.extend(["## Mutation Command", f"`{mutable_command}`", ""])
    if agent_provider:
        lines.extend(["## Agent Provider", f"`{agent_provider}`", ""])
    if agent_model:
        lines.extend(["## Agent Model", f"`{agent_model}`", ""])
    if agent_effort:
        lines.extend(["## Agent Effort", f"`{agent_effort}`", ""])
    if spec.get("acceptance_rule"):
        lines.extend(["## Acceptance Rule", str(spec.get("acceptance_rule")), ""])
    if promotion_strategy:
        lines.extend(["## Promotion Strategy", str(promotion_strategy), ""])
    if workspace_mode:
        lines.extend(["## Workspace Mode", str(workspace_mode), ""])
    if artifacts_expected:
        lines.extend(["## Expected Artifacts", *[f"- `{item}`" for item in artifacts_expected], ""])

    lines.extend(
        [
        "## Summary Snapshot",
        truncate(summary, 500),
        "",
        "## Relay Baton",
        next_text.strip() or "No next action recorded.",
        "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_runbook_md(exp_id: str, spec: dict[str, Any], status: dict[str, Any]) -> str:
    mutable_paths = spec_list(spec, "mutable_paths")
    read_only_paths = spec_list(spec, "read_only_paths")
    ingress_files = spec_list(spec, "ingress_files")
    egress_files = spec_list(spec, "egress_files")
    artifacts_expected = spec_list(spec, "artifacts_expected")
    worker_roles = spec_list(spec, "worker_roles")

    lines = [
        f"# Runbook - {exp_id}",
        "",
        f"Goal: {spec.get('goal', '')}",
        f"Metric: {spec.get('metric', '')}",
        f"Metric direction: {spec.get('metric_direction', 'maximize')}",
        f"Phase: {status.get('phase', 'queued')}",
        f"Current fidelity tier: {status.get('current_fidelity_tier', spec.get('current_fidelity_tier', 'default'))}",
        f"Executor class: {status.get('executor_class', spec.get('executor_class', 'default'))}",
        f"Cadence: {spec.get('cadence', 'every-30-minutes')}",
        f"Time budget (minutes): {int(spec.get('time_budget_minutes', 10))}",
        f"Workspace root: {spec.get('workspace_root', '(supplied by executor or repo root)')}",
        "",
        "## Ingress",
    ]

    if ingress_files:
        lines.extend([f"- `{item}`" for item in ingress_files])
    else:
        lines.extend(
            [
                "- `LAB-STATUS.md`",
                "- `PROGRAM.md`",
                "- `SUMMARY.md`",
                "- `NEXT.md`",
                "- `SPEC.yaml`",
            ]
        )
    lines.append("")

    if mutable_paths:
        lines.extend(["## Mutable Surfaces", *[f"- `{item}`" for item in mutable_paths], ""])
    if read_only_paths:
        lines.extend(["## Read-only Surfaces", *[f"- `{item}`" for item in read_only_paths], ""])
    if spec.get("setup_command"):
        lines.extend(["## Setup Command", f"`{spec.get('setup_command')}`", ""])
    if spec.get("baseline_command"):
        lines.extend(["## Baseline Command", f"`{spec.get('baseline_command')}`", ""])
    if spec.get("executor_command"):
        lines.extend(["## Executor Command", f"`{spec.get('executor_command')}`", ""])
    if resolved_mutation_command(spec):
        lines.extend(["## Mutation Command", f"`{resolved_mutation_command(spec)}`", ""])
    if spec.get("agent_provider"):
        lines.extend(["## Agent Provider", f"`{spec.get('agent_provider')}`", ""])
    if spec.get("agent_model"):
        lines.extend(["## Agent Model", f"`{spec.get('agent_model')}`", ""])
    if spec.get("agent_effort"):
        lines.extend(["## Agent Effort", f"`{spec.get('agent_effort')}`", ""])
    if spec.get("agent_instruction_file"):
        lines.extend(["## Agent Instruction File", f"`{spec.get('agent_instruction_file')}`", ""])
    if spec.get("validation_command"):
        lines.extend(["## Validation Command", f"`{spec.get('validation_command')}`", ""])
    if spec.get("acceptance_rule"):
        lines.extend(["## Acceptance Rule", str(spec.get("acceptance_rule")), ""])
    if spec.get("promotion_strategy"):
        lines.extend(["## Promotion Strategy", str(spec.get("promotion_strategy")), ""])
    if spec.get("workspace_mode"):
        lines.extend(["## Workspace Mode", str(spec.get("workspace_mode")), ""])
    if "require_clean_workspace" in spec:
        lines.extend(["## Require Clean Workspace", str(bool(spec.get("require_clean_workspace"))), ""])
    if worker_roles:
        lines.extend(["## Role Rotation", *[f"- `{item}`" for item in worker_roles], ""])
    tiers = spec.get("fidelity_tiers") or status.get("fidelity_tiers") or ["default"]
    if tiers:
        lines.extend(["## Fidelity Tiers", *[f"- `{item}`" for item in tiers], ""])
        if status.get("next_fidelity_tier"):
            lines.extend(["## Next Fidelity Tier", f"`{status.get('next_fidelity_tier')}`", ""])
    if egress_files:
        lines.extend(["## Required Egress Files", *[f"- `{item}`" for item in egress_files], ""])
    else:
        lines.extend(
            [
                "## Required Egress Files",
                "- `RESULT.md`",
                "- `metrics.json`",
                "- `stdout.log`",
                "- `stderr.log`",
                "",
            ]
        )
    if artifacts_expected:
        lines.extend(["## Expected Artifacts", *[f"- `{item}`" for item in artifacts_expected], ""])
    lines.extend(
        [
            "## Management",
            "- Scheduler acquires one lease per experiment before a run.",
            "- Executors only write inside the claimed run bundle.",
            "- `RESULT.md` is the write-ahead artifact.",
            "- Reducer rebuilds `SUMMARY.md`, `NEXT.md`, `context.md`, `best.md`, and checkpoint pointers after sealing.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_stub_result(
    paths: LabPaths,
    exp_id: str,
    role: str,
    iteration: int,
    spec: dict[str, Any],
) -> tuple[str, dict[str, Any], str, str, str]:
    exp_dir = experiment_dir(paths, exp_id)
    next_action = extract_markdown_section(load_text(exp_dir / "NEXT.md"), "Next Action for")
    if not next_action:
        next_action = load_text(exp_dir / "NEXT.md", "Review the current baton and continue.")
    role_notes = {
        "scout": "Scanned the current baton and surfaced the next narrow thread to pursue.",
        "researcher": "Deepened the currently highest-signal thread inside the existing constraints.",
        "critic": "Challenged the current direction and looked for failure modes or weak assumptions.",
        "synthesizer": "Compressed recent work into a shorter baton for the next run.",
    }
    result_md = "\n".join(
        [
            f"# {exp_id} - Iteration {iteration}",
            "",
            f"Timestamp: {now_iso()}",
            f"Worker: {role}",
            "",
            "## Hypothesis",
            f"The next useful step is to continue the `{role}` role within the current task box.",
            "",
            "## Method",
            role_notes.get(role, "Ran the configured bounded iteration."),
            "",
            "## Result",
            "No external executor command is configured yet, so this run produced a scaffolded bundle only.",
            "",
            "## Interpretation",
            "The experiment state is healthy and ready for a real executor. The baton remains intentionally conservative.",
            "",
            "## Next",
            first_content_line(
                next_action, "Review the current baton and configure an executor command."
            ),
            "",
        ]
    )
    metric_name = spec.get("metric", "placeholder_metric")
    metrics = {
        "iteration": iteration,
        "metric": metric_name,
        "value": 0,
        "source": "built-in-stub",
    }
    stdout = "No executor_command configured. Built-in stub executed.\n"
    stderr = ""
    return result_md, metrics, "success", stdout, stderr


def run_executor(
    paths: LabPaths,
    exp_id: str,
    run_dir: Path,
    *,
    role: str,
    spec: dict[str, Any],
    lease: dict[str, Any],
    iteration: int,
    env_overrides: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any], str]:
    timeout_seconds = int(spec.get("time_budget_minutes", 10)) * 60
    command = spec.get("executor_command") or os.environ.get("HERMES_LAB_EXECUTOR_COMMAND")
    status = get_status(paths, exp_id) or {}

    save_text(run_dir / "plan.md", build_run_plan(paths, exp_id, role, spec))

    if not command:
        result_md, metrics, outcome, stdout, stderr = build_stub_result(
            paths, exp_id, role, iteration, spec
        )
        save_text(run_dir / "stdout.log", stdout)
        save_text(run_dir / "stderr.log", stderr)
        return result_md, metrics, outcome

    env = os.environ.copy()
    env.update(
        {
            "LAB_SCHEMA_VERSION": str(SCHEMA_VERSION),
            "LAB_DATA_ROOT": str(paths.root),
            "LAB_REPO_ROOT": str(REPO_ROOT),
            "LAB_EXPERIMENT_ID": exp_id,
            "LAB_RUN_ID": run_dir.name,
            "LAB_RUN_DIR": str(run_dir),
            "LAB_SPEC_PATH": str(experiment_dir(paths, exp_id) / "SPEC.yaml"),
            "LAB_SUMMARY_PATH": str(experiment_dir(paths, exp_id) / "SUMMARY.md"),
            "LAB_NEXT_PATH": str(experiment_dir(paths, exp_id) / "NEXT.md"),
            "LAB_CONTEXT_PATH": str(experiment_dir(paths, exp_id) / "context.md"),
            "LAB_PROGRAM_PATH": str(paths.program_md),
            "LAB_ROLE": role,
            "LAB_LEASE_ID": lease["lease_id"],
            "LAB_TIME_BUDGET_MINUTES": str(int(spec.get("time_budget_minutes", 10))),
            "LAB_RUN_ITERATION": str(iteration),
            "LAB_PRIMARY_METRIC": str(spec.get("metric", "")),
            "LAB_METRIC_DIRECTION": str(spec.get("metric_direction", "maximize")),
            "LAB_FIDELITY_TIER": str(spec.get("current_fidelity_tier", "default")),
            "LAB_FIDELITY_TIERS": "\n".join([str(item) for item in spec.get("fidelity_tiers", ["default"])]),
            "LAB_NEXT_FIDELITY_TIER": str(spec.get("next_fidelity_tier", "") or ""),
            "LAB_EXECUTOR_CLASS": str(spec.get("executor_class", "default")),
            "LAB_WORKSPACE_ROOT": str(spec.get("workspace_root", "")),
            "LAB_SETUP_COMMAND": str(spec.get("setup_command", "")),
            "LAB_BASELINE_COMMAND": str(spec.get("baseline_command", "")),
            "LAB_MUTATION_COMMAND": str(resolved_mutation_command(spec)),
            "LAB_AGENT_PROVIDER": str(spec.get("agent_provider", "")),
            "LAB_AGENT_MODEL": str(spec.get("agent_model", "")),
            "LAB_AGENT_EFFORT": str(spec.get("agent_effort", "")),
            "LAB_AGENT_INSTRUCTION_FILE": str(spec.get("agent_instruction_file", "")),
            "LAB_AGENT_BASE_URL": str(spec.get("agent_base_url", "")),
            "LAB_AGENT_BACKGROUND": str(bool(spec.get("agent_background", False))).lower(),
            "LAB_VALIDATION_COMMAND": str(spec.get("validation_command", "")),
            "LAB_ACCEPTANCE_RULE": str(spec.get("acceptance_rule", "")),
            "LAB_PROMOTION_STRATEGY": str(spec.get("promotion_strategy", "")),
            "LAB_WORKSPACE_MODE": str(spec.get("workspace_mode", "")),
            "LAB_REQUIRE_CLEAN_WORKSPACE": str(bool(spec.get("require_clean_workspace", False))).lower(),
            "LAB_MUTABLE_PATHS": "\n".join(spec_list(spec, "mutable_paths")),
            "LAB_READ_ONLY_PATHS": "\n".join(spec_list(spec, "read_only_paths")),
            "LAB_INGRESS_FILES": "\n".join(spec_list(spec, "ingress_files")),
            "LAB_EGRESS_FILES": "\n".join(spec_list(spec, "egress_files")),
            "LAB_ARTIFACTS_EXPECTED": "\n".join(spec_list(spec, "artifacts_expected")),
            "LAB_BEST_RUN_ID": str(status.get("best_run") or ""),
            "LAB_BEST_METRIC_VALUE": str(
                status.get("best_metric_value")
                if status.get("best_metric_value") is not None
                else ""
            ),
            "LAB_DISPATCH_ID": "",
            "LAB_DISPATCH_DIR": "",
            "LAB_DISPATCH_STAGE": "",
        }
    )
    if env_overrides:
        env.update({key: str(value) for key, value in env_overrides.items()})

    try:
        completed = subprocess.run(
            resolve_command_tokens(str(command), base_dir=REPO_ROOT),
            cwd=run_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        save_text(run_dir / "stdout.log", completed.stdout)
        save_text(run_dir / "stderr.log", completed.stderr)
    except subprocess.TimeoutExpired as error:
        save_text(run_dir / "stdout.log", error.stdout or "")
        save_text(run_dir / "stderr.log", error.stderr or "")
        result_md = "\n".join(
            [
                f"# {exp_id} - Iteration {iteration}",
                "",
                "## Hypothesis",
                "Run the configured executor inside the task time budget.",
                "",
                "## Method",
                f"Command: {command}",
                "",
                "## Result",
                f"Timed out after {timeout_seconds} seconds.",
                "",
                "## Interpretation",
                "The configured executor exceeded the allowed budget and should be tightened or debugged.",
                "",
                "## Next",
                "Inspect stdout.log and stderr.log, then reduce scope or increase executor efficiency.",
                "",
            ]
        )
        return (
            result_md,
            {
                "iteration": iteration,
                "metric": spec.get("metric", "timeout"),
                "value": 0,
                "source": "executor",
                "error": "timeout",
            },
            "timeout",
        )

    result_path = run_dir / "RESULT.md"
    if result_path.exists():
        result_md = load_text(result_path)
    else:
        primary = first_nonempty_line(completed.stdout, "Executor produced no stdout.")
        result_md = "\n".join(
            [
                f"# {exp_id} - Iteration {iteration}",
                "",
                "## Hypothesis",
                "Run the configured executor and capture the output in a sealed run bundle.",
                "",
                "## Method",
                f"Command: {command}",
                "",
                "## Result",
                primary,
                "",
                "## Interpretation",
                "Executor output was captured directly because RESULT.md was not written by the worker.",
                "",
                "## Next",
                "Promote a more explicit RESULT.md and metrics.json contract in the executor.",
                "",
            ]
        )

    metrics_path = run_dir / "metrics.json"
    if metrics_path.exists():
        metrics = load_json(metrics_path, {})
    else:
        metrics = {
            "iteration": iteration,
            "metric": spec.get("metric", "executor_exit_code"),
            "value": 1 if completed.returncode == 0 else 0,
            "source": "executor",
            "returncode": completed.returncode,
        }

    outcome = "success" if completed.returncode == 0 else "failed"
    return result_md, metrics, outcome


def choose_best_run(
    runs: list[dict[str, Any]],
    metric_direction: str,
) -> dict[str, Any] | None:
    comparable = [run for run in runs if metric_value(run["metrics"]) is not None]
    if comparable:
        reverse = metric_direction != "minimize"
        return sorted(comparable, key=lambda item: metric_value(item["metrics"]) or 0.0, reverse=reverse)[0]
    successes = [run for run in runs if run["manifest"].get("status") == "success"]
    if successes:
        return successes[-1]
    return runs[-1] if runs else None


def build_summary_md(
    exp_id: str,
    spec: dict[str, Any],
    status: dict[str, Any],
    latest_run: dict[str, Any] | None,
    best_run: dict[str, Any] | None,
) -> str:
    lines = [
        f"# {exp_id}",
        "",
        f"Goal: {spec.get('goal', '')}",
        f"Metric: {spec.get('metric', '')}",
        f"Phase: {status.get('phase', 'unknown')}",
        f"Priority: {status.get('priority', 'normal')}",
        f"Current fidelity tier: {status.get('current_fidelity_tier', 'default')}",
        f"Executor class: {status.get('executor_class', 'default')}",
        f"Run count: {status.get('run_count', 0)}",
        f"Failure streak: {status.get('failure_streak', 0)}",
        "",
    ]
    if latest_run:
        interpretation = extract_markdown_section(latest_run["result"], "Interpretation")
        lines.extend(
            [
                "## Latest Run",
                f"Run: {latest_run['manifest'].get('run_id')}",
                f"Role: {latest_run['manifest'].get('role')}",
                f"Fidelity tier: {latest_run['manifest'].get('fidelity_tier', 'default')}",
                f"Outcome: {latest_run['manifest'].get('status')}",
                f"Metric value: {latest_run['metrics'].get('value')}",
                truncate(interpretation or latest_run["result"], 300),
                "",
            ]
        )
    if best_run:
        interpretation = extract_markdown_section(best_run["result"], "Interpretation")
        lines.extend(
            [
                "## Best So Far",
                f"Run: {best_run['manifest'].get('run_id')}",
                f"Fidelity tier: {best_run['manifest'].get('fidelity_tier', 'default')}",
                f"Metric value: {best_run['metrics'].get('value')}",
                truncate(interpretation or best_run["result"], 300),
                "",
            ]
        )
    best_metric_by_tier = status.get("best_metric_by_tier", {})
    if best_metric_by_tier:
        lines.extend(["## Best By Tier"])
        for tier, value in sorted(best_metric_by_tier.items()):
            lines.append(f"- `{tier}`: {value}")
        lines.append("")
    if status.get("blocked_reason"):
        lines.extend(["## Blocked", status["blocked_reason"], ""])
    return "\n".join(lines) + "\n"


def build_context_md(exp_id: str, runs: list[dict[str, Any]]) -> str:
    lines = [f"# Context - {exp_id}", "", f"Updated: {now_iso()}", ""]
    if not runs:
        lines.extend(["No runs yet.", ""])
        return "\n".join(lines) + "\n"

    latest = runs[-5:]
    lines.append("## Recent Runs")
    for run in reversed(latest):
        interpretation = extract_markdown_section(run["result"], "Interpretation")
        next_section = extract_markdown_section(run["result"], "Next")
        lines.append(
            "- "
            + f"{run['manifest'].get('run_id')} ({run['manifest'].get('role')}, "
            + f"tier={run['manifest'].get('fidelity_tier', 'default')}, "
            + f"{run['manifest'].get('status')}, metric={run['metrics'].get('value')}): "
            + truncate(interpretation or next_section or run["result"], 180)
        )
    lines.append("")
    latest_next = extract_markdown_section(runs[-1]["result"], "Next")
    lines.extend(["## Current Baton", latest_next or "No next action recorded.", ""])
    return "\n".join(lines) + "\n"


def build_next_md(exp_id: str, latest_run: dict[str, Any] | None) -> str:
    lines = [f"# Next Action for {exp_id}", "", f"Updated: {now_iso()}", ""]
    if latest_run is None:
        lines.extend(["Begin the first bounded iteration.", ""])
        return "\n".join(lines) + "\n"

    next_section = extract_markdown_section(latest_run["result"], "Next")
    if next_section:
        lines.append(next_section)
    else:
        lines.append("Review the latest run and set a tighter next action.")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_best_md(exp_id: str, best_run: dict[str, Any] | None) -> str:
    lines = [f"# Best Result - {exp_id}", "", f"Updated: {now_iso()}", ""]
    if best_run is None:
        lines.extend(["No results yet.", ""])
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            f"Run: {best_run['manifest'].get('run_id')}",
            f"Role: {best_run['manifest'].get('role')}",
            f"Fidelity tier: {best_run['manifest'].get('fidelity_tier', 'default')}",
            f"Outcome: {best_run['manifest'].get('status')}",
            f"Metric value: {best_run['metrics'].get('value')}",
            "",
        ]
    )
    interpretation = extract_markdown_section(best_run["result"], "Interpretation")
    lines.append(interpretation or truncate(best_run["result"], 400))
    lines.append("")
    return "\n".join(lines) + "\n"


def determine_phase_after_run(
    current_phase: str,
    *,
    autonomous: bool,
    run_count: int,
    max_iterations: int,
    failure_streak: int,
) -> tuple[str, str | None]:
    if current_phase in {"paused", "completed", "failed"}:
        return current_phase, None
    if run_count >= max_iterations:
        return "completed", "Reached max iterations"
    if failure_streak >= 5:
        return "paused", "Auto-paused after 5 consecutive failures"
    if failure_streak >= 3:
        return "awaiting-human", "Repeated failures require review"
    if not autonomous:
        return "awaiting-human", "Autonomous mode disabled for this experiment"
    return "active", None


def rebuild_experiment(paths: LabPaths, exp_id: str) -> dict[str, Any]:
    exp_dir = experiment_dir(paths, exp_id)
    base_spec = load_spec(exp_dir)
    status = load_json(exp_dir / "STATUS.json", {})

    runs: list[dict[str, Any]] = []
    for run_dir in sorted((exp_dir / "runs").iterdir()):
        if not run_dir.is_dir():
            continue
        manifest = load_json(run_dir / "manifest.json", {})
        if manifest.get("status") == "running":
            continue
        metrics = load_json(run_dir / "metrics.json", {})
        result = load_text(run_dir / "RESULT.md")
        runs.append({"dir": run_dir, "manifest": manifest, "metrics": metrics, "result": result})

    metrics_lines: list[str] = []
    failure_streak = 0
    success_streak_by_tier: dict[str, int] = {}
    for run in runs:
        payload = {"run_id": run["manifest"].get("run_id"), "ts": run["manifest"].get("completed_at")}
        payload.update(run["metrics"])
        metrics_lines.append(json.dumps(payload, sort_keys=False))
        tier = str(run["manifest"].get("fidelity_tier", "default") or "default")
        if run["manifest"].get("status") == "success":
            failure_streak = 0
            success_streak_by_tier[tier] = success_streak_by_tier.get(tier, 0) + 1
        else:
            failure_streak += 1
            success_streak_by_tier[tier] = 0

    save_text(exp_dir / "metrics.jsonl", ("\n".join(metrics_lines) + "\n") if metrics_lines else "")

    latest_run = runs[-1] if runs else None
    current_tier = normalize_fidelity_tier(
        base_spec, str(status.get("current_fidelity_tier", "")).strip() or None
    )
    tiers = fidelity_tiers(base_spec)
    best_run = choose_best_run(runs, str(base_spec.get("metric_direction", "maximize")))
    best_by_tier = choose_best_run_by_tier(runs, str(base_spec.get("metric_direction", "maximize")))
    previous_tier = current_tier

    status["success_streak_by_tier"] = success_streak_by_tier
    if should_auto_promote_fidelity(status, base_spec):
        promoted = next_fidelity_tier(base_spec, current_tier)
        if promoted:
            current_tier = promoted
            emit_event(
                paths,
                "fidelity_tier_changed",
                {
                    "experiment": exp_id,
                    "from": previous_tier,
                    "to": current_tier,
                    "reason": "auto promotion after success streak",
                },
            )

    spec = resolved_spec_for_tier(base_spec, current_tier)
    run_count_by_tier = per_tier_counts(runs)
    best_metric_by_tier = {
        tier: run["metrics"].get("value")
        for tier, run in best_by_tier.items()
    }
    best_run_by_tier = {
        tier: run["manifest"].get("run_id")
        for tier, run in best_by_tier.items()
    }

    status["schema_version"] = SCHEMA_VERSION
    status["goal"] = spec.get("goal", status.get("goal", ""))
    status["metric"] = spec.get("metric", status.get("metric", ""))
    status["metric_direction"] = spec.get("metric_direction", status.get("metric_direction", "maximize"))
    status["priority"] = spec.get("priority", status.get("priority", "normal"))
    status["autonomous"] = bool(spec.get("autonomous", status.get("autonomous", True)))
    status["fidelity_tiers"] = tiers
    status["current_fidelity_tier"] = current_tier
    status["next_fidelity_tier"] = next_fidelity_tier(base_spec, current_tier)
    status["executor_class"] = str(spec.get("executor_class", status.get("executor_class", "default")) or "default")
    status["run_count_by_tier"] = run_count_by_tier
    status["best_run_by_tier"] = best_run_by_tier
    status["best_metric_by_tier"] = best_metric_by_tier
    status["run_count"] = len(runs)
    status["last_run"] = latest_run["manifest"].get("run_id") if latest_run else None
    status["last_run_at"] = latest_run["manifest"].get("completed_at") if latest_run else None
    status["last_outcome"] = latest_run["manifest"].get("status") if latest_run else None
    status["failure_streak"] = failure_streak
    status["best_run"] = best_run["manifest"].get("run_id") if best_run else None
    status["best_metric_value"] = best_run["metrics"].get("value") if best_run else None

    save_status(paths, exp_id, status)
    save_text(exp_dir / "RUNBOOK.md", build_runbook_md(exp_id, spec, status))
    save_text(exp_dir / "SUMMARY.md", build_summary_md(exp_id, spec, status, latest_run, best_run))
    save_text(exp_dir / "context.md", build_context_md(exp_id, runs))
    save_text(exp_dir / "NEXT.md", build_next_md(exp_id, latest_run))
    save_text(exp_dir / "best.md", build_best_md(exp_id, best_run))
    checkpoints_dir = exp_dir / "checkpoints"
    checkpoints_dir.mkdir(exist_ok=True)
    save_text(
        checkpoints_dir / "latest-run.txt",
        (latest_run["manifest"].get("run_id") if latest_run else "") + "\n",
    )
    save_text(
        checkpoints_dir / "best-run.txt",
        (best_run["manifest"].get("run_id") if best_run else "") + "\n",
    )
    return status


def seal_run(
    paths: LabPaths,
    exp_id: str,
    run_dir: Path,
    *,
    result_md: str,
    metrics: dict[str, Any],
    outcome: str,
) -> None:
    # RESULT.md is the write-ahead artifact. If reduction crashes afterwards,
    # the canonical run bundle still exists and the reducer can rebuild from it.
    save_text(run_dir / "RESULT.md", result_md)
    save_json(run_dir / "metrics.json", metrics)

    manifest = load_json(run_dir / "manifest.json", {})
    manifest["completed_at"] = now_iso()
    manifest["status"] = outcome
    save_json(run_dir / "manifest.json", manifest)

    emit_event(
        paths,
        "run_completed",
        {
            "experiment": exp_id,
            "run_id": run_dir.name,
            "outcome": outcome,
            "metric_value": metrics.get("value"),
            "fidelity_tier": manifest.get("fidelity_tier", "default"),
            "executor_class": manifest.get("executor_class", "default"),
        },
    )
    rebuild_experiment(paths, exp_id)


def is_due(status: dict[str, Any]) -> bool:
    due = parse_iso(status.get("next_due_at"))
    if due is None:
        return True
    return due <= now_utc()


def run_once(
    paths: LabPaths,
    max_runs: int = 3,
    *,
    allowed_executor_classes: list[str] | None = None,
) -> list[str]:
    watchdog(paths, repair=True)
    messages: list[str] = []
    for experiment in eligible_experiments(paths, allowed_executor_classes=allowed_executor_classes)[:max_runs]:
        exp_id = experiment["id"]
        exp_dir = experiment_dir(paths, exp_id)
        base_spec = load_spec(exp_dir)
        current_tier = normalize_fidelity_tier(
            base_spec, str(experiment.get("current_fidelity_tier", "")).strip() or None
        )
        spec = resolved_spec_for_tier(base_spec, current_tier)
        role_order = spec.get("worker_roles") or DEFAULT_ROLE_ORDER
        if not isinstance(role_order, list) or not role_order:
            role_order = DEFAULT_ROLE_ORDER
        role = str(role_order[experiment.get("run_count", 0) % len(role_order)])
        max_iterations = int(base_spec.get("max_iterations_total", 20))
        ttl_seconds = max(int(spec.get("time_budget_minutes", 10)) * 60 + 120, 300)

        if int(experiment.get("run_count", 0)) >= max_iterations:
            set_phase(paths, exp_id, "completed", reason=f"Reached max iterations ({max_iterations})")
            messages.append(f"completed {exp_id} (max iterations)")
            continue

        lease = acquire_lease(paths, exp_id, owner="scheduler", ttl_seconds=ttl_seconds)
        run_dir = create_run_bundle(paths, exp_id, role=role, lease=lease, spec=spec)

        try:
            result_md, metrics, outcome = run_executor(
                paths,
                exp_id,
                run_dir,
                role=role,
                spec=spec,
                lease=lease,
                iteration=int(experiment.get("run_count", 0)) + 1,
            )
        except Exception as error:
            result_md = "\n".join(
                [
                    f"# {exp_id} - Iteration {int(experiment.get('run_count', 0)) + 1}",
                    "",
                    "## Hypothesis",
                    "Run the configured bounded iteration.",
                    "",
                    "## Method",
                    f"Role: {role}",
                    "",
                    "## Result",
                    f"Unexpected executor error: {error}",
                    "",
                    "## Interpretation",
                    "The run failed before completion and needs inspection.",
                    "",
                    "## Next",
                    "Inspect the run bundle and fix the executor before retrying.",
                    "",
                ]
            )
            metrics = {
                "iteration": int(experiment.get("run_count", 0)) + 1,
                "metric": spec.get("metric", "executor_error"),
                "value": 0,
                "source": "executor",
                "error": str(error),
            }
            save_text(run_dir / "stdout.log", load_text(run_dir / "stdout.log"))
            save_text(run_dir / "stderr.log", (load_text(run_dir / "stderr.log") + f"\n{error}\n").lstrip())
            outcome = "failed"

        try:
            seal_run(paths, exp_id, run_dir, result_md=result_md, metrics=metrics, outcome=outcome)
            status = get_status(paths, exp_id)
            if status is None:
                raise RuntimeError(f"Missing status after run: {exp_id}")
            refreshed_spec = resolved_spec_for_tier(
                base_spec,
                str(status.get("current_fidelity_tier", "")).strip() or None,
            )
            next_phase, blocked_reason = determine_phase_after_run(
                status.get("phase", "queued"),
                autonomous=bool(base_spec.get("autonomous", True)),
                run_count=int(status.get("run_count", 0)),
                max_iterations=max_iterations,
                failure_streak=int(status.get("failure_streak", 0)),
            )
            status["phase"] = next_phase
            status["blocked_reason"] = blocked_reason
            status["next_due_at"] = (
                None
                if next_phase in {"completed", "paused"}
                else next_due_iso(refreshed_spec.get("cadence"))
            )
            save_status(paths, exp_id, status)
            rebuild_experiment(paths, exp_id)
            emit_event(
                paths,
                "scheduler_updated",
                {
                    "experiment": exp_id,
                    "phase": next_phase,
                    "next_due_at": status.get("next_due_at"),
                    "fidelity_tier": status.get("current_fidelity_tier", "default"),
                    "executor_class": status.get("executor_class", "default"),
                },
            )
            messages.append(
                f"ran {exp_id} [{spec.get('current_fidelity_tier', 'default')}] as {role} ({outcome})"
            )
        finally:
            release_lease(paths, exp_id)

    return messages


def queue_dispatch(
    paths: LabPaths,
    max_runs: int = 3,
    *,
    allowed_executor_classes: list[str] | None = None,
) -> list[str]:
    watchdog(paths, repair=True)
    messages: list[str] = []
    for experiment in eligible_experiments(paths, allowed_executor_classes=allowed_executor_classes)[:max_runs]:
        exp_id = str(experiment["id"])
        exp_dir = experiment_dir(paths, exp_id)
        base_spec = load_spec(exp_dir)
        current_tier = normalize_fidelity_tier(
            base_spec, str(experiment.get("current_fidelity_tier", "")).strip() or None
        )
        spec = resolved_spec_for_tier(base_spec, current_tier)
        role_order = spec.get("worker_roles") or DEFAULT_ROLE_ORDER
        if not isinstance(role_order, list) or not role_order:
            role_order = DEFAULT_ROLE_ORDER
        role = str(role_order[int(experiment.get("run_count", 0)) % len(role_order)])
        run_id = make_run_id(role)
        max_iterations = int(base_spec.get("max_iterations_total", 20))
        ttl_seconds = max(int(spec.get("time_budget_minutes", 10)) * 60 + 120, 300)
        iteration = int(experiment.get("run_count", 0)) + 1

        if int(experiment.get("run_count", 0)) >= max_iterations:
            set_phase(paths, exp_id, "completed", reason=f"Reached max iterations ({max_iterations})")
            messages.append(f"completed {exp_id} (max iterations)")
            continue

        lease = acquire_lease(paths, exp_id, owner="dispatch-queue", ttl_seconds=ttl_seconds, run_id=run_id)
        dispatch_id = f"DISPATCH-{now_ts()}-{exp_id}"
        dispatch_dir = paths.dispatch_ready / dispatch_id
        try:
            dispatch_dir.mkdir(parents=True)
            run_dir = create_run_bundle_at(
                dispatch_dir / "run",
                exp_id,
                run_id=run_id,
                role=role,
                lease=lease,
                spec=spec,
                initial_status="prepared",
            )
            save_text(run_dir / "plan.md", build_run_plan(paths, exp_id, role, spec))
            record = {
                "schema_version": SCHEMA_VERSION,
                "dispatch_id": dispatch_id,
                "experiment": exp_id,
                "run_id": run_id,
                "role": role,
                "stage": "ready",
                "queued_at": now_iso(),
                "worker": None,
                "lease_id": lease["lease_id"],
                "lease_expires_at": lease.get("expires_at"),
                "iteration": iteration,
                "fidelity_tier": str(spec.get("current_fidelity_tier", "default") or "default"),
                "executor_class": str(spec.get("executor_class", "default") or "default"),
                "canonical_run_dir": str(experiment_dir(paths, exp_id) / "runs" / run_id),
                "spec": spec,
            }
            save_json(dispatch_dir / "dispatch.json", record)
            write_dispatch_package_readme(dispatch_dir, record)
            set_current_dispatch(paths, exp_id, dispatch_view(record, dispatch_dir))
            write_lab_status(paths)
            snapshot_dispatch_inputs(paths, exp_id, dispatch_dir)
            emit_event(
                paths,
                "dispatch_enqueued",
                {
                    "dispatch_id": dispatch_id,
                    "experiment": exp_id,
                    "run_id": run_id,
                    "role": role,
                    "fidelity_tier": record["fidelity_tier"],
                    "executor_class": record["executor_class"],
                },
            )
            messages.append(
                f"queued dispatch {dispatch_id} for {exp_id} [{record['fidelity_tier']}] as {role}"
            )
        except Exception:
            if dispatch_dir.exists():
                shutil.rmtree(dispatch_dir)
            release_lease(paths, exp_id, lease_id=lease["lease_id"])
            raise

    return messages


def claim_dispatch(
    paths: LabPaths,
    max_claims: int = 1,
    *,
    worker: str = "dispatch-worker",
    allowed_executor_classes: list[str] | None = None,
) -> list[dict[str, Any]]:
    allowed = {
        str(item).strip()
        for item in (allowed_executor_classes or [])
        if str(item).strip()
    }
    claimed: list[dict[str, Any]] = []
    ready = [
        package
        for package in list_dispatch_packages(paths, stage="ready")
        if not allowed
        or str(package["record"].get("executor_class", "default") or "default") in allowed
    ]
    for package in ready[:max_claims]:
        record = dict(package["record"])
        source_dir = package["dir"]
        target_dir = paths.dispatch_running / str(record["dispatch_id"])
        claimed_at = now_iso()

        manifest_path = source_dir / "run" / "manifest.json"
        manifest = load_json(manifest_path, {})
        manifest["status"] = "running"
        manifest["claimed_at"] = claimed_at
        save_json(manifest_path, manifest)

        record["stage"] = "running"
        record["worker"] = worker
        record["claimed_at"] = claimed_at
        save_json(source_dir / "dispatch.json", record)
        source_dir.rename(target_dir)

        set_current_dispatch(paths, str(record["experiment"]), dispatch_view(record, target_dir))
        emit_event(
            paths,
            "dispatch_claimed",
            {
                "dispatch_id": record["dispatch_id"],
                "experiment": record["experiment"],
                "worker": worker,
            },
        )
        claimed.append({"dir": target_dir, "record": record})
    return claimed


def mark_dispatch_complete(
    paths: LabPaths,
    dispatch_id: str,
    *,
    outcome: str,
    worker: str | None = None,
) -> dict[str, Any]:
    package = find_dispatch_package(paths, dispatch_id)
    if package["stage_dir"] != "running":
        raise ValueError(f"Dispatch package is not running: {dispatch_id}")

    dispatch_dir = package["dir"]
    record = dict(package["record"])
    run_dir = dispatch_dir / "run"
    result_path = run_dir / "RESULT.md"
    metrics_path = run_dir / "metrics.json"
    if not result_path.exists():
        raise RuntimeError(f"Dispatch package is missing RESULT.md: {dispatch_id}")
    if not metrics_path.exists():
        raise RuntimeError(f"Dispatch package is missing metrics.json: {dispatch_id}")

    completed_at = now_iso()
    manifest_path = run_dir / "manifest.json"
    manifest = load_json(manifest_path, {})
    manifest["status"] = outcome
    manifest["completed_at"] = completed_at
    save_json(manifest_path, manifest)

    metrics = load_json(metrics_path, {})
    record["stage"] = "complete"
    record["completed_at"] = completed_at
    record["last_outcome"] = outcome
    record["metric_value"] = metrics.get("value")
    if worker:
        record["worker"] = worker
    save_json(dispatch_dir / "dispatch.json", record)

    target_dir = paths.dispatch_complete / dispatch_id
    dispatch_dir.rename(target_dir)
    set_current_dispatch(paths, str(record["experiment"]), dispatch_view(record, target_dir))
    emit_event(
        paths,
        "dispatch_completed",
        {
            "dispatch_id": dispatch_id,
            "experiment": record["experiment"],
            "outcome": outcome,
            "metric_value": metrics.get("value"),
        },
    )
    return record


def dispatch_work(
    paths: LabPaths,
    max_runs: int = 1,
    *,
    worker: str = "dispatch-worker",
    allowed_executor_classes: list[str] | None = None,
) -> list[str]:
    messages: list[str] = []
    claimed = claim_dispatch(
        paths,
        max_claims=max_runs,
        worker=worker,
        allowed_executor_classes=allowed_executor_classes,
    )
    for package in claimed:
        dispatch_dir = package["dir"]
        record = dict(package["record"])
        exp_id = str(record["experiment"])
        spec = dict(record.get("spec", {}))
        iteration = int(record.get("iteration", 1))
        role = str(record.get("role", "researcher"))
        lease = {"lease_id": str(record.get("lease_id"))}
        run_dir = dispatch_dir / "run"
        inputs = dispatch_dir / "input"

        try:
            result_md, metrics, outcome = run_executor(
                paths,
                exp_id,
                run_dir,
                role=role,
                spec=spec,
                lease=lease,
                iteration=iteration,
                env_overrides={
                    "LAB_SPEC_PATH": str(inputs / "SPEC.yaml"),
                    "LAB_SUMMARY_PATH": str(inputs / "SUMMARY.md"),
                    "LAB_NEXT_PATH": str(inputs / "NEXT.md"),
                    "LAB_CONTEXT_PATH": str(inputs / "context.md"),
                    "LAB_PROGRAM_PATH": str(inputs / "PROGRAM.md"),
                    "LAB_DISPATCH_ID": str(record.get("dispatch_id")),
                    "LAB_DISPATCH_DIR": str(dispatch_dir),
                    "LAB_DISPATCH_STAGE": "running",
                },
            )
        except Exception as error:
            result_md = "\n".join(
                [
                    f"# {exp_id} - Iteration {iteration}",
                    "",
                    "## Hypothesis",
                    "Run the configured bounded iteration through the dispatch worker.",
                    "",
                    "## Method",
                    f"Role: {role}",
                    "",
                    "## Result",
                    f"Unexpected executor error: {error}",
                    "",
                    "## Interpretation",
                    "The dispatched run failed before completion and needs inspection.",
                    "",
                    "## Next",
                    "Inspect the dispatch package and fix the executor before retrying.",
                    "",
                ]
            )
            metrics = {
                "iteration": iteration,
                "metric": spec.get("metric", "executor_error"),
                "value": 0,
                "source": "dispatch-executor",
                "error": str(error),
            }
            save_text(run_dir / "stdout.log", load_text(run_dir / "stdout.log"))
            save_text(run_dir / "stderr.log", (load_text(run_dir / "stderr.log") + f"\n{error}\n").lstrip())
            outcome = "failed"

        save_text(run_dir / "RESULT.md", result_md)
        save_json(run_dir / "metrics.json", metrics)
        completed = mark_dispatch_complete(
            paths,
            str(record["dispatch_id"]),
            outcome=outcome,
            worker=worker,
        )
        messages.append(
            f"completed dispatch {completed['dispatch_id']} for {exp_id} [{completed.get('fidelity_tier', 'default')}] ({outcome})"
        )
    return messages


def ingest_dispatch(
    paths: LabPaths,
    max_runs: int = 3,
    *,
    dispatch_ids: list[str] | None = None,
) -> list[str]:
    selected_ids = {str(item).strip() for item in (dispatch_ids or []) if str(item).strip()}
    candidates = [
        package
        for package in list_dispatch_packages(paths, stage="complete")
        if not package["record"].get("ingested_at")
        and (
            not selected_ids
            or str(package["record"].get("dispatch_id")) in selected_ids
        )
    ]

    messages: list[str] = []
    for package in candidates[:max_runs]:
        dispatch_dir = package["dir"]
        record = dict(package["record"])
        exp_id = str(record["experiment"])
        run_id = str(record["run_id"])
        run_dir = dispatch_dir / "run"
        canonical_run_dir = experiment_dir(paths, exp_id) / "runs" / run_id
        if canonical_run_dir.exists():
            raise RuntimeError(f"Canonical run already exists for {exp_id}: {run_id}")

        shutil.copytree(run_dir, canonical_run_dir)
        manifest = load_json(canonical_run_dir / "manifest.json", {})
        metrics = load_json(canonical_run_dir / "metrics.json", {})
        emit_event(
            paths,
            "run_completed",
            {
                "experiment": exp_id,
                "run_id": run_id,
                "outcome": manifest.get("status"),
                "metric_value": metrics.get("value"),
                "fidelity_tier": manifest.get("fidelity_tier", "default"),
                "executor_class": manifest.get("executor_class", "default"),
                "source": "dispatch-ingest",
            },
        )

        base_spec = load_spec(experiment_dir(paths, exp_id))
        rebuild_experiment(paths, exp_id)
        status = get_status(paths, exp_id)
        if status is None:
            raise RuntimeError(f"Missing status after dispatch ingest: {exp_id}")
        refreshed_spec = resolved_spec_for_tier(
            base_spec,
            str(status.get("current_fidelity_tier", "")).strip() or None,
        )
        max_iterations = int(base_spec.get("max_iterations_total", 20))
        next_phase, blocked_reason = determine_phase_after_run(
            status.get("phase", "queued"),
            autonomous=bool(base_spec.get("autonomous", True)),
            run_count=int(status.get("run_count", 0)),
            max_iterations=max_iterations,
            failure_streak=int(status.get("failure_streak", 0)),
        )
        status["phase"] = next_phase
        status["blocked_reason"] = blocked_reason
        status["current_dispatch"] = None
        status["next_due_at"] = (
            None
            if next_phase in {"completed", "paused"}
            else next_due_iso(refreshed_spec.get("cadence"))
        )
        save_status(paths, exp_id, status)
        rebuild_experiment(paths, exp_id)
        release_lease(paths, exp_id, lease_id=str(record.get("lease_id") or ""))
        emit_event(
            paths,
            "scheduler_updated",
            {
                "experiment": exp_id,
                "phase": next_phase,
                "next_due_at": status.get("next_due_at"),
                "fidelity_tier": status.get("current_fidelity_tier", "default"),
                "executor_class": status.get("executor_class", "default"),
                "source": "dispatch-ingest",
            },
        )

        ingested_at = now_iso()
        record["stage"] = "ingested"
        record["ingested_at"] = ingested_at
        record["canonical_run_dir"] = str(canonical_run_dir)
        save_json(dispatch_dir / "dispatch.json", record)
        save_text(dispatch_dir / "INGESTED.txt", str(canonical_run_dir) + "\n")
        emit_event(
            paths,
            "dispatch_ingested",
            {
                "dispatch_id": record["dispatch_id"],
                "experiment": exp_id,
                "run_id": run_id,
                "canonical_run_dir": str(canonical_run_dir),
            },
        )
        messages.append(f"ingested dispatch {record['dispatch_id']} into {exp_id}/{run_id}")

    return messages


# ---------------------------------------------------------------------------
# Agent dispatch: dispatch-agent-next and dispatch-agent-submit
# ---------------------------------------------------------------------------


def dispatch_agent_next(
    paths: LabPaths,
    *,
    worker: str = "dispatch-agent",
    allowed_executor_classes: list[str] | None = None,
    experiment_id: str | None = None,
) -> dict[str, Any] | None:
    """Prepare + claim one dispatch package and return a JSON-ready context dict.

    Combines queue_dispatch + claim_dispatch into a single call.  Returns
    ``None`` when no eligible experiment is available.

    If ``experiment_id`` is given, only that experiment is eligible.
    """
    queue_dispatch(paths, max_runs=1, allowed_executor_classes=allowed_executor_classes)
    claimed = claim_dispatch(
        paths,
        max_claims=1,
        worker=worker,
        allowed_executor_classes=allowed_executor_classes,
    )
    if not claimed:
        return None

    package = claimed[0]
    record = dict(package["record"])
    dispatch_dir = package["dir"]
    spec = dict(record.get("spec", {}))
    exp_id = str(record["experiment"])

    # Filter by experiment_id if specified
    if experiment_id and exp_id != experiment_id:
        # Wrong experiment claimed — release and return None
        try:
            from lab.core import mark_dispatch_complete
            mark_dispatch_complete(paths, str(record["dispatch_id"]), outcome="skipped", worker=worker)
        except Exception:
            pass
        return None
    status = get_status(paths, exp_id) or {}
    inputs_dir = dispatch_dir / "input"

    # Gather input file contents
    input_files: dict[str, str] = {}
    for name in [
        "SPEC.yaml", "SUMMARY.md", "NEXT.md", "context.md",
        "PROGRAM.md", "RUNBOOK.md", "best.md", "LAB-STATUS.md",
    ]:
        path = inputs_dir / name
        if path.exists():
            input_files[name] = path.read_text()
    # plan.md is inside run/
    plan_path = dispatch_dir / "run" / "plan.md"
    if plan_path.exists():
        input_files["plan.md"] = plan_path.read_text()

    # Read current content of mutable files
    workspace_root = str(spec.get("workspace_root", ""))
    mutable_paths = spec_list(spec, "mutable_paths")
    current_files: dict[str, str] = {}
    if workspace_root:
        ws = Path(workspace_root).expanduser().resolve()
        for rel_path in mutable_paths:
            full = ws / rel_path
            if full.exists():
                try:
                    current_files[rel_path] = full.read_text()
                except Exception:
                    pass

    # Determine mutation brief path
    agent_instruction_file = str(spec.get("agent_instruction_file", "") or "")
    mutation_brief_path = ""
    if agent_instruction_file and workspace_root:
        candidate = Path(workspace_root).expanduser().resolve() / agent_instruction_file
        if candidate.exists():
            mutation_brief_path = str(candidate)

    best_metric_value = status.get("best_metric_value")

    return {
        "dispatch_id": record["dispatch_id"],
        "experiment": exp_id,
        "package_dir": str(dispatch_dir),
        "workspace_root": workspace_root,
        "mutable_paths": mutable_paths,
        "read_only_paths": spec_list(spec, "read_only_paths"),
        "validation_command": str(spec.get("validation_command", "")),
        "mutation_brief_path": mutation_brief_path,
        "metric": str(spec.get("metric", "")),
        "metric_direction": str(spec.get("metric_direction", "maximize")),
        "best_metric_value": best_metric_value,
        "iteration": int(record.get("iteration", 1)),
        "role": str(record.get("role", "researcher")),
        "run_id": str(record.get("run_id", "")),
        "fidelity_tier": str(record.get("fidelity_tier", "default")),
        "time_budget_minutes": int(spec.get("time_budget_minutes", 10)),
        "input_files": input_files,
        "current_files": current_files,
    }


def dispatch_agent_submit(
    paths: LabPaths,
    dispatch_id: str,
    changes: dict[str, str],
    *,
    reasoning: str = "",
    worker: str = "dispatch-agent",
) -> dict[str, Any]:
    """Apply agent file changes, validate, score, complete, and ingest a dispatch.

    This is the agent-side counterpart to ``dispatch_agent_next``.  It:

    1. Locates the running dispatch package.
    2. Creates a git-clone sandbox of workspace_root.
    3. Writes the agent-provided file changes into the sandbox.
    4. Runs the validation_command inside the sandbox.
    5. Parses metrics, compares to best-so-far.
    6. Writes RESULT.md, metrics.json, decision artifacts.
    7. Calls mark_dispatch_complete + ingest_dispatch.

    Returns a summary dict with outcome details.
    """
    package = find_dispatch_package(paths, dispatch_id)
    if package["stage_dir"] != "running":
        raise ValueError(f"Dispatch package is not running: {dispatch_id}")

    dispatch_dir = package["dir"]
    record = dict(package["record"])
    spec = dict(record.get("spec", {}))
    exp_id = str(record["experiment"])
    iteration = int(record.get("iteration", 1))
    status = get_status(paths, exp_id) or {}

    workspace_root_raw = str(spec.get("workspace_root", ""))
    if not workspace_root_raw:
        raise RuntimeError(f"No workspace_root in spec for {exp_id}")
    workspace_root = Path(workspace_root_raw).expanduser().resolve()

    validation_command = str(spec.get("validation_command", "")).strip()
    if not validation_command:
        raise RuntimeError(f"No validation_command in spec for {exp_id}")

    metric_name = str(spec.get("metric", "metric"))
    metric_direction = str(spec.get("metric_direction", "maximize")).strip().lower() or "maximize"
    previous_best = status.get("best_metric_value")
    promotion_strategy = str(spec.get("promotion_strategy", "patch-only")).strip() or "patch-only"

    run_dir = dispatch_dir / "run"
    artifacts_dir = run_dir / "artifacts"
    commands_dir = artifacts_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    # 1. Clone workspace into sandbox
    sandbox_root = artifacts_dir / "workspace"
    _use_git = shutil.which("git") is not None and _is_git_repo(workspace_root)
    if _use_git:
        _clone_git_workspace(workspace_root, sandbox_root)
    else:
        shutil.copytree(workspace_root, sandbox_root)

    # 2. Apply agent changes
    for rel_path, content in changes.items():
        target = sandbox_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    # 3. Save reasoning
    if reasoning:
        save_text(artifacts_dir / "agent_reasoning.md", reasoning)

    # 4. Run validation_command
    cmd_env = os.environ.copy()
    cmd_env.update({
        "LAB_EFFECTIVE_WORKSPACE_ROOT": str(sandbox_root),
        "LAB_ORIGINAL_WORKSPACE_ROOT": str(workspace_root),
        "LAB_RUN_ARTIFACTS_DIR": str(artifacts_dir),
        "LAB_EXPERIMENT_ID": exp_id,
        "LAB_RUN_ITERATION": str(iteration),
        "LAB_PRIMARY_METRIC": metric_name,
        "LAB_METRIC_DIRECTION": metric_direction,
        "LAB_WORKSPACE_ROOT": str(workspace_root),
        "LAB_RUN_DIR": str(run_dir),
        "LAB_DISPATCH_ID": dispatch_id,
        "LAB_DISPATCH_DIR": str(dispatch_dir),
    })

    timeout_seconds = int(spec.get("time_budget_minutes", 10)) * 60
    try:
        completed = subprocess.run(
            resolve_command_tokens(validation_command, base_dir=workspace_root),
            cwd=sandbox_root,
            env=cmd_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        save_text(commands_dir / "validation.stdout.log", completed.stdout)
        save_text(commands_dir / "validation.stderr.log", completed.stderr)
    except subprocess.TimeoutExpired as err:
        save_text(commands_dir / "validation.stdout.log", err.stdout or "")
        save_text(commands_dir / "validation.stderr.log", err.stderr or "")
        raise RuntimeError(f"Validation command timed out after {timeout_seconds}s") from err

    if completed.returncode != 0:
        raise RuntimeError(
            f"Validation command failed (rc={completed.returncode}): "
            + (completed.stderr.strip()[:500] or "(no stderr)")
        )

    # 5. Parse metric output
    from scripts.reference_executor import parse_metric_output, compare, build_result_md

    candidate_metric = parse_metric_output(completed.stdout, metric_name)
    candidate_value = float(candidate_metric["value"])

    # 6. Compare to best-so-far
    reference_source = "none"
    reference_value: float | None = None
    if previous_best is not None:
        reference_source = "best-so-far"
        reference_value = float(previous_best)

    if reference_value is None:
        accepted = True
    else:
        accepted = compare(candidate_value, reference_value, metric_direction)

    # 7. Git patch artifacts
    changed_files: list[str] = []
    applied_to_original = False
    workspace_mode = "git-clone" if _use_git else "copy"

    if _use_git:
        from scripts.reference_executor import git_patch
        patch_path = artifacts_dir / "diff.patch"
        diffstat_path = artifacts_dir / "diffstat.txt"
        changed_files_path = artifacts_dir / "changed-files.txt"
        changed_files = git_patch(sandbox_root, patch_path, diffstat_path, changed_files_path)

    # 8. Decision artifact
    decision = {
        "accepted": accepted,
        "promotion_strategy": promotion_strategy,
        "workspace_mode": workspace_mode,
        "reference_source": reference_source,
        "baseline_value": None,
        "previous_best_value": previous_best,
        "candidate_value": candidate_value,
        "applied_to_original": applied_to_original,
        "changed_files": changed_files,
        "agent_reasoning": reasoning[:500] if reasoning else "",
    }
    save_json(artifacts_dir / "decision.json", decision)

    # 9. Build RESULT.md
    mutation_label = "dispatch-agent (external)"
    result_md = build_result_md(
        experiment_id=exp_id,
        iteration=str(iteration),
        baseline_value=None,
        previous_best=previous_best,
        candidate_value=candidate_value,
        accepted=accepted,
        promotion_strategy=promotion_strategy,
        workspace_mode=workspace_mode,
        applied=applied_to_original,
        changed_files=changed_files,
        mutation_command=mutation_label,
        validation_command=validation_command,
        reference_source=reference_source,
    )

    # 10. Build metrics.json
    metrics = {
        "iteration": iteration,
        "metric": candidate_metric.get("metric", metric_name),
        "value": candidate_value,
        "accepted": accepted,
        "baseline_value": None,
        "previous_best_value": previous_best,
        "reference_source": reference_source,
        "promotion_strategy": promotion_strategy,
        "workspace_mode": workspace_mode,
        "applied_to_original": applied_to_original,
        "changed_file_count": len(changed_files),
        "source": "dispatch-agent",
    }

    save_text(run_dir / "RESULT.md", result_md)
    save_json(run_dir / "metrics.json", metrics)

    # 11. Complete + ingest
    outcome = "success"
    mark_dispatch_complete(paths, dispatch_id, outcome=outcome, worker=worker)
    ingested = ingest_dispatch(paths, dispatch_ids=[dispatch_id], max_runs=1)

    return {
        "dispatch_id": dispatch_id,
        "experiment": exp_id,
        "outcome": outcome,
        "accepted": accepted,
        "candidate_value": candidate_value,
        "previous_best_value": previous_best,
        "reference_source": reference_source,
        "changed_files": changed_files,
        "ingested": len(ingested) > 0,
    }


def _is_git_repo(path: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _clone_git_workspace(src: Path, dst: Path) -> None:
    completed = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(src), str(dst)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git clone failed: {completed.stderr.strip()}")


# ---------------------------------------------------------------------------
# Recovery, watchdog, and derived views
# ---------------------------------------------------------------------------


def recover_lab(paths: LabPaths) -> list[str]:
    repaired: list[str] = []
    for experiment in list_experiments(paths):
        exp_id = experiment["id"]
        rebuilt = rebuild_experiment(paths, exp_id)
        repaired.append(f"reduced {exp_id} ({rebuilt.get('run_count', 0)} runs)")
    write_lab_status(paths)
    return repaired


def watchdog(paths: LabPaths, *, repair: bool = False) -> dict[str, Any]:
    alerts: list[str] = []
    reclaimed: list[str] = []

    total, used, free = shutil.disk_usage(paths.root)
    free_gb = round(free / (1024**3), 2)
    if free_gb < 5:
        alerts.append(f"Low disk space: {free_gb} GB free")

    for lock_dir in sorted(paths.locks.glob("*.lock")):
        info = load_json(lock_dir / "info.json", None)
        if info and is_lease_expired(info):
            experiment = str(info.get("experiment"))
            alerts.append(f"Expired lease: {experiment}")
            if repair:
                clear_lease(paths, experiment, event_type="lease_reclaimed", reason="watchdog expired lease")
                reclaimed.append(experiment)

    status_mtime = paths.lab_status_md.stat().st_mtime if paths.lab_status_md.exists() else 0
    status_age = now_utc().timestamp() - status_mtime if status_mtime else None
    if status_age is None:
        alerts.append("LAB-STATUS.md missing")
    elif status_age > 7200:
        alerts.append("LAB-STATUS.md is stale")
        if repair:
            write_lab_status(paths)

    report = {
        "schema_version": SCHEMA_VERSION,
        "ts": now_iso(),
        "free_gb": free_gb,
        "alerts": alerts,
        "reclaimed_leases": reclaimed,
    }
    if repair and (alerts or reclaimed):
        emit_event(paths, "watchdog_ran", report)
    return report


def headline_for_experiment(status: dict[str, Any]) -> str:
    best_value = status.get("best_metric_value")
    tier = status.get("current_fidelity_tier", "default")
    if best_value is not None:
        return f"tier={tier}, best={best_value}"
    if status.get("last_outcome"):
        return f"tier={tier}, last={status.get('last_outcome')}"
    return f"tier={tier}, no runs yet"


def generate_lab_status(paths: LabPaths) -> str:
    experiments = list_experiments(paths)
    watchdog_report = watchdog(paths, repair=False)
    dispatch_packages = list_dispatch_packages(paths)

    active = [item for item in experiments if item.get("phase") == "active"]
    queued = [item for item in experiments if item.get("phase") in {"queued", "ready"}]
    awaiting_human = [item for item in experiments if item.get("phase") == "awaiting-human"]
    paused = [item for item in experiments if item.get("phase") == "paused"]
    completed = [item for item in experiments if item.get("phase") == "completed"]

    lines = [
        "# Hermes Lab Status",
        f"Updated: {now_iso()}",
        f"Data root: {paths.root}",
        "",
    ]

    def add_section(title: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        lines.append(f"## {title} ({len(items)})")
        for item in items:
            lines.append(
                "- "
                + f"`{item['id']}` - runs={item.get('run_count', 0)} - "
                + f"class={item.get('executor_class', 'default')} - "
                + f"{headline_for_experiment(item)} - {item.get('goal', '')}"
            )
        lines.append("")

    add_section("Active", active)
    add_section("Queued", queued)
    add_section("Awaiting Human", awaiting_human)
    add_section("Paused", paused)
    add_section("Completed", completed)

    if not experiments:
        lines.extend(
            [
                "No experiments yet. Create one with `python3 scripts/labctl.py create <spec.yaml>`.",
                "",
            ]
        )

    lines.append("## Next Actions")
    next_candidates = active + queued + awaiting_human
    if next_candidates:
        for item in next_candidates[:5]:
            next_path = experiment_dir(paths, item["id"]) / "NEXT.md"
            next_line = first_content_line(load_text(next_path), "Review the baton.")
            lines.append(f"- `{item['id']}`: {truncate(next_line, 120)}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Alerts")
    alerts = list(watchdog_report["alerts"])
    alerts.extend(
        [
            f"`{item['id']}` has failure streak {item.get('failure_streak', 0)}"
            for item in experiments
            if int(item.get("failure_streak", 0)) >= 3
        ]
    )
    if alerts:
        for alert in alerts:
            lines.append(f"- {alert}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Dispatch")
    if dispatch_packages:
        counts_by_stage: dict[str, int] = {}
        for package in dispatch_packages:
            stage = str(package["record"].get("stage", package["stage_dir"]))
            counts_by_stage[stage] = counts_by_stage.get(stage, 0) + 1
        lines.append(
            "- "
            + ", ".join(
                [
                    f"{stage}={count}"
                    for stage, count in sorted(counts_by_stage.items())
                ]
            )
        )
        for package in dispatch_packages[:5]:
            record = package["record"]
            lines.append(
                "- "
                + f"`{record.get('dispatch_id')}` - exp={record.get('experiment')} - "
                + f"stage={record.get('stage', package['stage_dir'])} - "
                + f"class={record.get('executor_class', 'default')} - "
                + f"tier={record.get('fidelity_tier', 'default')}"
            )
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Health")
    lines.append(f"- Free space: {watchdog_report['free_gb']} GB")
    lines.append(f"- Event ledger: {paths.events_jsonl}")
    lines.append("")

    return "\n".join(lines)


def generate_lab_index(paths: LabPaths) -> dict[str, Any]:
    experiments = sorted(list_experiments(paths), key=lambda item: item.get("id", ""))
    watchdog_report = watchdog(paths, repair=False)
    dispatch_packages = list_dispatch_packages(paths)
    counts_by_phase: dict[str, int] = {}
    for experiment in experiments:
        phase = str(experiment.get("phase", "unknown"))
        counts_by_phase[phase] = counts_by_phase.get(phase, 0) + 1
    dispatch_counts_by_stage: dict[str, int] = {}
    dispatch_index: list[dict[str, Any]] = []
    for package in dispatch_packages:
        stage = str(package["record"].get("stage", package["stage_dir"]))
        dispatch_counts_by_stage[stage] = dispatch_counts_by_stage.get(stage, 0) + 1
        dispatch_index.append(
            {
                "dispatch_id": package["record"].get("dispatch_id"),
                "experiment": package["record"].get("experiment"),
                "run_id": package["record"].get("run_id"),
                "role": package["record"].get("role"),
                "stage": stage,
                "worker": package["record"].get("worker"),
                "fidelity_tier": package["record"].get("fidelity_tier", "default"),
                "executor_class": package["record"].get("executor_class", "default"),
                "queued_at": package["record"].get("queued_at"),
                "claimed_at": package["record"].get("claimed_at"),
                "completed_at": package["record"].get("completed_at"),
                "ingested_at": package["record"].get("ingested_at"),
                "metric_value": package["record"].get("metric_value"),
                "path": str(package["dir"]),
            }
        )

    index_experiments: list[dict[str, Any]] = []
    for experiment in experiments:
        index_experiments.append(
            {
                "id": experiment.get("id"),
                "goal": experiment.get("goal"),
                "phase": experiment.get("phase"),
                "priority": experiment.get("priority"),
                "autonomous": experiment.get("autonomous"),
                "current_fidelity_tier": experiment.get("current_fidelity_tier", "default"),
                "next_fidelity_tier": experiment.get("next_fidelity_tier"),
                "fidelity_tiers": experiment.get("fidelity_tiers", ["default"]),
                "executor_class": experiment.get("executor_class", "default"),
                "metric": experiment.get("metric"),
                "metric_direction": experiment.get("metric_direction"),
                "run_count": experiment.get("run_count", 0),
                "run_count_by_tier": experiment.get("run_count_by_tier", {}),
                "failure_streak": experiment.get("failure_streak", 0),
                "success_streak_by_tier": experiment.get("success_streak_by_tier", {}),
                "best_run": experiment.get("best_run"),
                "best_metric_value": experiment.get("best_metric_value"),
                "best_run_by_tier": experiment.get("best_run_by_tier", {}),
                "best_metric_by_tier": experiment.get("best_metric_by_tier", {}),
                "last_run": experiment.get("last_run"),
                "last_run_at": experiment.get("last_run_at"),
                "last_outcome": experiment.get("last_outcome"),
                "next_due_at": experiment.get("next_due_at"),
                "blocked_reason": experiment.get("blocked_reason"),
                "current_lease": experiment.get("current_lease"),
                "current_dispatch": experiment.get("current_dispatch"),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "data_root": str(paths.root),
        "alerts": watchdog_report["alerts"],
        "free_gb": watchdog_report["free_gb"],
        "counts_by_phase": counts_by_phase,
        "dispatch": {
            "counts_by_stage": dispatch_counts_by_stage,
            "packages": dispatch_index,
        },
        "experiments": index_experiments,
    }


def write_lab_status(paths: LabPaths) -> Path:
    save_text(paths.lab_status_md, generate_lab_status(paths) + "\n")
    save_json(paths.lab_index_json, generate_lab_index(paths))
    return paths.lab_status_md


def write_digest(paths: LabPaths) -> Path:
    experiments = list_experiments(paths)
    watchdog_report = watchdog(paths, repair=False)
    today = datetime.now().strftime("%Y-%m-%d")
    out = paths.digests_daily / f"{today}.md"

    lines = [
        f"# Hermes Lab - {today}",
        f"Generated: {now_iso()}",
        "",
        "## Headlines",
    ]
    if not experiments:
        lines.append("- No experiments are registered yet.")
    else:
        top = sorted(experiments, key=lambda item: DEFAULT_PRIORITY_ORDER.get(item.get("priority", "normal"), 2))
        for item in top[:5]:
            lines.append(
                "- "
                + f"`{item['id']}` - {item.get('phase')} - runs={item.get('run_count', 0)} - "
                + headline_for_experiment(item)
            )

    lines.extend(["", "## Details"])
    if not experiments:
        lines.append("- None")
    else:
        for item in sorted(experiments, key=lambda record: record.get("id", "")):
            summary = load_text(experiment_dir(paths, item["id"]) / "SUMMARY.md", "")
            lines.append(f"- `{item['id']}`: {truncate(summary, 200)}")

    lines.extend(["", "## Decisions Needed"])
    needs_decision = [
        item
        for item in experiments
        if item.get("phase") in {"awaiting-human", "paused"} or int(item.get("failure_streak", 0)) >= 3
    ]
    if needs_decision:
        for item in needs_decision:
            reason = item.get("blocked_reason") or "Needs review"
            lines.append(f"- `{item['id']}`: {item.get('phase')} - {reason}")
    else:
        lines.append("- None")

    lines.extend(["", "## Lab Health", f"- Free space: {watchdog_report['free_gb']} GB"])
    if watchdog_report["alerts"]:
        for alert in watchdog_report["alerts"]:
            lines.append(f"- Alert: {alert}")
    else:
        lines.append("- Alerts: none")

    save_text(out, "\n".join(lines) + "\n")
    save_text(paths.control_outbox / f"{today}.md", "\n".join(lines[:10]) + "\n")
    return out


def write_weekly_digest(paths: LabPaths) -> Path:
    current = datetime.now()
    year, week, _ = current.isocalendar()
    out = paths.digests_weekly / f"{year}-W{week:02d}.md"
    daily = sorted(paths.digests_daily.glob(f"{year}-*.md"))
    lines = [
        f"# Hermes Lab Weekly Digest - {year}-W{week:02d}",
        f"Generated: {now_iso()}",
        "",
        "## Daily Files Included",
    ]
    current_week_docs = []
    for path in daily:
        try:
            doc_date = datetime.strptime(path.stem, "%Y-%m-%d")
        except ValueError:
            continue
        y, w, _ = doc_date.isocalendar()
        if y == year and w == week:
            current_week_docs.append(path)
    if current_week_docs:
        for path in current_week_docs:
            lines.append(f"- {path.name}")
    else:
        lines.append("- No daily digests for this week yet.")
    lines.extend(["", "## Current Experiment Snapshot"])
    for item in sorted(list_experiments(paths), key=lambda record: record.get("id", "")):
        lines.append(
            "- "
            + f"`{item['id']}` - {item.get('phase')} - runs={item.get('run_count', 0)} - "
            + headline_for_experiment(item)
        )
    save_text(out, "\n".join(lines) + "\n")
    return out
