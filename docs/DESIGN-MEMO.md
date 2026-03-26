# Hermes Lab Design Memo
## An Autonomous AI Lab That Any Agent Can Walk Into Cold

*2026-03-20 — design session*

---

## 1. Core Philosophy

**Calm over clever. Legible over powerful. Boring over exciting.**

The lab is not an agent swarm. It is a single-threaded research processor with clear state, bounded loops, and mandatory summaries. Any agent — cold, mid-session, or recovering from a crash — should read one file and know exactly what's happening, what happened last, and what to do next.

Design anchors:
- **Karpathy's autoresearch**: one metric, one editable surface, fixed time budgets, human writes strategy / agent executes. We adopt this separation wholesale.
- **Agent memory layering**: session logs, curated memory, knowledge graph. We extend this into experiment memory.
- **The 3 AM test**: if you wake up at 3 AM and check on the lab, can you understand its state in 30 seconds from your phone? If not, the design failed.

Three laws:
1. **No action without a written reason.** Every iteration logs *why* it chose what it chose.
2. **No unbounded loop.** Every task has max iterations, time caps, and stop conditions.
3. **No silent failure.** Errors escalate to digest. Digests escalate to human.

---

## 2. Harmonious Structure

```
$HERMES_LAB_DATA_ROOT/         ← single source of truth
├── LAB-STATUS.md                   ← THE file. Human-readable. Always current.
├── PROGRAM.md                      ← Human strategy doc (à la Karpathy)
│
├── inbox/                          ← Drop tasks here (human or agent)
├── queue/                          ← Normalized, waiting to run
├── active/                         ← Currently executing (≤3 concurrent)
├── paused/                         ← Human said "hold"
├── completed/                      ← Done, archived
│
├── results/
│   └── <task-id>/
│       ├── iteration-001.md        ← What happened, what was learned
│       ├── iteration-002.md
│       ├── best.md                 ← Current best result (symlink or copy)
│       └── metrics.jsonl           ← One JSON line per iteration
│
├── digests/
│   ├── 2026-03-20.md               ← Daily summary
│   └── weekly/
│       └── 2026-W12.md             ← Weekly rollup
│
├── state/
│   ├── tasks.json                  ← Task registry
│   ├── runs.json                   ← Run log
│   └── locks/                      ← Atomic mkdir locks
│
└── logs/
    ├── orchestrator.log            ← Append-only run log
    └── errors.log                  ← Errors only, easy to scan
```

### LAB-STATUS.md — The Heartbeat File

This is the most important file in the lab. Auto-generated after every cycle. Human-readable. Contains:

```markdown
# Hermes Lab Status
Updated: 2026-03-20T14:32:00Z

## Active (2)
- `qrng-lit-review` — iteration 7/20 — last: found 3 new papers on QRNG decoherence
- `land-price-model` — iteration 3/14 — last: baseline RMSE 0.34, trying county features

## Paused (1)
- `heart-coherence-survey` — paused: "wait for dataset"

## Queued (1)
- `esm-binding-benchmark` — priority: low — waiting

## Today's Headline
Best finding: qrng-lit-review surfaced a 2025 paper linking QRNG post-processing
to Bell inequality violations. Flagged for human review.

## Next Actions
- Run qrng-lit-review iteration 8 (deepening the decoherence thread)
- Run land-price-model iteration 4 (add county-level features)

## Alerts
- None
```

Any agent reads this file first. Period.

---

## 3. Agent Ingress / Egress

### Ingress Protocol (Cold Start)

Every agent session — whether it's a fresh Claude Code instance, a cron-triggered worker, or a human checking in — follows the same 4-step ingress:

```
1. Read  LAB-STATUS.md          → "What's happening right now?"
2. Read  PROGRAM.md             → "What's the human strategy?"
3. Read  active/<task>.yaml     → "What am I supposed to do?"
4. Read  results/<task>/best.md → "What's been tried? What worked?"
```

That's it. No 50-file context load. No "let me reconstruct what happened." Four reads, you're oriented.

### Egress Protocol (Session End)

Before any agent exits or times out:

```
1. Write iteration result to results/<task>/iteration-NNN.md
2. Append metric line to results/<task>/metrics.jsonl
3. Update state/tasks.json with new iteration count + status
4. Regenerate LAB-STATUS.md
5. If noteworthy: append to digests/<today>.md
```

Rule: **never leave the lab in a state where LAB-STATUS.md is stale.** If you crash before step 4, the next agent detects the stale timestamp and runs recovery.

### Recovery Protocol

If `LAB-STATUS.md` timestamp is >2x the expected cadence:
1. Check `state/tasks.json` for last known state
2. Check `results/<task>/` for the last written iteration
3. Reconcile: if iteration file exists but state wasn't updated, fix state
4. Regenerate `LAB-STATUS.md`
5. Log the recovery event to `logs/errors.log`
6. Continue normally

---

## 4. Memory Layers

Three layers, each with a different half-life:

