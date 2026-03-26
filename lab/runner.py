"""Hermes Lab experiment runner — burst, guided, and swarm modes.

Wraps the existing dispatch loop with different scheduling and strategy patterns.
All modes use the same core: dispatch_agent_next → propose → dispatch_agent_submit.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lab.core import (
    dispatch_agent_next,
    dispatch_agent_submit,
    get_paths,
    get_status,
    save_status,
    load_spec,
    experiment_dir,
    LabPaths,
)
from lab.strategies import (
    Strategy,
    SearchSpace,
    Trial,
    get_strategy,
    STRATEGIES,
)
from lab.blackboard import Blackboard, TrialRecord


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    """Configuration for a run session."""
    experiment: str
    mode: str = "burst"            # burst, guided, swarm
    iterations: int = 10           # max iterations
    strategy: str = "llm"          # default strategy (burst/guided)
    strategies: list[str] = field(default_factory=list)  # swarm strategies
    search_space_file: str = ""    # path to search_space.json
    worker: str = "hermes-runner"
    data_root: str = ""
    pause_between: float = 0.0    # seconds between iterations
    direction: str = "maximize"    # metric direction
    seed: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print(msg: str, **kwargs):
    print(msg, **kwargs)
    sys.stdout.flush()


def _banner(mode: str, experiment: str, iterations: int, strategy: str | list[str]):
    _print(f"\n{'='*60}")
    _print(f"  Hermes Lab — {mode.upper()} mode")
    _print(f"  Experiment: {experiment}")
    _print(f"  Iterations: {iterations}")
    if isinstance(strategy, list):
        _print(f"  Strategies: {', '.join(strategy)}")
    else:
        _print(f"  Strategy: {strategy}")
    _print(f"{'='*60}\n")


def _result_line(i: int, total: int, score: float | None, accepted: bool, strategy: str, summary: str):
    status = "✓" if accepted else "✗"
    score_str = f"{score:.6f}" if score is not None else "N/A"
    _print(f"  [{i}/{total}] {status} score={score_str} strategy={strategy} | {summary}")


# ---------------------------------------------------------------------------
# Core dispatch cycle (one iteration)
# ---------------------------------------------------------------------------

def _deep_merge_params(config: dict, flat_params: dict[str, Any]) -> None:
    """Merge flat params into a nested config dict.

    If a key exists at top level, set it there.
    Otherwise, search nested dicts for the key.
    E.g., flat_params={"weight_decay": 3.0} finds config["training"]["weight_decay"].
    """
    for key, value in flat_params.items():
        if key in config:
            config[key] = value
            continue
        # Search nested dicts
        found = False
        for section_key, section_val in config.items():
            if isinstance(section_val, dict) and key in section_val:
                section_val[key] = value
                found = True
                break
        if not found:
            # Put in top level as fallback
            config[key] = value


def _run_one_cycle(
    paths: LabPaths,
    experiment: str,
    *,
    worker: str,
    params_override: dict[str, Any] | None = None,
    reasoning: str = "",
    current_files_override: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Run one dispatch cycle. Returns result dict or None if no work available."""
    # Get next work package
    ctx = dispatch_agent_next(paths, worker=worker)
    if ctx is None:
        return None

    dispatch_id = ctx["dispatch_id"]

    # If params_override, merge into current mutable files
    if params_override is not None and current_files_override is None:
        # Read current mutable file, parse as JSON, deep-merge params
        changes = {}
        for rel_path, content in ctx.get("current_files", {}).items():
            try:
                data = json.loads(content)
                _deep_merge_params(data, params_override)
                changes[rel_path] = json.dumps(data, indent=2)
            except (json.JSONDecodeError, TypeError):
                # Not JSON, skip
                changes[rel_path] = content
        if not changes:
            # Fallback: create the first mutable path with just the params
            mutable = ctx.get("mutable_paths", [])
            if mutable:
                changes[mutable[0]] = json.dumps(params_override, indent=2)
    elif current_files_override is not None:
        changes = current_files_override
    else:
        # LLM mode: caller handles the changes externally
        return {"dispatch_id": dispatch_id, "context": ctx, "needs_llm": True}

    # Submit
    result = dispatch_agent_submit(
        paths,
        dispatch_id,
        changes,
        reasoning=reasoning,
        worker=worker,
    )
    return result


