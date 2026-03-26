# Local Agent Mutation Router

_One local mutation contract, pluggable model providers_

---

## Why this exists

The lab treats local model-backed mutation as a higher-level contract than a raw provider command.

Instead of hardcoding a specific mutation command, you can describe the mutation worker with generic spec fields:

- `agent_provider`
- `agent_model`
- `agent_effort`
- `agent_instruction_file`
- `agent_base_url`
- `agent_background`

If `mutation_command` is omitted and `agent_provider` is set, the lab synthesizes:

`python3 scripts/local_agent_mutation.py`

## Supported providers

- `stub` — built-in, no credentials needed. Useful for dry runs and lab wiring.
- Custom adapters — drop a `scripts/<provider>_mutation_adapter.py` and it's auto-discovered.

## What the router does

`scripts/local_agent_mutation.py` reads the generic `agent_*` contract and delegates to a concrete provider adapter. It looks for `scripts/<provider>_mutation_adapter.py` and passes through model, effort, instruction, and other flags.

The reference executor and the rest of the lab do not need to change when you switch providers.

## Adding a custom provider

Create `scripts/<provider>_mutation_adapter.py` that accepts these CLI flags:

- `--model` — model name
- `--effort` — effort level
- `--instruction` — inline instruction text
- `--instruction-file` — path to instruction file
- `--base-url` — API base URL
- `--background` — run in background mode

The router will auto-discover it when `agent_provider` matches the filename prefix.

## Local-first usage

Start from `templates/local-agent-autoresearch.yaml`.

That template defaults to `agent_provider: stub` so the lab can run locally without credentials before you switch to a real provider.

Typical flow:

```bash
export HERMES_LAB_DATA_ROOT=~/lab-data

python3 scripts/labctl.py init
python3 scripts/labctl.py create templates/local-agent-autoresearch.yaml
python3 scripts/labctl.py run-once --max-runs 1
```

To switch to a custom provider, change only:

```yaml
agent_provider: my-provider
agent_model: my-model-name
```

And ensure `scripts/my-provider_mutation_adapter.py` exists.

## Artifacts

The router writes a provider-agnostic trace under:

- `artifacts/local-agent/selection.json`
- `artifacts/local-agent/command.txt`
- `artifacts/local-agent/stdout.log`
- `artifacts/local-agent/stderr.log`

Provider-specific adapters can write additional artifacts under their own directories.

## Design rule

This keeps the lab core stable:

1. the scheduler sees one local mutation abstraction
2. the executor still validates independently
3. provider changes stay at the edge
4. specs become easier for root-pointed agents to understand