| Layer | Location | Half-life | Purpose |
|-------|----------|-----------|---------|
| **Hot** | `LAB-STATUS.md` + `active/<task>.yaml` | Minutes | What's happening now |
| **Warm** | `results/<task>/` + `digests/` | Days–weeks | What was learned |
| **Cold** | `completed/` + `digests/weekly/` | Months | Institutional knowledge |

### Within each task: iteration memory

Every `iteration-NNN.md` follows a fixed template:

```markdown
# <task-id> — Iteration NNN
Timestamp: <ISO>
Worker: <role>
Duration: <seconds>

## Hypothesis
What I expected to find or test.

## Method
What I actually did (search terms, API calls, model changes).

## Result
What happened. Metric: <value>.

## Interpretation
What this means for the overall goal.

## Next
What the next iteration should try, and why.
```

The `## Next` section is critical. It's the relay baton. The next agent reads *only* the last iteration's `## Next` plus `best.md` to decide what to do. This prevents context window bloat while preserving research direction.

### Metric tracking

`metrics.jsonl` — one line per iteration:
```json
{"iteration": 7, "metric": "papers_found", "value": 3, "secondary": {"relevance_avg": 0.82}, "ts": "2026-03-20T14:32:00Z"}
```

Simple. Plottable. Diffable. The metric name is defined in the task YAML.

---

## 5. Experiment Lifecycle

### Task Definition (YAML)

```yaml
id: qrng-lit-review
mode: research-sprint
goal: Map the landscape of QRNG applications in consciousness research
metric: relevant_papers_found
constraints:
  - prefer peer-reviewed over preprints
  - exclude pure-engineering QRNG papers (focus on consciousness/cognition)
cadence: every-2-hours
max_iterations_total: 20
max_iterations_per_run: 2
time_budget_minutes: 10
stop_conditions:
  - plateau_after_consecutive_low_signal_runs: 3
  - hard_stop_after_days: 14
escalate_when:
  - breakthrough_found
  - blocked_on_external_access
  - metric_regression_3_consecutive
outputs:
  - daily_digest
  - final_ranked_memo
worker_roles:
  - scout
  - researcher
  - critic
priority: high
```

### State Machine

```
inbox → queue → active ←→ paused
                  ↓
              completed
```

Transitions:
- `inbox → queue`: orchestrator normalizes and validates
- `queue → active`: orchestrator picks by priority, respects max concurrent (3)
- `active → paused`: human says hold, or escalation condition met
- `paused → active`: human says resume
- `active → completed`: stop condition met or max iterations reached
- `active → active`: normal iteration (most common transition)

### Worker Role Rotation

Within a multi-role task, roles rotate per iteration:

```
iteration 1: scout      → broad search, find sources
iteration 2: scout      → continue if coverage < threshold
iteration 3: researcher → deepen top 3 threads
iteration 4: researcher → deepen remaining threads
iteration 5: critic     → attack assumptions, find gaps
iteration 6: synthesizer → compress into ranked findings
iteration 7: scout      → new search informed by gaps
...
```

The rotation schedule is defined in the task YAML or defaults to a sensible cycle. The key insight: **critics run regularly, not just at the end.** Early criticism saves wasted iterations.

---

## 6. Daily Rhythm

### Automated (launchd/cron)

| Time | Action | Script |
|------|--------|--------|
| Every 30min | `run-once` (process ≤3 tasks) | `scripts/run-cycle.sh` |
| 06:00 | Daily digest | `scripts/write-digest.sh` |
| 06:05 | Push digest to agent chat | via heartbeat or cron |
| Sunday 06:00 | Weekly rollup | `scripts/weekly-digest.sh` |

### Human Touchpoints

The human interacts through the agent chat with natural language:

```
"pause the land model, I want to rethink the features"
"add a new sprint: survey ESM3 protein binding benchmarks"
"show me only breakthroughs from this week"
"tighten the lit review — only Nature/Science/PNAS"
"kill low-priority tasks, focus everything on QRNG"
```

These translate to `labctl.py` commands. The agent is the translator, not the executor.

### The Morning Read

Every morning at 06:00, the digest lands. It's structured for phone reading:

```markdown
# Hermes Lab — Friday March 20

## Headlines (read this only if busy)
- qrng-lit-review: Found decoherence paper. Possibly significant. ★
- land-price-model: RMSE improved 12% with county features.

## Details (read if you have 5 min)
[expandable sections per task]

## Decisions Needed
- qrng-lit-review wants to pivot to decoherence subfield. Approve? [yes/no]
- heart-coherence-survey still paused. Resume or drop?

## Lab Health
- 47 iterations run today, 0 errors
- SSD: 234 GB free
- Next scheduled: land-price-model iteration 5 at 06:30
```

---

## 7. Failure Containment

### Principle: Blast Radius = One Iteration

A failed iteration never corrupts:
- Other tasks
- The task's previous results
- LAB-STATUS.md (regenerated from state, not from the failed iteration)
- The queue or scheduling

