# Multi-Fidelity Experiments

_Proxy, validation, and finalist runs inside one stable experiment folder_

---

## Why this exists

Karpathy-style autoresearch is usually presented as a single edit surface plus a single evaluator. That abstraction is still correct, but many real systems need a cheaper screening stage before the expensive one.

Examples:

- small-model smoke tests before full LLM training runs
- Jetson or CPU proxy runs before rented H100 finalists
- coarse simulation before high-resolution simulation
- surrogate scoring before wet-lab or human review

Hermes Lab now treats that pattern as first-class.

## Core idea

One experiment can expose multiple fidelity tiers:

- `proxy`
- `validation`
- `final`

The experiment still lives at one stable path and still produces one append-only run ledger. What changes is the effective execution contract for the next run.

## Spec fields

Base fields:

- `fidelity_tiers`
- `initial_fidelity_tier`
- `fidelity_promotion_rule`
- `promote_after_successes`
- `executor_class`

Current implementation supports two promotion modes:

- `manual`
  Switch tiers explicitly with `labctl set-fidelity`.
- `after-success-streak`
  Auto-advance to the next tier after `promote_after_successes` successful runs in the current tier.

## Tier-specific overrides

Any normal execution field can be specialized with the naming convention:

`<tier>_<field>`

Examples:

- `proxy_executor_class`
- `proxy_time_budget_minutes`
- `proxy_validation_command`
- `proxy_promotion_strategy`
- `final_executor_command`
- `final_workspace_root`
- `final_mutable_paths`

The scheduler resolves the effective spec for the current tier before each run.

## Supported override targets

The tier resolver currently supports:

- `goal`
- `metric`
- `metric_direction`
- `priority`
- `cadence`
- `time_budget_minutes`
- `workspace_root`
- `setup_command`
- `baseline_command`
- `executor_command`
- `mutation_command`
- `validation_command`
- `acceptance_rule`
- `promotion_strategy`
- `workspace_mode`
- `require_clean_workspace`
- `mutable_paths`
- `read_only_paths`
- `ingress_files`
- `egress_files`
- `artifacts_expected`
- `worker_roles`
- `constraints`
- `executor_class`

## Status and run metadata

The reducer now tracks:

- `current_fidelity_tier`
- `next_fidelity_tier`
- `fidelity_tiers`
- `executor_class`
- `run_count_by_tier`
- `success_streak_by_tier`
- `best_run_by_tier`
- `best_metric_by_tier`

Each run manifest also records:

- `fidelity_tier`
- `executor_class`

This prevents proxy and final runs from being mixed together implicitly.

## Control surface

Manual promotion:

```bash
python3 scripts/labctl.py set-fidelity <exp_id> final --reason "promote to finalist runs"
```

Root-pointed agents do not need to inspect the Python to understand the current tier. They can read:

1. `LAB-STATUS.md`
2. `experiments/<id>/RUNBOOK.md`
3. `experiments/<id>/SUMMARY.md`
4. `experiments/<id>/STATUS.json`

## Recommended pattern

The safest default is:

- `proxy_promotion_strategy: patch-only`
- `final_promotion_strategy: apply-on-accept`

That keeps cheap screening runs from dirtying the canonical workspace while still allowing finalist runs to promote a validated patch back when appropriate.

## Generic design rule

This is not an ML-only feature.

The generic principle is:

1. use cheap signal first
2. keep tiers explicit
3. record which tier produced each result
4. never confuse proxy wins with final wins

That is the right abstraction for a generic research lab, regardless of domain.
