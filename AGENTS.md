# Hermes Lab agent instructions

_Root-folder harness contract for generic coding and research agents_

---

## 📁 Directory layout

```
<repo-root>/
  scripts/        lab engine (labctl.py)
  lab-data/  experiments, dispatch, metrics
  workspaces/     experiment code (e.g. my-experiment/)
  templates/      experiment SPEC templates
  docs/           reference documentation
```

All paths use `HERMES_LAB_DATA_ROOT=<your-data-root>`.
New experiment workspaces go in `workspaces/`.

### Execution modes

**Cron** (autonomous background): Agent runs every N minutes via Hermes cron.
Use when you want unattended overnight/background search.

**Burst** (in-session, back-to-back): Run N iterations with auto-generated params.
Use when you're present and want fast exploration without manual steering.
```bash
labctl burst <exp_id> --strategy random -n 20
labctl burst <exp_id> --strategy bayesian -n 50
labctl burst <exp_id> --strategy perturb -n 30
```

**Guided** (in-session, approval per step): Like burst but pauses for your
approval/edit before each iteration. Use for early experiment design or when
you want to steer the search.
```bash
labctl guided <exp_id> --strategy perturb -n 10
# At each step: approve (Y), skip (n), or edit (custom JSON)
```

**Swarm** (multi-strategy, shared blackboard): Rotates through multiple
strategies. Each sees what others tried via SQLite blackboard. Use for
broad search across different optimization approaches.
```bash
labctl swarm <exp_id> --strategies random perturb bayesian -n 30
labctl swarm <exp_id> --strategies random perturb bayesian evolution -n 40
```

### When to use which strategy

| Strategy   | When to use                                              |
|------------|----------------------------------------------------------|
| `random`   | Initial exploration, no prior knowledge of the space     |
| `perturb`  | You have a good baseline and want to refine nearby       |
| `bayesian` | 10+ trials done, want smarter suggestions (needs optuna) |
| `evolution` | Large search space, population-based (needs nevergrad)  |
| `tree`      | AIDE-style tree search: branch new ideas or improve best |
| `llm`      | LLM reads SUMMARY.md/NEXT.md and proposes (cron mode)   |

### Search space

Every workspace that uses burst/guided/swarm needs a `search_space.json`:
```json
{
  "weight_decay": {"low": 0.1, "high": 10.0, "log": true, "type": "float"},
  "learning_rate": {"low": 1e-5, "high": 1e-2, "log": true, "type": "float"},
  "hidden_dim": {"low": 64, "high": 512, "type": "int"}
}
```
Flat params are auto-merged into nested configs (e.g. `weight_decay` finds
`config["training"]["weight_decay"]`).

See `docs/RUNNER-MODES.md` for full reference.

---

## 🤖 What this repo is

This repository is a generic autoresearch lab scaffold.

- The repo root holds code, templates, docs, and launchd config.
- The data root holds canonical experiment state.
- `python3 scripts/labctl.py` is the control surface.
- `LAB_MANIFEST.json` is the machine-readable harness manifest.

## 📋 Repo ingress

Read these in order when you are pointed at the repo root:

1. `README.md`
2. `LAB_MANIFEST.json`
3. `docs/AUTORESEARCH-COMPATIBILITY.md`
4. `docs/REFERENCE-EXECUTOR.md`
5. `docs/MULTI-FIDELITY.md`
6. `docs/LOCAL-AGENT-MUTATION.md`
7. `docs/DISPATCH.md`
8. `docs/OPERATIONS.md`

If a lab data root already exists, then read:

1. `README-FIRST.md`
2. `LAB-STATUS.md`
3. `LAB-INDEX.json`
4. `PROGRAM.md`
5. `experiments/<id>/RUNBOOK.md`
6. `experiments/<id>/SUMMARY.md`
7. `experiments/<id>/NEXT.md`

## 🧪 Main commands

```bash
python3 scripts/labctl.py init
python3 scripts/labctl.py create templates/autoresearch-generic.yaml
python3 scripts/labctl.py create templates/code-autoresearch.yaml
python3 scripts/labctl.py create templates/local-agent-autoresearch.yaml
python3 scripts/labctl.py create templates/multifidelity-autoresearch.yaml
python3 scripts/labctl.py create templates/research-sprint.yaml
python3 scripts/labctl.py run-once --max-runs 1
python3 scripts/labctl.py run-once --max-runs 1 --executor-class jetson-orin
python3 scripts/labctl.py dispatch-ready --max-runs 1
python3 scripts/labctl.py dispatch-claim --max-runs 1 --worker jetson-orin
python3 scripts/labctl.py dispatch-work --max-runs 1 --worker jetson-orin
python3 scripts/labctl.py dispatch-complete <dispatch_id>
python3 scripts/labctl.py dispatch-ingest [dispatch_id]
python3 scripts/labctl.py status
python3 scripts/labctl.py list
python3 scripts/labctl.py pause <exp_id>
python3 scripts/labctl.py resume <exp_id>
python3 scripts/labctl.py complete <exp_id>
python3 scripts/labctl.py set-fidelity <exp_id> <tier>
python3 scripts/labctl.py digest
python3 scripts/labctl.py weekly-digest
python3 scripts/labctl.py refresh
python3 scripts/labctl.py watchdog --repair
python3 scripts/labctl.py recover
# Targeted dispatch (ALWAYS use --experiment in cron jobs)
python3 scripts/labctl.py dispatch-agent-next --worker hermes --experiment <exp_id>
# Runner modes
python3 scripts/labctl.py burst <exp_id> --strategy random -n 20
python3 scripts/labctl.py guided <exp_id> --strategy perturb -n 10
python3 scripts/labctl.py swarm <exp_id> --strategies random perturb bayesian -n 30
```

## 🔐 Write rules

- Treat the data root as canonical state.
- Only write inside a claimed run bundle when executing an experiment.
- If you are working from `dispatch/running/<dispatch-id>/`, only write inside `run/` until completion.
- Do not hand-edit derived files unless you are intentionally rebuilding the reducer.
- `RESULT.md` is the write-ahead artifact.
- Use the runbook and spec to determine mutable vs read-only surfaces.

## 🧭 Generic experiment model

Each experiment can describe a generalized autoresearch loop through these SPEC fields:

- `goal`
- `metric`
- `metric_direction`
- `time_budget_minutes`
- `fidelity_tiers`
- `initial_fidelity_tier`
- `fidelity_promotion_rule`
- `promote_after_successes`
- `executor_class`
- `workspace_root`
- `setup_command`
- `baseline_command`
- `executor_command`
- `validation_command`
- `mutation_command`
- `agent_provider`
- `agent_model`
- `agent_effort`
- `agent_instruction_file`
- `agent_base_url`
- `agent_background`
- `acceptance_rule`
- `promotion_strategy`
- `workspace_mode`
- `require_clean_workspace`
- `mutable_paths`
- `read_only_paths`
- `ingress_files`
- `egress_files`
- `artifacts_expected`

The scheduler passes these into the executor environment so an external harness can act without reverse-engineering the repo.

If `mutation_command` is omitted and `agent_provider` is set, the lab synthesizes a local provider-agnostic mutation worker through `scripts/local_agent_mutation.py`.

For agent-managed or remote execution, use the dispatch protocol instead of writing directly into `experiments/<id>/runs/`. The queue package includes `dispatch.json`, an `input/` snapshot, and a writable `run/` folder that is ingested into canonical history only after completion.
