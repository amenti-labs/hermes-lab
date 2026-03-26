#!/usr/bin/env python3
"""labctl - Hermes Lab command-line control."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from lab.core import (
    create_experiment,
    claim_dispatch,
    dispatch_agent_next,
    dispatch_agent_submit,
    dispatch_work,
    get_paths,
    list_experiments,
    list_dispatch_packages,
    ingest_dispatch,
    mark_dispatch_complete,
    queue_dispatch,
    record_command,
    recover_lab,
    run_once,
    set_fidelity_tier,
    set_phase,
    watchdog,
    write_digest,
    write_lab_status,
    write_weekly_digest,
)


def get_paths_or_die(*, create: bool = False):
    try:
        return get_paths(create=create)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)


def cmd_init(_args: argparse.Namespace) -> None:
    paths = get_paths_or_die(create=True)
    record_command(paths, "init")
    write_lab_status(paths)
    print(f"initialized: {paths.root}")


def cmd_status(_args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    experiments = list_experiments(paths)
    dispatch_packages = list_dispatch_packages(paths)
    health = watchdog(paths, repair=False)
    print(f"data_root: {paths.root}")
    print(f"experiments: {len(experiments)}")
    by_phase: dict[str, int] = {}
    for experiment in experiments:
        phase = experiment.get("phase", "unknown")
        by_phase[phase] = by_phase.get(phase, 0) + 1
    for phase, count in sorted(by_phase.items()):
        print(f"  {phase}: {count}")
    if dispatch_packages:
        print("dispatch:")
        by_stage: dict[str, int] = {}
        for package in dispatch_packages:
            stage = str(package["record"].get("stage", package["stage_dir"]))
            by_stage[stage] = by_stage.get(stage, 0) + 1
        for stage, count in sorted(by_stage.items()):
            print(f"  {stage}: {count}")
    print(f"free_gb: {health['free_gb']}")
    if health["alerts"]:
        print("alerts:")
        for alert in health["alerts"]:
            print(f"  - {alert}")


def cmd_list(_args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    experiments = list_experiments(paths)
    if not experiments:
        print("No experiments")
        return
    for experiment in sorted(experiments, key=lambda item: item.get("id", "")):
        print(
            f"{experiment.get('id')}\t"
            f"{experiment.get('phase')}\t"
            f"tier={experiment.get('current_fidelity_tier', 'default')}\t"
            f"class={experiment.get('executor_class', 'default')}\t"
            f"runs={experiment.get('run_count', 0)}\t"
            f"best={experiment.get('best_metric_value')}\t"
            f"{experiment.get('goal', '')}"
        )


def cmd_create(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    spec_path = Path(args.spec)
    if not spec_path.exists():
        print(f"File not found: {spec_path}", file=sys.stderr)
        sys.exit(1)
    record_command(paths, "create", target=spec_path.stem, parameters={"spec": str(spec_path)})
    status = create_experiment(paths, spec_path)
    write_lab_status(paths)
    print(f"created experiment: {status['id']}")


def cmd_run_once(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    messages = run_once(paths, max_runs=args.max_runs, allowed_executor_classes=args.executor_class)
    if not messages:
        print("No eligible experiments")
    else:
        for message in messages:
            print(message)
    write_lab_status(paths)


def cmd_dispatch_ready(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(
        paths,
        "dispatch-ready",
        parameters={"max_runs": args.max_runs, "executor_class": args.executor_class},
    )
    messages = queue_dispatch(paths, max_runs=args.max_runs, allowed_executor_classes=args.executor_class)
    if not messages:
        print("No eligible experiments")
    else:
        for message in messages:
            print(message)
    write_lab_status(paths)


def cmd_dispatch_claim(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(
        paths,
        "dispatch-claim",
        parameters={
            "max_runs": args.max_runs,
            "worker": args.worker,
            "executor_class": args.executor_class,
        },
    )
    claimed = claim_dispatch(
        paths,
        max_claims=args.max_runs,
        worker=args.worker,
        allowed_executor_classes=args.executor_class,
    )
    if not claimed:
        print("No ready dispatch packages")
    else:
        for package in claimed:
            print(f"{package['record']['dispatch_id']}\t{package['dir']}")
    write_lab_status(paths)


def cmd_dispatch_work(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(
        paths,
        "dispatch-work",
        parameters={
            "max_runs": args.max_runs,
            "worker": args.worker,
            "executor_class": args.executor_class,
        },
    )
    messages = dispatch_work(
        paths,
        max_runs=args.max_runs,
        worker=args.worker,
        allowed_executor_classes=args.executor_class,
    )
    if not messages:
        print("No ready dispatch packages")
    else:
        for message in messages:
            print(message)
    write_lab_status(paths)


def cmd_dispatch_complete(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(
        paths,
        "dispatch-complete",
        target=args.dispatch_id,
        parameters={"outcome": args.outcome, "worker": args.worker},
    )
    record = mark_dispatch_complete(
        paths,
        args.dispatch_id,
        outcome=args.outcome,
        worker=args.worker or None,
    )
    write_lab_status(paths)
    print(f"{record['dispatch_id']} -> complete")


def cmd_dispatch_ingest(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(
        paths,
        "dispatch-ingest",
        parameters={"max_runs": args.max_runs, "dispatch_ids": args.dispatch_id},
    )
    messages = ingest_dispatch(paths, max_runs=args.max_runs, dispatch_ids=args.dispatch_id)
    if not messages:
        print("No complete dispatch packages")
    else:
        for message in messages:
            print(message)
    write_lab_status(paths)


def cmd_dispatch_agent_next(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(
        paths,
        "dispatch-agent-next",
        parameters={"worker": args.worker, "executor_class": args.executor_class},
    )
    result = dispatch_agent_next(
        paths,
        worker=args.worker,
        allowed_executor_classes=args.executor_class or None,
        experiment_id=args.experiment or None,
    )
    if result is None:
        print("{}", flush=True)
        sys.exit(1)
    print(json.dumps(result, indent=2, default=str))
    write_lab_status(paths)


def cmd_dispatch_agent_submit(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    # Read the submission JSON
    submission_path = Path(args.submission)
    if not submission_path.exists():
        print(f"File not found: {submission_path}", file=sys.stderr)
        sys.exit(1)
    submission = json.loads(submission_path.read_text())
    dispatch_id = str(submission.get("dispatch_id") or args.dispatch_id or "")
    if not dispatch_id:
        print("dispatch_id required (in JSON or as argument)", file=sys.stderr)
        sys.exit(1)
    changes = submission.get("changes", {})
    reasoning = str(submission.get("reasoning", "") or "")

    record_command(
        paths,
        "dispatch-agent-submit",
        target=dispatch_id,
        parameters={"changes_count": len(changes)},
    )
    try:
        result = dispatch_agent_submit(
            paths,
            dispatch_id,
            changes,
            reasoning=reasoning,
            worker=args.worker,
        )
        print(json.dumps(result, indent=2, default=str))
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    write_lab_status(paths)


def cmd_pause(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(paths, "pause", target=args.exp_id)
    set_phase(paths, args.exp_id, "paused", reason=args.reason or "manual pause")
    write_lab_status(paths)
    print(f"paused: {args.exp_id}")


def cmd_resume(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(paths, "resume", target=args.exp_id)
    set_phase(paths, args.exp_id, "queued", reason=args.reason or "manual resume")
    write_lab_status(paths)
    print(f"resumed: {args.exp_id}")


def cmd_complete(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(paths, "complete", target=args.exp_id)
    set_phase(paths, args.exp_id, "completed", reason=args.reason or "manual completion")
    write_lab_status(paths)
    print(f"completed: {args.exp_id}")


def cmd_set_fidelity(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    record_command(
        paths,
        "set-fidelity",
        target=args.exp_id,
        parameters={"tier": args.tier, "reason": args.reason or "manual fidelity change"},
    )
    status = set_fidelity_tier(paths, args.exp_id, args.tier, reason=args.reason or "manual fidelity change")
    write_lab_status(paths)
    print(f"{args.exp_id} -> fidelity tier {status['current_fidelity_tier']}")


def cmd_digest(_args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    out = write_digest(paths)
    print(f"digest written: {out}")


def cmd_weekly_digest(_args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    out = write_weekly_digest(paths)
    print(f"weekly digest written: {out}")


def cmd_refresh(_args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    recover_lab(paths)
    out = write_lab_status(paths)
    print(f"LAB-STATUS.md refreshed: {out}")


def cmd_runner(args: argparse.Namespace, mode: str) -> None:
    from lab.runner import RunConfig, run_burst, run_guided, run_swarm
    config = RunConfig(
        experiment=args.exp_id,
        mode=mode,
        iterations=args.iterations,
        strategy=args.strategy,
        strategies=args.strategies or [],
        search_space_file=args.search_space_file,
        worker=args.worker,
        pause_between=args.pause,
        direction=args.direction,
        seed=args.seed,
    )
    if mode == "burst":
        run_burst(config)
    elif mode == "guided":
        run_guided(config)
    elif mode == "swarm":
        run_swarm(config)


def cmd_recover(_args: argparse.Namespace) -> None:
    from lab.recovery import recover
    recover()


def cmd_watchdog(args: argparse.Namespace) -> None:
    paths = get_paths_or_die()
    report = watchdog(paths, repair=args.repair)
    print(f"free_gb: {report['free_gb']}")
    if report["reclaimed_leases"]:
        print("reclaimed_leases:")
        for experiment in report["reclaimed_leases"]:
            print(f"  - {experiment}")
    if report["alerts"]:
        print("alerts:")
        for alert in report["alerts"]:
            print(f"  - {alert}")
    else:
        print("alerts: none")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Lab control")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("list").set_defaults(func=cmd_list)

    create_parser = sub.add_parser("create")
    create_parser.add_argument("spec", help="Path to SPEC.yaml file")
    create_parser.set_defaults(func=cmd_create)

    add_task_parser = sub.add_parser("add-task")
    add_task_parser.add_argument("spec", help="Path to SPEC.yaml file")
    add_task_parser.set_defaults(func=cmd_create)

    run_parser = sub.add_parser("run-once")
    run_parser.add_argument("--max-runs", type=int, default=3)
    run_parser.add_argument("--executor-class", action="append", default=[], help="Only claim experiments for matching executor_class")
    run_parser.set_defaults(func=cmd_run_once)

    dispatch_ready_parser = sub.add_parser("dispatch-ready")
    dispatch_ready_parser.add_argument("--max-runs", type=int, default=3)
    dispatch_ready_parser.add_argument("--executor-class", action="append", default=[], help="Only queue experiments for matching executor_class")
    dispatch_ready_parser.set_defaults(func=cmd_dispatch_ready)

    dispatch_claim_parser = sub.add_parser("dispatch-claim")
    dispatch_claim_parser.add_argument("--max-runs", type=int, default=1)
    dispatch_claim_parser.add_argument("--worker", default="dispatch-worker")
    dispatch_claim_parser.add_argument("--executor-class", action="append", default=[], help="Only claim packages for matching executor_class")
    dispatch_claim_parser.set_defaults(func=cmd_dispatch_claim)

    dispatch_work_parser = sub.add_parser("dispatch-work")
    dispatch_work_parser.add_argument("--max-runs", type=int, default=1)
    dispatch_work_parser.add_argument("--worker", default="dispatch-worker")
    dispatch_work_parser.add_argument("--executor-class", action="append", default=[], help="Only claim packages for matching executor_class")
    dispatch_work_parser.set_defaults(func=cmd_dispatch_work)

    dispatch_complete_parser = sub.add_parser("dispatch-complete")
    dispatch_complete_parser.add_argument("dispatch_id")
    dispatch_complete_parser.add_argument("--outcome", default="success")
    dispatch_complete_parser.add_argument("--worker", default="")
    dispatch_complete_parser.set_defaults(func=cmd_dispatch_complete)

    dispatch_ingest_parser = sub.add_parser("dispatch-ingest")
    dispatch_ingest_parser.add_argument("dispatch_id", nargs="*")
    dispatch_ingest_parser.add_argument("--max-runs", type=int, default=3)
    dispatch_ingest_parser.set_defaults(func=cmd_dispatch_ingest)

    agent_next_parser = sub.add_parser("dispatch-agent-next",
        help="Prepare + claim one dispatch package and output JSON context for an external agent")
    agent_next_parser.add_argument("--worker", default="dispatch-agent")
    agent_next_parser.add_argument("--experiment", default="",
        help="Only dispatch for this specific experiment ID")
    agent_next_parser.add_argument("--executor-class", action="append", default=[],
        help="Only claim experiments for matching executor_class")
    agent_next_parser.set_defaults(func=cmd_dispatch_agent_next)

    agent_submit_parser = sub.add_parser("dispatch-agent-submit",
        help="Submit agent changes, validate, score, complete, and ingest a dispatch")
    agent_submit_parser.add_argument("submission", help="Path to JSON file with dispatch_id, changes, and optional reasoning")
    agent_submit_parser.add_argument("--dispatch-id", default="", help="Override dispatch_id (if not in JSON)")
    agent_submit_parser.add_argument("--worker", default="dispatch-agent")
    agent_submit_parser.set_defaults(func=cmd_dispatch_agent_submit)

    pause_parser = sub.add_parser("pause")
    pause_parser.add_argument("exp_id")
    pause_parser.add_argument("--reason", default="")
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("exp_id")
    resume_parser.add_argument("--reason", default="")
    resume_parser.set_defaults(func=cmd_resume)

    complete_parser = sub.add_parser("complete")
    complete_parser.add_argument("exp_id")
    complete_parser.add_argument("--reason", default="")
    complete_parser.set_defaults(func=cmd_complete)

    fidelity_parser = sub.add_parser("set-fidelity")
    fidelity_parser.add_argument("exp_id")
    fidelity_parser.add_argument("tier")
    fidelity_parser.add_argument("--reason", default="")
    fidelity_parser.set_defaults(func=cmd_set_fidelity)

    sub.add_parser("digest").set_defaults(func=cmd_digest)
    sub.add_parser("weekly-digest").set_defaults(func=cmd_weekly_digest)
    sub.add_parser("refresh").set_defaults(func=cmd_refresh)

    sub.add_parser("recover", help="Full lab recovery — clear stale leases, dispatches, workspaces").set_defaults(func=cmd_recover)

    watchdog_parser = sub.add_parser("watchdog")
    watchdog_parser.add_argument("--repair", action="store_true")
    watchdog_parser.set_defaults(func=cmd_watchdog)

    # Runner modes: burst, guided, swarm
    for mode_name in ["burst", "guided", "swarm"]:
        mode_parser = sub.add_parser(mode_name, help=f"Run experiment in {mode_name} mode")
        mode_parser.add_argument("exp_id", help="Experiment ID")
        mode_parser.add_argument("-n", "--iterations", type=int, default=10)
        mode_parser.add_argument("--strategy", default="random", help="Strategy (burst/guided)")
        mode_parser.add_argument("--strategies", nargs="+", help="Strategies (swarm)")
        mode_parser.add_argument("--search-space", dest="search_space_file", default="")
        mode_parser.add_argument("--worker", default="hermes-runner")
        mode_parser.add_argument("--pause", type=float, default=0.0)
        mode_parser.add_argument("--direction", default="maximize", choices=["maximize", "minimize"])
        mode_parser.add_argument("--seed", type=int, default=None)
        mode_parser.set_defaults(func=lambda args, m=mode_name: cmd_runner(args, m))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
