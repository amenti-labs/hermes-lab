"""Hermes Lab recovery — clean stale state after crashes.

Run this on gateway startup or manually to restore lab health.

Usage:
    python3 -m lab.recovery
    # Or from labctl:
    python3 scripts/labctl.py recover
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lab.core import (
    get_paths,
    get_status,
    save_status,
    list_experiments,
    list_dispatch_packages,
    clear_lease,
    mark_dispatch_complete,
    watchdog,
    write_lab_status,
    LabPaths,
)

DATA_ROOT = os.environ.get(
    "HERMES_LAB_DATA_ROOT",
    "./lab-data",
)


def recover(data_root: str | None = None, verbose: bool = True) -> dict:
    """Full lab recovery. Returns a report dict."""
    root = data_root or DATA_ROOT
    
    # Check if data root exists
    if not Path(root).exists():
        msg = "Data root not found. Skipping lab recovery."
        if verbose:
            print(msg)
        return {"skipped": True, "reason": msg}

    try:
        paths = get_paths(root)
    except Exception as e:
        msg = f"Cannot access lab data: {e}"
        if verbose:
            print(msg)
        return {"skipped": True, "reason": msg}

    report = {
        "skipped": False,
        "leases_cleared": [],
        "dispatches_cleared": [],
        "stale_workspaces_removed": [],
        "watchdog_alerts": [],
        "experiments_reset": [],
    }

    # 1. Run watchdog repair
    wd = watchdog(paths, repair=True)
    report["watchdog_alerts"] = wd.get("alerts", [])
    report["leases_cleared"].extend(wd.get("reclaimed_leases", []))

    # 2. Clear stale leases and dispatches for all experiments
    experiments = list_experiments(paths)
    for exp in experiments:
        eid = exp.get("id", "")
        phase = exp.get("phase", "")

        # Skip completed experiments
        if phase == "completed":
            continue

        status = get_status(paths, eid)
        if not status:
            continue

        needs_save = False

        # Clear stale lease
        if status.get("current_lease"):
            try:
                clear_lease(paths, eid, event_type="lease_reclaimed", reason="recovery")
                report["leases_cleared"].append(eid)
            except Exception:
                pass
            status["current_lease"] = None
            needs_save = True

        # Clear stale dispatch
        if status.get("current_dispatch"):
            did = status["current_dispatch"].get("dispatch_id", "")
            if did:
                try:
                    mark_dispatch_complete(paths, did, outcome="error", worker="recovery")
                    report["dispatches_cleared"].append(did)
                except Exception:
                    pass
            status["current_dispatch"] = None
            needs_save = True

        # Reset failure streak if experiment was stuck
        if status.get("failure_streak", 0) > 5:
            status["failure_streak"] = 0
            report["experiments_reset"].append(eid)
            needs_save = True

        # Fix poisoned next_due_at (2099 blocking from old untargeted crons)
        next_due = str(status.get("next_due_at", ""))
        if "2099" in next_due or "2098" in next_due or "2097" in next_due:
            status["next_due_at"] = "2020-01-01T00:00:00+00:00"
            report.setdefault("due_dates_fixed", []).append(eid)
            needs_save = True

        if needs_save:
            save_status(paths, eid, status)

    # 3. Clean stale running dispatches
    running_dir = paths.dispatch_running
    if running_dir.exists():
        for entry in running_dir.iterdir():
            if entry.is_dir():
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                    report["stale_workspaces_removed"].append(entry.name)
                except Exception:
                    pass

    # 4. Clean stale ready dispatches (shouldn't exist after crash)
    ready_dir = paths.dispatch_ready
    if ready_dir.exists():
        for entry in ready_dir.iterdir():
            if entry.is_dir():
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                except Exception:
                    pass

    # 5. Refresh lab status
    write_lab_status(paths)

    if verbose:
        print("Hermes Lab Recovery Complete")
        if report["leases_cleared"]:
            print(f"  Leases cleared: {', '.join(report['leases_cleared'])}")
        if report["dispatches_cleared"]:
            print(f"  Dispatches cleared: {len(report['dispatches_cleared'])}")
        if report["stale_workspaces_removed"]:
            print(f"  Stale workspaces removed: {len(report['stale_workspaces_removed'])}")
        if report["experiments_reset"]:
            print(f"  Failure streaks reset: {', '.join(report['experiments_reset'])}")
        if report.get("due_dates_fixed"):
            print(f"  Poisoned due dates fixed: {', '.join(report['due_dates_fixed'])}")
        if report["watchdog_alerts"]:
            for a in report["watchdog_alerts"]:
                print(f"  Alert: {a}")
        if not any([report["leases_cleared"], report["dispatches_cleared"],
                     report["stale_workspaces_removed"], report["experiments_reset"]]):
            print("  Nothing to clean up. Lab is healthy.")

    return report


if __name__ == "__main__":
    recover()