### Specific failure modes

| Failure | Response |
|---------|----------|
| Agent crashes mid-iteration | Next agent detects stale timestamp, runs recovery protocol |
| API rate limit | Log, skip iteration, try next cycle. Don't retry in a loop. |
| SSD disconnected | Orchestrator detects missing data root, halts all work, alerts digest |
| Task produces garbage | Critic role catches in next rotation. Metric regression triggers escalation. |
| Infinite loop in worker | `time_budget_minutes` enforced via wall-clock timeout on worker subprocess |
| All tasks complete | Lab goes idle. LAB-STATUS.md says "All clear. Waiting for new tasks." |
| Conflicting edits | Atomic `mkdir` locks in `state/locks/`. Second writer waits or skips. |

### Escalation Chain

```
Iteration fails → log to errors.log
3 consecutive fails → flag in LAB-STATUS.md
5 consecutive fails → pause task automatically
Paused task → appears in daily digest "Decisions Needed"
Human decides → resume with new constraints, or drop
```

### Data Safety

- All writes go to the data root (`$HERMES_LAB_DATA_ROOT/`)
- Results are append-only (iterations never overwrite)
- `metrics.jsonl` is append-only
- State files use atomic write (write to `.tmp`, rename)
- Weekly: `rsync` snapshot to Mac mini local disk as backup

---

## 8. PROGRAM.md — The Human Strategy Doc

Borrowed directly from Karpathy's insight: **the human programs the strategy, the agent executes the tactics.**

```markdown
# Hermes Lab Program

## Research Direction
We're exploring QRNG applications in consciousness research,
with a secondary thread on embodied cognition and heart-based intelligence.

## What Good Looks Like
- Papers that connect quantum randomness to measurable cognitive phenomena
- Benchmarkable claims (not just philosophy)
- Surprising connections between fields

## What to Avoid
- Pure engineering papers about random number generation
- Speculative consciousness papers with no empirical grounding
- Anything we've already covered (check results/)

## Current Priorities
1. QRNG decoherence thread (high — potential breakthrough)
2. Land price modeling (medium — practical value)
3. ESM3 benchmarks (low — background scan)

## Standing Rules
- Never spend more than 10 minutes on a single iteration
- Always cite sources with URLs
- If you find something exciting, say so clearly in the iteration summary
- When in doubt, be conservative. A missed finding can be caught later.
  A false positive wastes human attention.
```

This file is the single most important lever for the human. Updating PROGRAM.md changes the lab's direction without touching code.

---

## 9. Future: PC Worker

When the PC comes online:

```
Mac mini (orchestrator)  ←→  PC (worker)
   ↓                           ↓
 scheduling                  inference
 digests                     embeddings
 API calls                   batch experiments
 Agent                       overnight GPU runs
```

Communication: shared filesystem (SSD or network mount) + task claiming via atomic locks. No message bus. No RPC. Just files.

The PC reads the same `LAB-STATUS.md`, claims tasks tagged `gpu: true`, writes results to the same `results/` tree. The orchestrator doesn't care who ran the iteration — it just reads the output.

---

## 10. Recommended Final Plan

### Phase 1: Foundation (this week)
- [ ] Add `LAB-STATUS.md` generation to `write_digest` / post-cycle hook
- [ ] Add `PROGRAM.md` to data root with initial strategy
- [ ] Enforce iteration template in `run_once` output
- [ ] Add `metrics.jsonl` append to iteration writes
- [ ] Add `time_budget_minutes` enforcement (subprocess timeout)
- [ ] Add recovery detection (stale timestamp check)
- [ ] Add atomic writes for state files

### Phase 2: Worker Roles (next week)
- [ ] Implement worker role dispatch (scout/researcher/critic/synthesizer)
- [ ] Wire roles to actual Claude Code agent calls
- [ ] Add role rotation logic per task config
- [ ] Add escalation logic (consecutive failures → pause)

### Phase 3: Daily Rhythm (week after)
- [ ] Set up launchd plists for `run-cycle` (30min) and `digest` (daily)
- [ ] Build digest format with Headlines / Details / Decisions Needed
- [ ] Wire digest delivery to agent chat
- [ ] Add weekly rollup script

### Phase 4: Harden (ongoing)
- [ ] SSD health check in pre-cycle
- [ ] Backup rsync to local disk
- [ ] Disk space monitoring in digest
- [ ] Dry-run mode for testing task configs without executing

### Not Now (future)
- PC worker integration
- Web dashboard
- Multi-lab federation
- Custom metric visualization

---

## Summary

The whole system fits in your head:

> Tasks flow left to right: **inbox → queue → active → completed**.
> Each iteration writes a result and updates the scoreboard.
> LAB-STATUS.md is always current.
> The daily digest tells you what matters.
> PROGRAM.md tells the lab what you care about.
> Any agent reads 4 files and knows everything.

That's it. No framework. No database. No microservices. Just files, discipline, and a clear protocol.
