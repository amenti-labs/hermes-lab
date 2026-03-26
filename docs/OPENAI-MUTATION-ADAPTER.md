# OpenAI Mutation Adapter

_Concrete model-backed mutation loop for Hermes Lab code experiments_

---

## What it is

`scripts/openai_mutation_adapter.py` is the OpenAI-specific worker behind the generic local mutation router.

It runs inside the sandbox workspace created by the reference executor, gathers a bounded set of editable and reference files, calls the OpenAI Responses API, forces the model to return a strict JSON edit plan, and applies the edits only inside the sandbox.

In normal local use, prefer the higher-level `agent_provider: openai` contract documented in `docs/LOCAL-AGENT-MUTATION.md`. Reach for the OpenAI-specific script directly only when you need low-level control.

The external validator still decides whether the candidate is kept. The model proposes; the validator scores; the executor promotes or rejects.

## Why this shape

This adapter follows the current OpenAI and autoresearch-style guidance closely:

1. Use the Responses API for new agentic workflows.
2. Use strict structured outputs instead of freeform code prose.
3. Give the model an explicit goal, constraints, and success condition.
4. Keep the mutable surface narrow and the evaluator independent.
5. Store the lab’s canonical traces locally instead of depending on hidden conversation state.

That is why the adapter returns a JSON file-rewrite plan instead of a chatty patch explanation.

## Best-practice defaults

- `store: false`
  The lab already writes prompt, response, and plan artifacts into the run bundle, so the adapter disables default response storage.
- Strict JSON schema
  The request uses `text.format` with `type: json_schema` and `strict: true`.
- Bounded edits
  `LAB_MUTABLE_PATHS` is required. Existing files may only be rewritten if their full contents were included in prompt context.
- Small context by default
  Editable files are included in full. Read-only context may be truncated. Large editable files are omitted rather than partially exposed.
- Independent validator
  `validation_command` remains the source of truth for promotion. This matches the keep-or-reject discipline from autoresearch.
- Safe rollout
  Start with `promotion_strategy: patch-only`. Move to `apply-on-accept` only after the validator is trustworthy.

## API and prompt guidance used

### Responses API

OpenAI’s migration guide recommends the Responses API for new work and notes that Responses are stored by default unless `store: false` is set. The adapter follows that guidance directly.

### Structured outputs

OpenAI’s structured outputs guide moves schema-constrained output to `text.format` in the Responses API and recommends strict schemas plus explicit handling for refusals, incomplete outputs, and user inputs that do not fit the schema.

### Reasoning and coding prompts

The reasoning best-practices guide says to avoid chain-of-thought prompting, use delimiters, try zero-shot first, provide specific guidelines, and be explicit about the end goal. The Codex prompting guide reinforces the same pattern for software engineering agents: deliver working code, gather only relevant context, and operate autonomously inside clear constraints.

### Evaluation discipline

OpenAI’s evaluation guidance emphasizes representative evals and combining automated checks with human review where needed. In Hermes Lab, that maps to:

- `validation_command` for automated scoring
- `patch-only` for reviewable dry runs
- `apply-on-accept` only after the validator and workspace contract are proven

### Production hygiene

OpenAI’s production guidance recommends keeping API keys out of code, using environment variables or secret managers, and separating staging and production projects. Use one project/key for experimentation and a different one for unattended production runs.

### Background mode

For long-running generations, OpenAI documents `background: true` plus polling on the response ID. The adapter exposes `--background` and `--poll-interval-seconds`, but synchronous mode is the default because most mutation plans should be short and bounded.

## Artifacts written

Under the run bundle:

- `artifacts/openai/prompt.md`
- `artifacts/openai/request.json`
- `artifacts/openai/response.json`
- `artifacts/openai/plan.json`
- `artifacts/openai/applied-files.txt`

This makes the mutation proposal legible after the fact and keeps the run cold-startable.

## Usage

Start from `templates/openai-codex-autoresearch.yaml`.

Typical flow:

```bash
export OPENAI_API_KEY=...
export HERMES_LAB_DATA_ROOT=~/lab-data

python3 scripts/labctl.py init
python3 scripts/labctl.py create templates/openai-codex-autoresearch.yaml
python3 scripts/labctl.py run-once --max-runs 1
```

Inside the target workspace, create `.mutation.md` with the current brief. Example:

```md
Tighten the hot path in `src/score.py` without broad refactors.
Prioritize faster validation time and preserve existing external behavior.
If the evidence is weak, choose a no-op over a risky rewrite.
```

## Recommended operating pattern

1. Keep `mutable_paths` very small.
2. Keep `validation_command` model-independent.
3. Start with `patch-only`.
4. Review several `artifacts/openai/plan.json` files.
5. Only then switch to `apply-on-accept`.

## Sources

- https://developers.openai.com/api/docs/guides/migrate-to-responses
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/guides/reasoning-best-practices
- https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide
- https://developers.openai.com/api/docs/guides/evaluation-best-practices
- https://developers.openai.com/api/docs/guides/production-best-practices
- https://developers.openai.com/api/docs/guides/background
