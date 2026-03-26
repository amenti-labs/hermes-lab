#!/usr/bin/env python3
"""Provider-agnostic local mutation adapter for Hermes Lab.

This wrapper keeps the lab contract stable while routing to a concrete local
provider adapter such as OpenAI or Claude.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name, str(default).lower()).strip().lower()
    return value in {"1", "true", "yes", "on"}


def write_text(path: Path, text: str) -> None:
    path.write_text(text)


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Lab local mutation router")
    parser.add_argument("--provider", default=env("LAB_AGENT_PROVIDER"))
    parser.add_argument("--model", default=env("LAB_AGENT_MODEL"))
    parser.add_argument("--effort", default=env("LAB_AGENT_EFFORT", "medium"))
    parser.add_argument("--instruction", default="")
    parser.add_argument("--instruction-file", default=env("LAB_AGENT_INSTRUCTION_FILE"))
    parser.add_argument("--base-url", default=env("LAB_AGENT_BASE_URL"))
    parser.add_argument("--background", action="store_true", default=env_bool("LAB_AGENT_BACKGROUND"))
    parser.add_argument("--extra-path", action="append", default=[])
    return parser


def artifacts_dir() -> Path:
    root = env("LAB_RUN_ARTIFACTS_DIR") or env("LAB_RUN_DIR") or os.getcwd()
    path = Path(root).expanduser().resolve() / "local-agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_stub(route_dir: Path, args: argparse.Namespace) -> int:
    payload = {
        "provider": "stub",
        "model": args.model or "stub",
        "effort": args.effort,
        "outcome": "noop",
        "summary": "Stub local agent produced no mutation.",
        "applied_files": [],
    }
    write_json(route_dir / "plan.json", payload)
    print(json.dumps(payload, indent=2))
    return 0


def provider_command(args: argparse.Namespace) -> list[str]:
    """Build a mutation command for the given provider.

    Custom adapters can be placed in scripts/<provider>_mutation_adapter.py.
    The router will find them automatically.
    """
    provider = args.provider.strip().lower()
    if provider == "stub":
        return []

    # Look for a provider-specific adapter script
    adapter = REPO_ROOT / "scripts" / f"{provider}_mutation_adapter.py"
    if not adapter.exists():
        raise RuntimeError(
            f"No mutation adapter found for provider '{args.provider}'. "
            f"Create scripts/{provider}_mutation_adapter.py or use provider=stub."
        )

    command = [sys.executable, str(adapter)]
    if args.model:
        command.extend(["--model", args.model])
    if args.effort:
        command.extend(["--effort", args.effort])
    if args.instruction:
        command.extend(["--instruction", args.instruction])
    if args.instruction_file:
        command.extend(["--instruction-file", args.instruction_file])
    if args.base_url:
        command.extend(["--base-url", args.base_url])
    if args.background:
        command.append("--background")
    for extra in args.extra_path:
        command.extend(["--extra-path", extra])
    return command


def main() -> int:
    args = build_parser().parse_args()
    provider = args.provider.strip().lower()
    if not provider:
        raise RuntimeError("Local agent provider is required. Set `agent_provider` in the spec or pass --provider.")

    route_dir = artifacts_dir()
    route_payload = {
        "provider": provider,
        "model": args.model,
        "effort": args.effort,
        "instruction_file": args.instruction_file,
        "base_url": args.base_url,
        "background": args.background,
        "extra_path": args.extra_path,
    }
    write_json(route_dir / "selection.json", route_payload)

    if provider == "stub":
        return run_stub(route_dir, args)

    command = provider_command(args)
    write_text(route_dir / "command.txt", " ".join(command) + "\n")
    completed = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    write_text(route_dir / "stdout.log", completed.stdout)
    write_text(route_dir / "stderr.log", completed.stderr)
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise
