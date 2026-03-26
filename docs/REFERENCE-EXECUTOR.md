# Reference executor

_Concrete adapter between Hermes Lab experiments and real workspace mutation loops_

---

## What it does

`scripts/reference_executor.py` turns the scheduler contract into a usable execution loop for generalized autoresearch tasks, especially code-oriented tasks.

It reads the lab env contract, creates a sandbox workspace, runs the configured commands, compares the metric, writes `RESULT.md` and `metrics.json`, and saves decision artifacts into the run bundle.

## Supported spec fields

| Field | Purpose |
| --- | --- |
| `workspace_root` | Original workspace to evaluate |
| `setup_command` | Optional setup step inside the sandbox |
| `baseline_command` | Optional metric measurement before mutation |
| `mutation_command` | Mutation step, or omit it and use the local `agent_*` contract |
| `validation_command` | Required metric measurement after mutation |
| `acceptance_rule` | Human-readable decision policy |
| `promotion_strategy` | `patch-only` or `apply-on-accept` |
| `workspace_mode` | `auto`, `git-clone`, or `copy` |
| `require_clean_workspace` | Require a clean git workspace before mutation/promotion |

If a SPEC sets `agent_provider`, the lab synthesizes `python3 scripts/local_agent_mutation.py` and passes the generic `LAB_AGENT_*` fields to the executor environment.

## Sandbox behavior

Default behavior is intentionally conservative:

- If the workspace is a git repo and `workspace_mode` is `auto`, the executor uses a sandboxed git clone.
- If the workspace is not a git repo, it copies the workspace into the run bundle.
- All mutation happens in the sandbox, not the original workspace.

## Promotion strategies

### `patch-only`

The executor:

1. Mutates the sandbox.
2. Measures the candidate metric.
3. Saves `diff.patch`, `diffstat.txt`, and `decision.json`.
4. Leaves the original workspace unchanged.

Use this when you want safe offline exploration and manual review of candidates.

### `apply-on-accept`

The executor:

1. Mutates the sandbox.
2. Measures the candidate metric.
3. Compares it to the best-so-far metric, or the baseline if no best exists yet.
4. Applies the generated patch back to the original workspace only if the candidate strictly improves the reference metric.

Use this only with a clean git workspace. This is the closest shipped behavior to a classic autoresearch keep-or-revert loop.

## Multi-fidelity guidance

If you are using fidelity tiers such as `proxy` and `final`, prefer:

- `proxy_promotion_strategy: patch-only`
- `final_promotion_strategy: apply-on-accept`

That keeps cheap screening runs from making the original workspace dirty before finalist runs begin.

## Artifacts written into the run bundle

- `RESULT.md`
- `metrics.json`
- `stdout.log`
- `stderr.log`
- `artifacts/decision.json`
- `artifacts/diff.patch` when git-backed
- `artifacts/diffstat.txt` when git-backed
- `artifacts/changed-files.txt` when git-backed
- `artifacts/commands/*.stdout.log`
- `artifacts/commands/*.stderr.log`

## Command contract

Metric commands should print either:

1. A raw numeric value on the last non-empty stdout line, or
2. A JSON object containing at least:

```json
{"value": 1.23}
```

Mutation commands can be any shell command that operates inside the sandbox workspace.

Repo-shipped commands also work. If the command points at a repo-relative script such as `python3 scripts/openai_mutation_adapter.py`, the reference executor resolves that path against the lab repo before execution.

## Recommended first use

Start from `templates/code-autoresearch.yaml`, fill in:

- `workspace_root`
- `baseline_command`
- `mutation_command`
- `validation_command`

Then keep `promotion_strategy: patch-only` until the task is stable. Move to `apply-on-accept` only when you trust the validator and the workspace is dedicated to the loop.

For model-backed code loops, see:

- `templates/openai-codex-autoresearch.yaml` and `docs/OPENAI-MUTATION-ADAPTER.md`
- `templates/claude-autoresearch.yaml` and `docs/CLAUDE-MUTATION-ADAPTER.md`
