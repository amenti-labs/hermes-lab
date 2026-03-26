# Local Agent Mutation Router

_One local mutation contract, multiple model providers_

---

## Why this exists

The lab now treats local model-backed mutation as a higher-level contract than a raw provider command.

Instead of hardcoding:

- Custom mutation commands via `executor_command` or `mutation_command`

you can describe the mutation worker with generic spec fields:

- `agent_provider`
- `agent_model`
- `agent_effort`
- `agent_instruction_file`
- `agent_base_url`
- `agent_background`

If `mutation_command` is omitted and `agent_provider` is set, the lab synthesizes:

`python3 scripts/local_agent_mutation.py`

## Supported providers

- `openai`
- `claude`
- `stub`

`stub` is useful for dry runs and lab wiring.

## What the router does

`scripts/local_agent_mutation.py` reads the generic `agent_*` contract and delegates to the concrete provider adapter locally.

Today that means:

- Provider routing is handled by `scripts/local_agent_mutation.py`

The reference executor and the rest of the lab do not need to change when you switch providers.

## Local-first usage

Start from `templates/local-agent-autoresearch.yaml`.

That template defaults to `agent_provider: stub` so the lab can run locally without credentials before you switch to a real provider.

Typical flow:

```bash
export HERMES_LAB_DATA_ROOT=~/lab-data
export OPENAI_API_KEY=...

python3 scripts/labctl.py init
python3 scripts/labctl.py create templates/local-agent-autoresearch.yaml
python3 scripts/labctl.py run-once --max-runs 1
```

To switch providers, keep the same template shape and change only:

```yaml
agent_provider: claude
agent_model: claude-sonnet-4-6
```

## Artifacts

The router writes a provider-agnostic trace under:

- `artifacts/local-agent/selection.json`
- `artifacts/local-agent/command.txt`
- `artifacts/local-agent/stdout.log`
- `artifacts/local-agent/stderr.log`

Provider-specific artifacts still appear under their existing directories such as:

- `artifacts/openai/*`
- `artifacts/claude/*`

## Design rule

This keeps the lab core stable:

1. the scheduler sees one local mutation abstraction
2. the executor still validates independently
3. provider changes stay at the edge
4. specs become easier for root-pointed agents to understand
