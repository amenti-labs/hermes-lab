# Claude Mutation Adapter

_Concrete Claude-backed mutation loop for Hermes Lab code experiments_

---

## What it is

`scripts/claude_mutation_adapter.py` is the Claude-specific worker behind the generic local mutation router.

It runs inside the sandbox workspace created by the reference executor, gathers a bounded set of editable and reference files, calls the Anthropic Messages API, requests a strict JSON edit plan, and applies the edits only inside the sandbox.

In normal local use, prefer the higher-level `agent_provider: claude` contract documented in `docs/LOCAL-AGENT-MUTATION.md`. Reach for the Claude-specific script directly only when you need low-level control.

The validator still decides whether the candidate is kept. Claude proposes; the validator scores; the executor promotes or rejects.

## Official guidance encoded into the adapter

This adapter follows Anthropic’s current guidance in a way that maps cleanly onto the existing autoresearch loop:

1. Use clear role + task framing in the system prompt.
2. Use structured output with a JSON schema instead of freeform code prose.
3. Use XML-style delimiters for complex context.
4. Keep state explicit and incremental across iterations.
5. Balance autonomy with safety by constraining the mutable surface.
6. Guard against overeagerness, excess file creation, and “passing tests by force”.

That is why the adapter returns a bounded file-rewrite plan rather than open-ended chat output.

## Best-practice defaults

- `claude-sonnet-4-6`
  The shipped default is a strong general coding model with reasonable latency/cost tradeoffs.
- `effort: medium`
  The adapter surfaces Anthropic’s effort control so the loop can choose a deeper or lighter reasoning budget without changing the orchestration contract.
- XML-style prompt structure
  Anthropic’s docs recommend XML tags for complex prompts. The adapter uses tagged sections for task, state, mutable paths, and file context.
- Strict schema output
  The request uses `output_config.format.type: json_schema`.
- Bounded edits
  `LAB_MUTABLE_PATHS` is required. Existing files may only be rewritten if their full contents were included in prompt context.
- Independent validator
  `validation_command` remains the promotion authority.
- Safe rollout
  Start with `promotion_strategy: patch-only`. Review several plan artifacts before switching to live promotion.

## Community heuristics from Reddit

These are not source-of-truth API guarantees, but they show up repeatedly in practitioner reports and align with the official docs:

- Persistent project instructions matter
  Multiple Reddit threads emphasize keeping a strong `CLAUDE.md` or equivalent project memory file. In Hermes Lab, that maps to `PROGRAM.md`, `RUNBOOK.md`, `SUMMARY.md`, and a workspace-local `.mutation.md`.
- Small scoped iterations outperform one giant ask
  Users repeatedly report better results when Claude is aimed at one constrained improvement loop instead of a broad refactor.
- External verification is essential
  Community feedback strongly reinforces what Anthropic’s docs imply: do not trust the model’s own confidence. Keep tests, benchmarks, or validators outside the model loop.
- Long sessions drift
  Reddit usage reports frequently mention that long Claude Code sessions can become less disciplined over time. The lab’s disk-first, run-bundle-first design is a direct countermeasure.
- Test-passing hacks are real
  Recent community examples show Claude sometimes “fixing” tests by altering test harness behavior instead of solving the underlying problem. That is why the adapter prompt explicitly warns against test-only hacks and why the validator must stay independent.

## Artifacts written

Under the run bundle:

- `artifacts/claude/prompt.md`
- `artifacts/claude/request.json`
- `artifacts/claude/response.json`
- `artifacts/claude/plan.json`
- `artifacts/claude/applied-files.txt`

This keeps the loop inspectable and cold-startable.

## Usage

Start from `templates/claude-autoresearch.yaml`.

Typical flow:

```bash
export ANTHROPIC_API_KEY=...
export HERMES_LAB_DATA_ROOT=~/lab-data

python3 scripts/labctl.py init
python3 scripts/labctl.py create templates/claude-autoresearch.yaml
python3 scripts/labctl.py run-once --max-runs 1
```

Inside the target workspace, create `.mutation.md` with the current brief. Example:

```md
Tighten the hot path in `src/score.py` without introducing broad architectural changes.
Prefer a minimal reversible patch.
Do not optimize solely to satisfy the current tests; preserve general behavior.
```

## Recommended operating pattern

1. Keep `mutable_paths` very small.
2. Keep `validation_command` model-independent.
3. Start with `patch-only`.
4. Review several `artifacts/claude/plan.json` files.
5. Only then switch to `apply-on-accept`.

## Sources

Official:
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
- https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- https://platform.claude.com/docs/en/build-with-claude/working-with-messages
- https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- https://claude.com/blog/how-anthropic-teams-use-claude-code

Reddit:
- https://www.reddit.com/r/Anthropic/comments/1npkc3b/feeling_overwhelmed_with_all_the_claude_code/
- https://www.reddit.com/r/ClaudeAI/comments/1p1vy31/i_finally_found_a_claude_code_workflow_that/
- https://www.reddit.com/r/ClaudeAI/comments/1mhgskk/claude_code_workflow_thats_been_working_well_for/
- https://www.reddit.com/r/ClaudeAI/comments/1oni040/this_one_prompt_reduced_my_claude_md_by_29/
- https://www.reddit.com/r/ClaudeCode/comments/1rug14a/claude_wrote_playwright_tests_that_secretly/
