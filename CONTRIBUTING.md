# Contributing to Hermes Lab

Thanks for your interest in contributing!

## Getting Started

1. Fork the repo and clone it locally.
2. Create a branch for your changes.
3. Run the tests: `python3 -m pytest tests/`
4. Submit a pull request.

## Project Structure

- `lab/` - Core runtime. Changes here need tests.
- `scripts/` - CLI and executors. `labctl.py` is the main entry point.
- `templates/` - SPEC templates. Add new experiment types here.
- `docs/` - Architecture and operations documentation.
- `tests/` - Test suite.

## Guidelines

- Keep the core file-first. No databases, no servers, no required services.
- New features should work without optional dependencies.
- Templates should be generic enough for anyone to adapt.
- Agent-facing surfaces (AGENTS.md, LAB_MANIFEST.json) are API contracts -- change carefully.
- Document new SPEC fields in `docs/AUTORESEARCH-COMPATIBILITY.md`.

## Adding a New Strategy

1. Add the strategy function to `lab/strategies.py`.
2. Register it in the strategy map.
3. Add tests in `tests/test_strategies.py`.
4. Document it in `docs/RUNNER-MODES.md`.

## Adding a New Mutation Adapter

1. Create `scripts/<provider>_mutation_adapter.py`.
2. Follow the pattern in `scripts/openai_mutation_adapter.py`.
3. Create a matching template in `templates/`.
4. Document it in `docs/`.

## Reporting Issues

Include:
- What you expected to happen
- What actually happened
- Your Python version and OS
- The labctl command you ran
- Relevant files from your data root (SPEC.yaml, STATUS.json, error logs)