def _reset_due_time(paths: LabPaths, experiment: str):
    """Reset next_due_at to allow immediate dispatch."""
    status = get_status(paths, experiment)
    if status:
        status["next_due_at"] = "2020-01-01T00:00:00+00:00"
        save_status(paths, experiment, status)


def _cleanup_stale_state(paths: LabPaths, experiment: str):
    """Clear any stale lease/dispatch after an error."""
    from lab.core import clear_lease, list_dispatch_packages, mark_dispatch_complete
    try:
        clear_lease(paths, experiment, event_type="lease_reclaimed", reason="runner cleanup")
    except Exception:
        pass
    status = get_status(paths, experiment)
    if status and status.get("current_dispatch"):
        dispatch_id = status["current_dispatch"].get("dispatch_id", "")
        if dispatch_id:
            try:
                mark_dispatch_complete(paths, dispatch_id, outcome="error", worker="runner-cleanup")
            except Exception:
                pass
        status["current_dispatch"] = None
        save_status(paths, experiment, status)


# ---------------------------------------------------------------------------
# Mode: Burst
# ---------------------------------------------------------------------------

def run_burst(
    config: RunConfig,
    *,
    on_propose: Callable[[dict[str, Any], int], dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """Run N iterations back-to-back.

    If strategy is 'llm', on_propose callback is called with (context, iteration)
    and must return {"changes": {...}, "reasoning": "..."} or None to skip.

    For other strategies, params are auto-generated from search space.
    """
    paths = get_paths(config.data_root or None)
    results = []

    space = None
    strategy_obj = None
    if config.strategy != "llm":
        strategy_obj = get_strategy(
            config.strategy,
            seed=config.seed,
            **({"direction": config.direction} if config.strategy == "bayesian" else {}),
            **({"budget": config.iterations} if config.strategy == "evolution" else {}),
        )
        if config.search_space_file:
            space = SearchSpace.from_json_file(Path(config.search_space_file))
        else:
            # Try to find search_space.json in workspace
            exp_dir = experiment_dir(paths, config.experiment)
            spec = load_spec(exp_dir)
            ws = Path(str(spec.get("workspace_root", "")))
            space_path = ws / "search_space.json"
            if space_path.exists():
                space = SearchSpace.from_json_file(space_path)
            else:
                raise FileNotFoundError(
                    f"No search space found. Create {space_path} or pass --search-space."
                )

    history: list[Trial] = []

    _banner(config.mode, config.experiment, config.iterations, config.strategy)

    for i in range(1, config.iterations + 1):
        _reset_due_time(paths, config.experiment)

        if strategy_obj and space:
            # Auto-generate params
            params = strategy_obj.ask(space, history)
            reasoning = f"Strategy '{strategy_obj.name}' suggested: {json.dumps(params)}"

            try:
                result = _run_one_cycle(
                    paths, config.experiment,
                    worker=config.worker,
                    params_override=params,
                    reasoning=reasoning,
                )
            except Exception as e:
                _print(f"  [{i}/{config.iterations}] ✗ Error: {str(e)[:120]}")
                _cleanup_stale_state(paths, config.experiment)
                history.append(Trial(
                    params=params, score=None, accepted=False,
                    strategy=strategy_obj.name,
                ))
                continue

            if result is None:
                _print(f"  [{i}/{config.iterations}] No work available. Stopping.")
                break

            score = result.get("candidate_value")
            accepted = result.get("accepted", False)
            strategy_obj.tell(params, score if score is not None else 0.0)
            history.append(Trial(
                params=params, score=score, accepted=accepted,
                strategy=strategy_obj.name,
            ))
            _result_line(i, config.iterations, score, accepted, strategy_obj.name, reasoning[:80])

        elif config.strategy == "llm":
            # LLM mode: get context, let callback propose
            result = _run_one_cycle(
                paths, config.experiment,
                worker=config.worker,
            )
            if result is None:
                _print(f"  [{i}/{config.iterations}] No work available. Stopping.")
                break

            if result.get("needs_llm"):
                if on_propose is None:
                    _print(f"  [{i}/{config.iterations}] LLM mode requires on_propose callback. Stopping.")
                    break
                proposal = on_propose(result["context"], i)
                if proposal is None:
                    _print(f"  [{i}/{config.iterations}] Skipped by callback.")
                    continue
                # Submit the proposal
                submit_result = dispatch_agent_submit(
                    paths,
                    result["dispatch_id"],
                    proposal["changes"],
                    reasoning=proposal.get("reasoning", ""),
                    worker=config.worker,
                )
                result = submit_result

            score = result.get("candidate_value")
            accepted = result.get("accepted", False)
            _result_line(i, config.iterations, score, accepted, "llm", "LLM-proposed mutation")

        results.append(result)

        if config.pause_between > 0 and i < config.iterations:
            time.sleep(config.pause_between)

    _print(f"\nCompleted {len(results)} iterations.")
    _print_summary(results, config)
    return results


# ---------------------------------------------------------------------------
# Mode: Guided
# ---------------------------------------------------------------------------

def run_guided(
    config: RunConfig,
    *,
    on_propose: Callable[[dict[str, Any], int], dict[str, Any] | None] | None = None,
    on_approve: Callable[[dict[str, Any], int], bool] | None = None,
) -> list[dict[str, Any]]:
    """Like burst but pauses for approval before each iteration.

    on_approve(proposed_params, iteration) -> True to proceed, False to skip.
    If on_approve is None, uses stdin.
    """
    paths = get_paths(config.data_root or None)
    results = []

    space = None
    strategy_obj = None
    if config.strategy != "llm":
        strategy_obj = get_strategy(
            config.strategy,
            seed=config.seed,
            **({"direction": config.direction} if config.strategy == "bayesian" else {}),
            **({"budget": config.iterations} if config.strategy == "evolution" else {}),
        )
        if config.search_space_file:
            space = SearchSpace.from_json_file(Path(config.search_space_file))
        else:
            exp_dir = experiment_dir(paths, config.experiment)
            spec = load_spec(exp_dir)
            ws = Path(str(spec.get("workspace_root", "")))
            space_path = ws / "search_space.json"
            if space_path.exists():
                space = SearchSpace.from_json_file(space_path)
            else:
                raise FileNotFoundError("No search space found.")

    history: list[Trial] = []
    _banner("guided", config.experiment, config.iterations, config.strategy)

    for i in range(1, config.iterations + 1):
        _reset_due_time(paths, config.experiment)

        if strategy_obj and space:
            params = strategy_obj.ask(space, history)
            _print(f"\n  [{i}/{config.iterations}] Proposed ({strategy_obj.name}):")
            _print(f"    {json.dumps(params, indent=2)}")

            # Get approval
            if on_approve:
                approved = on_approve(params, i)
            else:
                response = input("    Approve? [Y/n/edit] ").strip().lower()
                if response == "n":
                    _print("    Skipped.")
                    continue
                elif response == "edit":
                    _print("    Enter JSON params:")
                    raw = input("    > ").strip()
                    try:
                        params = json.loads(raw)
                    except json.JSONDecodeError:
                        _print("    Invalid JSON. Skipping.")
                        continue
                approved = True

            if not approved:
                _print("    Skipped.")
                continue

            reasoning = f"Guided: strategy '{strategy_obj.name}' suggested, user approved"
            result = _run_one_cycle(
                paths, config.experiment,
                worker=config.worker,
                params_override=params,
                reasoning=reasoning,
            )
            if result is None:
                _print("    No work available. Stopping.")
                break

            score = result.get("candidate_value")
            accepted = result.get("accepted", False)
            strategy_obj.tell(params, score if score is not None else 0.0)
            history.append(Trial(params=params, score=score, accepted=accepted, strategy=strategy_obj.name))
            _result_line(i, config.iterations, score, accepted, strategy_obj.name, "")

        results.append(result)

    _print(f"\nCompleted {len(results)} iterations.")
    _print_summary(results, config)
    return results


# ---------------------------------------------------------------------------
# Mode: Swarm
# ---------------------------------------------------------------------------

def run_swarm(config: RunConfig) -> list[dict[str, Any]]:
    """Multi-strategy swarm: rotate through strategies, coordinate via blackboard.

    Each iteration picks the next strategy in rotation, asks it for params,
    runs the dispatch cycle, and records results to the blackboard.
    """
    paths = get_paths(config.data_root or None)
    strategies_list = config.strategies or ["random", "perturb", "bayesian"]

    # Load search space
    space = None
    if config.search_space_file:
        space = SearchSpace.from_json_file(Path(config.search_space_file))
    else:
        exp_dir = experiment_dir(paths, config.experiment)
        spec = load_spec(exp_dir)
        ws = Path(str(spec.get("workspace_root", "")))
        space_path = ws / "search_space.json"
        if space_path.exists():
            space = SearchSpace.from_json_file(space_path)
        else:
            raise FileNotFoundError(
                f"No search space found. Create {space_path} or pass --search-space."
            )

    # Initialize strategies
    strategy_objects: dict[str, Strategy] = {}
    for name in strategies_list:
        kwargs = {"seed": config.seed}
        if name == "bayesian":
            kwargs["direction"] = config.direction
        if name == "evolution":
            kwargs["budget"] = config.iterations
        strategy_objects[name] = get_strategy(name, **kwargs)

    # Initialize blackboard
    bb_path = paths.root / "blackboard.db"
    bb = Blackboard(bb_path)

    # Load existing history from blackboard
    existing_trials = bb.history(config.experiment, limit=500)
    history: list[Trial] = [
        Trial(
            params=t.params, score=t.score, accepted=t.accepted,
            strategy=t.strategy,
        )
        for t in existing_trials
    ]

    results = []
    _banner("swarm", config.experiment, config.iterations, strategies_list)
    _print(f"  Blackboard: {bb_path}")
    _print(f"  Existing trials: {len(existing_trials)}")
    _print("")

    for i in range(1, config.iterations + 1):
        # Round-robin strategy selection
        strategy_name = strategies_list[(i - 1) % len(strategies_list)]
        strategy_obj = strategy_objects[strategy_name]

        _reset_due_time(paths, config.experiment)

        # Check active claims (avoid duplicate work)
        claims = bb.active_claims(config.experiment)
        claim_descs = [c.description for c in claims]

        # Ask strategy for params
        params = strategy_obj.ask(space, history)
        reasoning = f"Swarm iteration {i}, strategy '{strategy_name}': {json.dumps(params)}"

        # Post claim
        claim_id = bb.claim(
            config.experiment, config.worker,
            f"{strategy_name}: {json.dumps(params)[:100]}",
            ttl_seconds=900,
        )

        # Record trial as pending
        trial_id = bb.submit(
            config.experiment, strategy_name, params,
            reasoning=reasoning, status="running",
        )

        # Run dispatch
        try:
            result = _run_one_cycle(
                paths, config.experiment,
                worker=config.worker,
                params_override=params,
                reasoning=reasoning,
            )
        except Exception as e:
            _print(f"  [{i}/{config.iterations}] ✗ Error: {str(e)[:120]}")
            _cleanup_stale_state(paths, config.experiment)
            bb.update(trial_id, status="failed")
            history.append(Trial(params=params, score=None, accepted=False, strategy=strategy_name))
            continue

        if result is None:
            _print(f"  [{i}/{config.iterations}] No work available. Stopping.")
            bb.update(trial_id, status="failed")
            break

        score = result.get("candidate_value")
        accepted = result.get("accepted", False)

        # Update blackboard
        bb.update(
            trial_id,
            score=score,
            accepted=accepted,
            status="completed",
            metadata={"dispatch_id": result.get("dispatch_id", "")},
        )

        # Tell strategy
        strategy_obj.tell(params, score if score is not None else 0.0)
        history.append(Trial(
            params=params, score=score, accepted=accepted,
            strategy=strategy_name,
        ))

        # Post to feed if accepted
        if accepted:
            bb.post(
                config.experiment,
                f"New best! score={score:.6f} via {strategy_name}: {json.dumps(params)}",
                worker=config.worker,
                trial_id=trial_id,
            )

        _result_line(i, config.iterations, score, accepted, strategy_name, "")
        results.append(result)

        # Clean expired claims
        bb.clear_expired_claims(config.experiment)

        if config.pause_between > 0 and i < config.iterations:
            time.sleep(config.pause_between)

    # Final summary
    _print(f"\nCompleted {len(results)} iterations.")
    _print_summary(results, config)

    # Blackboard summary
    _print(f"\n{bb.summary(config.experiment)}")

    # Per-strategy breakdown
    _print("\n  Strategy breakdown:")
    for name in strategies_list:
        strat_trials = [t for t in history if t.strategy == name]
        if strat_trials:
            scores = [t.score for t in strat_trials if t.score is not None]
            accepted_count = sum(1 for t in strat_trials if t.accepted)
            avg = sum(scores) / len(scores) if scores else 0
            best = max(scores) if scores else 0
            _print(f"    {name}: {len(strat_trials)} trials, "
                   f"avg={avg:.6f}, best={best:.6f}, accepted={accepted_count}")

    bb.close()
    return results


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _print_summary(results: list[dict[str, Any]], config: RunConfig):
    if not results:
        return
    scores = [r.get("candidate_value") for r in results if r.get("candidate_value") is not None]
    accepted = sum(1 for r in results if r.get("accepted"))
    if scores:
        best = max(scores) if config.direction == "maximize" else min(scores)
        avg = sum(scores) / len(scores)
        _print(f"  Best: {best:.6f} | Avg: {avg:.6f} | Accepted: {accepted}/{len(results)}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Hermes Lab runner — burst, guided, and swarm modes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  burst    Run N iterations back-to-back with a single strategy
  guided   Like burst but pause for approval each iteration
  swarm    Rotate through multiple strategies, coordinate via blackboard

Strategies:
  random    Random sampling within bounds
  perturb   Perturb the current best (PBT-inspired)
  bayesian  Optuna TPE-guided suggestions
  evolution Nevergrad CMA-ES optimization
  llm       Pass-through for LLM-driven mutations (burst/guided only)

Examples:
  # 20 iterations of random search
  python -m lab.runner burst grokking-entropy-search --strategy random -n 20

  # Bayesian optimization with Optuna
  python -m lab.runner burst grokking-entropy-search --strategy bayesian -n 50

  # Swarm with 3 strategies
  python -m lab.runner swarm grokking-entropy-search --strategies random perturb bayesian -n 30

  # Guided mode (interactive approval)
  python -m lab.runner guided grokking-entropy-search --strategy perturb -n 10
        """,
    )
    parser.add_argument("mode", choices=["burst", "guided", "swarm"])
    parser.add_argument("experiment", help="Experiment ID")
    parser.add_argument("-n", "--iterations", type=int, default=10)
    parser.add_argument("--strategy", default="random", help="Strategy for burst/guided")
    parser.add_argument("--strategies", nargs="+", help="Strategies for swarm mode")
    parser.add_argument("--search-space", dest="search_space_file", default="")
    parser.add_argument("--worker", default="hermes-runner")
    parser.add_argument("--data-root", dest="data_root", default="")
    parser.add_argument("--pause", type=float, default=0.0, help="Seconds between iterations")
    parser.add_argument("--direction", default="maximize", choices=["maximize", "minimize"])
    parser.add_argument("--seed", type=int, default=None)

    args = parser.parse_args()

    config = RunConfig(
        experiment=args.experiment,
        mode=args.mode,
        iterations=args.iterations,
        strategy=args.strategy,
        strategies=args.strategies or [],
        search_space_file=args.search_space_file,
        worker=args.worker,
        data_root=args.data_root,
        pause_between=args.pause,
        direction=args.direction,
        seed=args.seed,
    )

    if args.mode == "burst":
        run_burst(config)
    elif args.mode == "guided":
        run_guided(config)
    elif args.mode == "swarm":
        run_swarm(config)


if __name__ == "__main__":
    main()
