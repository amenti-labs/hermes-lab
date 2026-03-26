"""Hermes Lab Telegram command handler.

Parses natural language lab commands from Telegram messages.
Designed to be called by the Hermes agent when it detects lab-related requests.

Usage in agent context:
    from lab.telegram_commands import handle_lab_command
    result = handle_lab_command("lab status")
    # Returns formatted text ready for Telegram
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure lab is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lab.core import (
    get_paths,
    get_status,
    save_status,
    list_experiments,
    set_phase,
    load_spec,
    experiment_dir,
    watchdog,
    write_lab_status,
    clear_lease,
    mark_dispatch_complete,
    LabPaths,
)

DATA_ROOT = os.environ.get(
    "HERMES_LAB_DATA_ROOT",
    "./lab-data",
)


def _get_paths() -> LabPaths:
    return get_paths(DATA_ROOT)


def _fmt_score(val: Any) -> str:
    if val is None:
        return "none"
    try:
        return f"{float(val):.6f}"
    except (ValueError, TypeError):
        return str(val)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_status() -> str:
    """Full lab status dashboard."""
    paths = _get_paths()
    experiments = list_experiments(paths)
    health = watchdog(paths, repair=False)

    if not experiments:
        return "No experiments in the lab."

    lines = ["Hermes Lab Status", ""]

    active = [e for e in experiments if e.get("phase") in ("active", "queued")]
    completed = [e for e in experiments if e.get("phase") == "completed"]
    paused = [e for e in experiments if e.get("phase") == "paused"]

    if active:
        lines.append(f"Active ({len(active)}):")
        for e in active:
            eid = e.get("id", "?")
            phase = e.get("phase", "?")
            runs = e.get("run_count", 0)
            best = _fmt_score(e.get("best_metric_value"))
            streak = e.get("failure_streak", 0)
            dispatch = "dispatched" if e.get("current_dispatch") else "idle"
            status_icon = "!" if streak > 0 else ""
            tags = e.get("tags", [])
            runtime = e.get("estimated_runtime_minutes", 0)
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            runtime_str = f" ~{runtime}min/run" if runtime else ""
            lines.append(f"  {status_icon}{eid}{tag_str}")
            lines.append(f"    {phase} | {runs} runs | best={best} | {dispatch}{runtime_str}")
            if streak > 0:
                lines.append(f"    (!) {streak} consecutive failures")
        lines.append("")

    if paused:
        lines.append(f"Paused ({len(paused)}):")
        for e in paused:
            lines.append(f"  {e.get('id')} ({e.get('run_count', 0)} runs)")
        lines.append("")

    if completed:
        lines.append(f"Completed ({len(completed)}):")
        for e in completed:
            lines.append(f"  {e.get('id')} | {e.get('run_count', 0)} runs | best={_fmt_score(e.get('best_metric_value'))}")
        lines.append("")

    lines.append(f"Disk: {health['free_gb']:.1f} GB free")
    if health["alerts"]:
        lines.append("Alerts:")
        for a in health["alerts"]:
            lines.append(f"  (!) {a}")

    return "\n".join(lines)


def cmd_list() -> str:
    """List all experiments."""
    paths = _get_paths()
    experiments = list_experiments(paths)
    if not experiments:
        return "No experiments."
    lines = ["Experiments:", ""]
    for e in sorted(experiments, key=lambda x: x.get("id", "")):
        eid = e.get("id", "?")
        phase = e.get("phase", "?")
        runs = e.get("run_count", 0)
        best = _fmt_score(e.get("best_metric_value"))
        lines.append(f"  {eid}: {phase}, {runs} runs, best={best}")
    return "\n".join(lines)


def cmd_detail(exp_id: str) -> str:
    """Detailed view of one experiment."""
    paths = _get_paths()
    status = get_status(paths, exp_id)
    if status is None:
        return f"Experiment not found: {exp_id}"

    exp_dir = experiment_dir(paths, exp_id)
    summary_path = exp_dir / "SUMMARY.md"
    next_path = exp_dir / "NEXT.md"

    summary_text = summary_path.read_text()[:1000] if summary_path.exists() else "(no summary)"
    next_text = next_path.read_text()[:500] if next_path.exists() else "(no next steps)"

    tags = status.get("tags", [])
    parent = status.get("parent_experiment", "")
    runtime = status.get("estimated_runtime_minutes", 0)
    notify = status.get("notify", "silent")
    known_good = status.get("known_good_config", "")

    lines = [
        f"Experiment: {exp_id}",
        f"Phase: {status.get('phase')}",
        f"Runs: {status.get('run_count', 0)}",
        f"Best: {_fmt_score(status.get('best_metric_value'))}",
        f"Goal: {status.get('goal', '?')[:200]}",
        f"Failures: {status.get('failure_streak', 0)} streak",
        f"Dispatched: {'yes' if status.get('current_dispatch') else 'no'}",
        f"Leased: {'yes' if status.get('current_lease') else 'no'}",
    ]
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    if parent:
        lines.append(f"Parent: {parent}")
    if runtime:
        lines.append(f"Runtime: ~{runtime}min/iteration")
    if notify != "silent":
        lines.append(f"Notify: {notify}")
    if known_good:
        lines.append(f"Safe fallback: {known_good}")
    lines += [
        "",
        "--- Summary ---",
        summary_text[:800],
        "",
        "--- Next Steps ---",
        next_text[:400],
    ]
    return "\n".join(lines)


def cmd_pause(exp_id: str, reason: str = "paused via Telegram") -> str:
    """Pause an experiment."""
    paths = _get_paths()
    status = get_status(paths, exp_id)
    if status is None:
        return f"Experiment not found: {exp_id}"
    if status.get("phase") == "paused":
        return f"{exp_id} is already paused."
    set_phase(paths, exp_id, "paused", reason=reason)
    write_lab_status(paths)
    return f"Paused: {exp_id}"


def cmd_resume(exp_id: str) -> str:
    """Resume a paused experiment."""
    paths = _get_paths()
    status = get_status(paths, exp_id)
    if status is None:
        return f"Experiment not found: {exp_id}"
    if status.get("phase") != "paused":
        return f"{exp_id} is not paused (current: {status.get('phase')})"
    set_phase(paths, exp_id, "queued", reason="resumed via Telegram")
    write_lab_status(paths)
    return f"Resumed: {exp_id}"


def cmd_repair() -> str:
    """Run watchdog repair and clean stale state."""
    paths = _get_paths()
    report = watchdog(paths, repair=True)

    # Also clean stale leases for all active experiments
    experiments = list_experiments(paths)
    cleaned = []
    for e in experiments:
        eid = e.get("id", "")
        if e.get("current_lease") or e.get("current_dispatch"):
            try:
                clear_lease(paths, eid, event_type="lease_reclaimed", reason="telegram repair")
            except Exception:
                pass
            s = get_status(paths, eid)
            if s:
                if s.get("current_dispatch"):
                    did = s["current_dispatch"].get("dispatch_id", "")
                    if did:
                        try:
                            mark_dispatch_complete(paths, did, outcome="error", worker="telegram-repair")
                        except Exception:
                            pass
                    s["current_dispatch"] = None
                s["current_lease"] = None
                save_status(paths, eid, s)
                cleaned.append(eid)

    write_lab_status(paths)

    lines = ["Lab repair complete."]
    if report.get("reclaimed_leases"):
        lines.append(f"Reclaimed leases: {', '.join(report['reclaimed_leases'])}")
    if cleaned:
        lines.append(f"Cleaned stale state: {', '.join(cleaned)}")
    lines.append(f"Disk: {report['free_gb']:.1f} GB free")
    if report.get("alerts"):
        for a in report["alerts"]:
            lines.append(f"Alert: {a}")
    else:
        lines.append("No alerts.")
    return "\n".join(lines)


def cmd_cleanup_dispatches(exp_id: str | None = None) -> str:
    """Clean completed dispatch directories to free disk."""
    paths = _get_paths()
    import shutil
    complete_dir = paths.dispatch_complete
    if not complete_dir.exists():
        return "No completed dispatches to clean."

    removed = 0
    freed_bytes = 0
    for entry in complete_dir.iterdir():
        if not entry.is_dir():
            continue
        if exp_id and exp_id not in entry.name:
            continue
        try:
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            shutil.rmtree(entry)
            removed += 1
            freed_bytes += size
        except Exception:
            pass

    freed_mb = freed_bytes / (1024 * 1024)
    return f"Cleaned {removed} dispatches, freed {freed_mb:.1f} MB"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def handle_lab_command(text: str) -> str | None:
    """Parse and execute a lab command. Returns response text or None if not a lab command.

    Recognized patterns:
        lab status / lab dashboard
        lab list
        lab detail <exp_id> / lab show <exp_id>
        lab pause <exp_id>
        lab resume <exp_id>
        lab repair / lab fix / lab cleanup
        lab clean dispatches [exp_id]
    """
    # Normalize
    text = text.strip().lower()

    # Strip leading slash or "lab" prefix
    if text.startswith("/lab"):
        text = text[4:].strip()
    elif text.startswith("lab "):
        text = text[4:].strip()
    elif text == "lab":
        text = "status"
    else:
        return None

    parts = text.split()
    cmd = parts[0] if parts else "status"
    args = parts[1:]

    try:
        if cmd in ("status", "dashboard", "s"):
            return cmd_status()
        elif cmd in ("list", "ls", "l"):
            return cmd_list()
        elif cmd in ("detail", "show", "info", "d") and args:
            return cmd_detail(args[0])
        elif cmd in ("pause", "stop", "p") and args:
            reason = " ".join(args[1:]) if len(args) > 1 else "paused via Telegram"
            return cmd_pause(args[0], reason)
        elif cmd in ("resume", "start", "r") and args:
            return cmd_resume(args[0])
        elif cmd in ("repair", "fix", "heal"):
            return cmd_repair()
        elif cmd in ("clean", "cleanup", "gc"):
            exp_id = args[0] if args else None
            return cmd_cleanup_dispatches(exp_id)
        elif cmd == "help":
            return LAB_HELP
        else:
            return f"Unknown lab command: {cmd}\n\n{LAB_HELP}"
    except Exception as e:
        return f"Error: {str(e)[:300]}"


LAB_HELP = """Hermes Lab Commands:
  lab status     Dashboard overview
  lab list       All experiments
  lab detail ID  Experiment details + summary
  lab pause ID   Pause an experiment
  lab resume ID  Resume a paused experiment
  lab repair     Fix stale leases, dispatches
  lab clean      Remove completed dispatch dirs
  lab help       This message"""
