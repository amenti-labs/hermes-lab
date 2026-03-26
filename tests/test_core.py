from __future__ import annotations

import os
import json
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import threading
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lab.core import (
    acquire_lease,
    claim_dispatch,
    create_experiment,
    dispatch_agent_next,
    dispatch_agent_submit,
    dispatch_work,
    get_paths,
    get_status,
    ingest_dispatch,
    now_utc,
    queue_dispatch,
    resolved_mutation_command,
    run_once,
    save_json,
    save_text,
    set_fidelity_tier,
    mark_dispatch_complete,
    watchdog,
    write_digest,
    write_lab_status,
    write_weekly_digest,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeOpenAIServer:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                parent.requests.append({"method": "POST", "path": self.path, "payload": payload})
                if not parent.responses:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error":"no response queued"}')
                    return
                response = parent.responses.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode("utf-8"))

            def do_GET(self) -> None:  # noqa: N802
                parent.requests.append({"method": "GET", "path": self.path, "payload": None})
                if not parent.responses:
                    self.send_response(404)
                    self.end_headers()
                    return
                response = parent.responses.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


class FakeAnthropicServer:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                parent.requests.append({"method": "POST", "path": self.path, "payload": payload})
                if not parent.responses:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error":"no response queued"}')
                    return
                response = parent.responses.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


class HermesLabSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name) / "lab-data"
        self.previous_root = os.environ.get("HERMES_LAB_DATA_ROOT")
        self.previous_openai_api_key = os.environ.get("OPENAI_API_KEY")
        self.previous_openai_base_url = os.environ.get("OPENAI_BASE_URL")
        self.previous_anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.previous_anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL")
        os.environ["HERMES_LAB_DATA_ROOT"] = str(self.root)
        self.paths = get_paths(create=True)

    def tearDown(self) -> None:
        if self.previous_root is None:
            os.environ.pop("HERMES_LAB_DATA_ROOT", None)
        else:
            os.environ["HERMES_LAB_DATA_ROOT"] = self.previous_root
        if self.previous_openai_api_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.previous_openai_api_key
        if self.previous_openai_base_url is None:
            os.environ.pop("OPENAI_BASE_URL", None)
        else:
            os.environ["OPENAI_BASE_URL"] = self.previous_openai_base_url
        if self.previous_anthropic_api_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self.previous_anthropic_api_key
        if self.previous_anthropic_base_url is None:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        else:
            os.environ["ANTHROPIC_BASE_URL"] = self.previous_anthropic_base_url
        self._tmpdir.cleanup()

    def make_spec(self, exp_id: str) -> Path:
        template = (REPO_ROOT / "templates" / "research-sprint.yaml").read_text()
        spec_text = template.replace("example-research-sprint", exp_id)
        spec_path = Path(self._tmpdir.name) / f"{exp_id}.yaml"
        spec_path.write_text(spec_text)
        return spec_path

    def make_executor_class_spec(self, exp_id: str, executor_class: str) -> Path:
        spec_text = textwrap.dedent(
            f"""
            id: {exp_id}
            mode: autoresearch-loop
            goal: Verify executor class filtering
            metric: placeholder_metric
            metric_direction: maximize
            priority: normal
            autonomous: true
            cadence: every-30-minutes
            time_budget_minutes: 10
            max_iterations_total: 3
            max_iterations_per_run: 1
            executor_class: {executor_class}
            worker_roles:
              - researcher
            """
        ).strip() + "\n"
        spec_path = Path(self._tmpdir.name) / f"{exp_id}.yaml"
        spec_path.write_text(spec_text)
        return spec_path

    def make_code_spec(
        self,
        exp_id: str,
        *,
        workspace_root: Path,
        mutation_command: str,
        promotion_strategy: str = "apply-on-accept",
    ) -> Path:
        spec_text = textwrap.dedent(
            f"""
            id: {exp_id}
            mode: autoresearch-code
            goal: Improve a numeric score in a git workspace
            metric: validation_score
            metric_direction: maximize
            priority: normal
            autonomous: true
            cadence: every-30-minutes
            time_budget_minutes: 10
            max_iterations_total: 5
            max_iterations_per_run: 1
            workspace_root: {workspace_root}
            executor_command: python3 scripts/reference_executor.py
            baseline_command: python3 validate.py
            mutation_command: {mutation_command}
            validation_command: python3 validate.py
            acceptance_rule: Keep only strict improvements
            promotion_strategy: {promotion_strategy}
            workspace_mode: git-clone
            require_clean_workspace: true
            mutable_paths:
              - state.txt
            read_only_paths:
              - validate.py
              - mutate.py
            worker_roles:
              - researcher
            """
        ).strip() + "\n"
        spec_path = Path(self._tmpdir.name) / f"{exp_id}.yaml"
        spec_path.write_text(spec_text)
        return spec_path

    def make_local_agent_code_spec(
        self,
        exp_id: str,
        *,
        workspace_root: Path,
        provider: str,
        model: str,
        base_url: str = "",
        promotion_strategy: str = "apply-on-accept",
    ) -> Path:
        base_url_line = f"agent_base_url: {base_url}\n" if base_url else ""
        spec_text = textwrap.dedent(
            f"""
            id: {exp_id}
            mode: autoresearch-local-agent
            goal: Improve a numeric score in a git workspace through a local agent router
            metric: validation_score
            metric_direction: maximize
            priority: normal
            autonomous: true
            cadence: every-30-minutes
            time_budget_minutes: 10
            max_iterations_total: 5
            max_iterations_per_run: 1
            workspace_root: {workspace_root}
            executor_command: python3 scripts/reference_executor.py
            baseline_command: python3 validate.py
            agent_provider: {provider}
            agent_model: {model}
            agent_effort: medium
            agent_instruction_file: .mutation.md
            """
        )
        spec_text += base_url_line
        spec_text += textwrap.dedent(
            f"""
            validation_command: python3 validate.py
            acceptance_rule: Keep only strict improvements
            promotion_strategy: {promotion_strategy}
            workspace_mode: git-clone
            require_clean_workspace: true
            mutable_paths:
              - state.txt
            read_only_paths:
              - validate.py
            worker_roles:
              - researcher
            """
        ).strip() + "\n"
        spec_path = Path(self._tmpdir.name) / f"{exp_id}.yaml"
        spec_path.write_text(spec_text)
        return spec_path

    def make_multitier_code_spec(
        self,
        exp_id: str,
        *,
        workspace_root: Path,
        promotion_rule: str = "manual",
    ) -> Path:
        spec_text = textwrap.dedent(
            f"""
            id: {exp_id}
            mode: autoresearch-code
            goal: Improve a numeric score in a git workspace with proxy and final tiers
            metric: validation_score
            metric_direction: maximize
            priority: normal
            autonomous: true
            cadence: 0
            time_budget_minutes: 10
            max_iterations_total: 6
            max_iterations_per_run: 1
            fidelity_tiers:
              - proxy
              - final
            initial_fidelity_tier: proxy
            fidelity_promotion_rule: {promotion_rule}
            promote_after_successes: 1
            executor_command: python3 scripts/reference_executor.py
            workspace_root: {workspace_root}
            validation_command: python3 validate.py
            workspace_mode: git-clone
            require_clean_workspace: true
            proxy_executor_class: jetson-orin
            final_executor_class: cloud-h100
            proxy_mutation_command: python3 mutate_proxy.py
            final_mutation_command: python3 mutate_final.py
            proxy_promotion_strategy: patch-only
            final_promotion_strategy: apply-on-accept
            mutable_paths:
              - state.txt
            read_only_paths:
              - validate.py
              - mutate_proxy.py
              - mutate_final.py
            worker_roles:
              - researcher
            """
        ).strip() + "\n"
        spec_path = Path(self._tmpdir.name) / f"{exp_id}.yaml"
        spec_path.write_text(spec_text)
        return spec_path

    def make_git_workspace(self, *, initial_value: int, mutated_value: int) -> Path:
        if shutil.which("git") is None:
            self.skipTest("git is required for git-backed executor tests")

        workspace = Path(self._tmpdir.name) / f"workspace-{initial_value}-{mutated_value}"
        workspace.mkdir()
        (workspace / "state.txt").write_text(f"{initial_value}\n")
        (workspace / "validate.py").write_text(
            textwrap.dedent(
                """
                from pathlib import Path
                print(Path("state.txt").read_text().strip())
                """
            ).strip()
            + "\n"
        )
        (workspace / "mutate.py").write_text(
            textwrap.dedent(
                f"""
                from pathlib import Path
                Path("state.txt").write_text("{mutated_value}\\n")
                """
            ).strip()
            + "\n"
        )
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Hermes Lab Tests"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=workspace, check=True, capture_output=True, text=True)
        return workspace

    def make_multitier_git_workspace(self, *, initial_value: int, proxy_value: int, final_value: int) -> Path:
        if shutil.which("git") is None:
            self.skipTest("git is required for git-backed executor tests")

        workspace = Path(self._tmpdir.name) / f"workspace-{initial_value}-{proxy_value}-{final_value}"
        workspace.mkdir()
        (workspace / "state.txt").write_text(f"{initial_value}\n")
        (workspace / "validate.py").write_text(
            textwrap.dedent(
                """
                from pathlib import Path
                print(Path("state.txt").read_text().strip())
                """
            ).strip()
            + "\n"
        )
        (workspace / "mutate_proxy.py").write_text(
            textwrap.dedent(
                f"""
                from pathlib import Path
                Path("state.txt").write_text("{proxy_value}\\n")
                """
            ).strip()
            + "\n"
        )
        (workspace / "mutate_final.py").write_text(
            textwrap.dedent(
                f"""
                from pathlib import Path
                Path("state.txt").write_text("{final_value}\\n")
                """
            ).strip()
            + "\n"
        )
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Hermes Lab Tests"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=workspace, check=True, capture_output=True, text=True)
        return workspace

    def fake_openai_response(self, plan: dict[str, object]) -> dict[str, object]:
        return {
            "id": "resp_test_123",
            "status": "completed",
            "output_text": json.dumps(plan),
        }

    def fake_anthropic_response(self, plan: dict[str, object]) -> dict[str, object]:
        return {
            "id": "msg_test_123",
            "type": "message",
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(plan)}],
        }

    def test_full_cycle_builds_run_bundle_and_projections(self) -> None:
        create_experiment(self.paths, self.make_spec("smoke-exp"))

        messages = run_once(self.paths, max_runs=1)
        self.assertEqual(len(messages), 1)

        status = get_status(self.paths, "smoke-exp")
        self.assertIsNotNone(status)
        self.assertEqual(status["run_count"], 1)
        self.assertEqual(status["phase"], "active")

        exp_dir = self.paths.experiments / "smoke-exp"
        run_dirs = list((exp_dir / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        self.assertTrue((run_dirs[0] / "manifest.json").exists())
        self.assertTrue((run_dirs[0] / "RESULT.md").exists())
        self.assertTrue((run_dirs[0] / "metrics.json").exists())
        self.assertTrue((run_dirs[0] / "plan.md").exists())
        self.assertTrue((exp_dir / "SUMMARY.md").exists())
        self.assertTrue((exp_dir / "NEXT.md").exists())
        self.assertTrue((exp_dir / "RUNBOOK.md").exists())
        self.assertTrue((exp_dir / "context.md").exists())
        self.assertTrue((exp_dir / "best.md").exists())
        self.assertTrue((exp_dir / "checkpoints" / "latest-run.txt").exists())
        self.assertTrue((exp_dir / "checkpoints" / "best-run.txt").exists())
        self.assertTrue((run_dirs[0] / "artifacts").exists())

        write_lab_status(self.paths)
        self.assertTrue(self.paths.lab_index_json.exists())
        index = json.loads(self.paths.lab_index_json.read_text())
        self.assertEqual(index["experiments"][0]["id"], "smoke-exp")
        self.assertEqual(index["experiments"][0]["current_fidelity_tier"], "default")

        daily = write_digest(self.paths)
        weekly = write_weekly_digest(self.paths)
        self.assertTrue(daily.exists())
        self.assertTrue(weekly.exists())

    def test_watchdog_reclaims_expired_leases(self) -> None:
        create_experiment(self.paths, self.make_spec("lease-exp"))
        lease = acquire_lease(self.paths, "lease-exp", owner="test", ttl_seconds=300)

        info_path = self.paths.locks / "lease-exp.lock" / "info.json"
        data = info_path.read_text()
        self.assertIn(lease["lease_id"], data)

        expired = dict(lease)
        expired["expires_at"] = (now_utc() - timedelta(minutes=5)).isoformat()
        save_json(info_path, expired)

        report = watchdog(self.paths, repair=True)
        self.assertIn("lease-exp", report["reclaimed_leases"])
        self.assertFalse((self.paths.locks / "lease-exp.lock").exists())

        status = get_status(self.paths, "lease-exp")
        self.assertIsNotNone(status)
        self.assertIsNone(status["current_lease"])

    def test_reference_executor_applies_patch_on_improve(self) -> None:
        workspace = self.make_git_workspace(initial_value=1, mutated_value=2)
        create_experiment(
            self.paths,
            self.make_code_spec(
                "code-accept",
                workspace_root=workspace,
                mutation_command="python3 mutate.py",
                promotion_strategy="apply-on-accept",
            ),
        )

        messages = run_once(self.paths, max_runs=1)
        self.assertEqual(len(messages), 1)

        status = get_status(self.paths, "code-accept")
        self.assertIsNotNone(status)
        self.assertEqual(status["best_metric_value"], 2.0)
        self.assertEqual((workspace / "state.txt").read_text().strip(), "2")

        run_dir = next((self.paths.experiments / "code-accept" / "runs").iterdir())
        decision = (run_dir / "artifacts" / "decision.json").read_text()
        self.assertIn('"accepted": true', decision)
        self.assertIn('"applied_to_original": true', decision)
        self.assertTrue((run_dir / "artifacts" / "diff.patch").exists())

    def test_reference_executor_rejects_non_improvement(self) -> None:
        workspace = self.make_git_workspace(initial_value=1, mutated_value=0)
        create_experiment(
            self.paths,
            self.make_code_spec(
                "code-reject",
                workspace_root=workspace,
                mutation_command="python3 mutate.py",
                promotion_strategy="apply-on-accept",
            ),
        )

        messages = run_once(self.paths, max_runs=1)
        self.assertEqual(len(messages), 1)

        status = get_status(self.paths, "code-reject")
        self.assertIsNotNone(status)
        self.assertEqual(status["best_metric_value"], 0.0)
        self.assertEqual((workspace / "state.txt").read_text().strip(), "1")

        run_dir = next((self.paths.experiments / "code-reject" / "runs").iterdir())
        decision = (run_dir / "artifacts" / "decision.json").read_text()
        self.assertIn('"accepted": false', decision)
        self.assertIn('"applied_to_original": false', decision)

    def test_set_fidelity_tier_switches_tier_specific_commands(self) -> None:
        workspace = self.make_multitier_git_workspace(initial_value=1, proxy_value=2, final_value=5)
        create_experiment(
            self.paths,
            self.make_multitier_code_spec(
                "multitier-manual",
                workspace_root=workspace,
                promotion_rule="manual",
            ),
        )

        first_messages = run_once(self.paths, max_runs=1)
        self.assertEqual(len(first_messages), 1)
        self.assertEqual((workspace / "state.txt").read_text().strip(), "1")

        status = get_status(self.paths, "multitier-manual")
        self.assertIsNotNone(status)
        self.assertEqual(status["current_fidelity_tier"], "proxy")
        self.assertEqual(status["executor_class"], "jetson-orin")
        self.assertEqual(status["run_count_by_tier"]["proxy"], 1)
        self.assertEqual(status["best_metric_by_tier"]["proxy"], 2.0)

        set_fidelity_tier(self.paths, "multitier-manual", "final", reason="promote to finalist runs")
        second_messages = run_once(self.paths, max_runs=1)
        self.assertEqual(len(second_messages), 1)
        self.assertEqual((workspace / "state.txt").read_text().strip(), "5")

        status = get_status(self.paths, "multitier-manual")
        self.assertIsNotNone(status)
        self.assertEqual(status["current_fidelity_tier"], "final")
        self.assertEqual(status["executor_class"], "cloud-h100")
        self.assertEqual(status["run_count_by_tier"]["final"], 1)
        self.assertEqual(status["best_metric_by_tier"]["proxy"], 2.0)
        self.assertEqual(status["best_metric_by_tier"]["final"], 5.0)

        run_dirs = sorted((self.paths.experiments / "multitier-manual" / "runs").iterdir())
        self.assertEqual(len(run_dirs), 2)
        first_manifest = json.loads((run_dirs[0] / "manifest.json").read_text())
        second_manifest = json.loads((run_dirs[1] / "manifest.json").read_text())
        self.assertEqual(first_manifest["fidelity_tier"], "proxy")
        self.assertEqual(first_manifest["executor_class"], "jetson-orin")
        self.assertEqual(second_manifest["fidelity_tier"], "final")
        self.assertEqual(second_manifest["executor_class"], "cloud-h100")

    def test_auto_promotes_fidelity_after_success_streak(self) -> None:
        workspace = self.make_multitier_git_workspace(initial_value=1, proxy_value=2, final_value=6)
        create_experiment(
            self.paths,
            self.make_multitier_code_spec(
                "multitier-auto",
                workspace_root=workspace,
                promotion_rule="after-success-streak",
            ),
        )

        first_messages = run_once(self.paths, max_runs=1)
        self.assertEqual(len(first_messages), 1)
        self.assertEqual((workspace / "state.txt").read_text().strip(), "1")

        status = get_status(self.paths, "multitier-auto")
        self.assertIsNotNone(status)
        self.assertEqual(status["current_fidelity_tier"], "final")
        self.assertEqual(status["next_fidelity_tier"], None)
        self.assertEqual(status["success_streak_by_tier"]["proxy"], 1)

        second_messages = run_once(self.paths, max_runs=1)
        self.assertEqual(len(second_messages), 1)
        self.assertEqual((workspace / "state.txt").read_text().strip(), "6")

        status = get_status(self.paths, "multitier-auto")
        self.assertIsNotNone(status)
        self.assertEqual(status["run_count_by_tier"]["proxy"], 1)
        self.assertEqual(status["run_count_by_tier"]["final"], 1)

    def test_run_once_filters_by_executor_class(self) -> None:
        create_experiment(self.paths, self.make_executor_class_spec("cpu-exp", "cpu"))
        create_experiment(self.paths, self.make_executor_class_spec("gpu-exp", "gpu"))

        messages = run_once(self.paths, max_runs=5, allowed_executor_classes=["gpu"])
        self.assertEqual(messages, ["ran gpu-exp [default] as researcher (success)"])

        cpu_status = get_status(self.paths, "cpu-exp")
        gpu_status = get_status(self.paths, "gpu-exp")
        self.assertIsNotNone(cpu_status)
        self.assertIsNotNone(gpu_status)
        self.assertEqual(cpu_status["run_count"], 0)
        self.assertEqual(gpu_status["run_count"], 1)

    def test_local_agent_router_stub_provider_runs_without_credentials(self) -> None:
        workspace = self.make_git_workspace(initial_value=1, mutated_value=1)
        (workspace / ".mutation.md").write_text("Dry run the local agent contract.\n")
        subprocess.run(["git", "add", ".mutation.md"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add mutation brief"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

        create_experiment(
            self.paths,
            self.make_local_agent_code_spec(
                "local-stub",
                workspace_root=workspace,
                provider="stub",
                model="stub",
                promotion_strategy="patch-only",
            ),
        )

        messages = run_once(self.paths, max_runs=1)
        self.assertEqual(len(messages), 1)
        self.assertEqual((workspace / "state.txt").read_text().strip(), "1")

        run_dir = next((self.paths.experiments / "local-stub" / "runs").iterdir())
        self.assertTrue((run_dir / "artifacts" / "local-agent" / "selection.json").exists())
        self.assertTrue((run_dir / "artifacts" / "local-agent" / "plan.json").exists())

    def test_dispatch_queue_work_and_ingest_stub_flow(self) -> None:
        create_experiment(self.paths, self.make_spec("dispatch-exp"))

        queued = queue_dispatch(self.paths, max_runs=1)
        self.assertEqual(len(queued), 1)
        ready_packages = list(self.paths.dispatch_ready.iterdir())
        self.assertEqual(len(ready_packages), 1)

        status = get_status(self.paths, "dispatch-exp")
        self.assertIsNotNone(status)
        self.assertIsNotNone(status["current_dispatch"])
        self.assertEqual(status["current_dispatch"]["stage"], "ready")

        completed = dispatch_work(self.paths, max_runs=1, worker="jetson-worker")
        self.assertEqual(len(completed), 1)
        self.assertFalse(list(self.paths.dispatch_ready.iterdir()))
        complete_packages = list(self.paths.dispatch_complete.iterdir())
        self.assertEqual(len(complete_packages), 1)
        self.assertFalse(list((self.paths.experiments / "dispatch-exp" / "runs").iterdir()))

        ingested = ingest_dispatch(self.paths, max_runs=1)
        self.assertEqual(len(ingested), 1)

        status = get_status(self.paths, "dispatch-exp")
        self.assertIsNotNone(status)
        self.assertEqual(status["run_count"], 1)
        self.assertIsNone(status["current_dispatch"])

        run_dirs = list((self.paths.experiments / "dispatch-exp" / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        self.assertTrue((run_dirs[0] / "RESULT.md").exists())

        write_lab_status(self.paths)
        index = json.loads(self.paths.lab_index_json.read_text())
        self.assertIn("dispatch", index)
        self.assertEqual(index["dispatch"]["counts_by_stage"]["ingested"], 1)

    def test_dispatch_claim_manual_complete_and_ingest(self) -> None:
        create_experiment(self.paths, self.make_spec("dispatch-manual"))
        queue_dispatch(self.paths, max_runs=1)

        claimed = claim_dispatch(self.paths, max_claims=1, worker="external-agent")
        self.assertEqual(len(claimed), 1)
        dispatch_dir = claimed[0]["dir"]
        dispatch_id = claimed[0]["record"]["dispatch_id"]
        run_dir = dispatch_dir / "run"

        (run_dir / "RESULT.md").write_text(
            textwrap.dedent(
                """
                # Manual dispatch result

                ## Result
                External worker completed the package.
                """
            ).strip()
            + "\n"
        )
        save_json(
            run_dir / "metrics.json",
            {
                "iteration": 1,
                "metric": "placeholder_metric",
                "value": 7,
                "source": "external-worker",
            },
        )

        record = mark_dispatch_complete(
            self.paths,
            dispatch_id,
            outcome="success",
            worker="external-agent",
        )
        self.assertEqual(record["stage"], "complete")

        ingested = ingest_dispatch(self.paths, dispatch_ids=[dispatch_id], max_runs=1)
        self.assertEqual(len(ingested), 1)

        status = get_status(self.paths, "dispatch-manual")
        self.assertIsNotNone(status)
        self.assertEqual(status["best_metric_value"], 7)
        self.assertIsNone(status["current_dispatch"])

    def test_dispatch_queue_respects_executor_class_filter(self) -> None:
        create_experiment(self.paths, self.make_executor_class_spec("dispatch-cpu", "cpu"))
        create_experiment(self.paths, self.make_executor_class_spec("dispatch-gpu", "gpu"))

        messages = queue_dispatch(self.paths, max_runs=5, allowed_executor_classes=["gpu"])
        self.assertEqual(len(messages), 1)
        self.assertIn("dispatch-gpu", messages[0])

        ready_packages = list(self.paths.dispatch_ready.iterdir())
        self.assertEqual(len(ready_packages), 1)
        dispatch_record = json.loads((ready_packages[0] / "dispatch.json").read_text())
        self.assertEqual(dispatch_record["experiment"], "dispatch-gpu")

    def test_resolved_mutation_command_dispatch_provider_returns_empty(self) -> None:
        for provider in ["dispatch", "external", "agent"]:
            result = resolved_mutation_command({"agent_provider": provider})
            self.assertEqual(result, "", f"agent_provider={provider} should return empty")

    def test_resolved_mutation_command_other_provider_returns_script(self) -> None:
        result = resolved_mutation_command({"agent_provider": "openai"})
        self.assertEqual(result, "python3 scripts/local_agent_mutation.py")

    def test_dispatch_agent_next_returns_context(self) -> None:
        create_experiment(self.paths, self.make_spec("agent-next-exp"))
        result = dispatch_agent_next(self.paths, worker="test-agent")
        self.assertIsNotNone(result)
        self.assertEqual(result["experiment"], "agent-next-exp")
        self.assertIn("dispatch_id", result)
        self.assertIn("package_dir", result)
        self.assertIn("input_files", result)
        self.assertIn("current_files", result)
        self.assertIn("iteration", result)
        self.assertEqual(result["iteration"], 1)
        # Verify dispatch is now in running state
        status = get_status(self.paths, "agent-next-exp")
        self.assertIsNotNone(status)
        self.assertIsNotNone(status["current_dispatch"])
        self.assertEqual(status["current_dispatch"]["stage"], "running")
        self.assertEqual(status["current_dispatch"]["worker"], "test-agent")

    def test_dispatch_agent_next_returns_none_when_no_experiments(self) -> None:
        result = dispatch_agent_next(self.paths)
        self.assertIsNone(result)

    def test_dispatch_agent_submit_full_cycle(self) -> None:
        # Create a workspace with a mutable file and a validation script
        workspace = Path(self._tmpdir.name) / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", str(workspace)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.email", "test@test.com"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (workspace / "params.json").write_text('{"lr": 0.01}\n')
        # Validation script that outputs a metric
        (workspace / "validate.sh").write_text('#!/bin/bash\necho \'{"metric": "score", "value": 42}\'\n')
        (workspace / "validate.sh").chmod(0o755)
        subprocess.run(
            ["git", "-C", str(workspace), "add", "."],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(workspace), "commit", "-m", "init"],
            capture_output=True, check=True,
        )

        # Create experiment with workspace and validation
        spec_text = textwrap.dedent(
            f"""
            id: agent-submit-exp
            mode: autoresearch-loop
            goal: Test agent submit
            metric: score
            metric_direction: maximize
            priority: normal
            autonomous: true
            cadence: every-30-minutes
            time_budget_minutes: 5
            max_iterations_total: 10
            max_iterations_per_run: 1
            workspace_root: {workspace}
            mutable_paths:
              - params.json
            validation_command: bash validate.sh
            worker_roles:
              - researcher
            """
        ).strip() + "\n"
        spec_path = Path(self._tmpdir.name) / "agent-submit-exp.yaml"
        spec_path.write_text(spec_text)
        create_experiment(self.paths, spec_path)

        # Get next dispatch
        context = dispatch_agent_next(self.paths, worker="test-agent")
        self.assertIsNotNone(context)
        dispatch_id = context["dispatch_id"]

        # Submit changes
        result = dispatch_agent_submit(
            self.paths,
            dispatch_id,
            changes={"params.json": '{"lr": 0.001}\n'},
            reasoning="Lowered learning rate for stability",
            worker="test-agent",
        )

        self.assertEqual(result["dispatch_id"], dispatch_id)
        self.assertEqual(result["experiment"], "agent-submit-exp")
        self.assertEqual(result["outcome"], "success")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["candidate_value"], 42.0)
        self.assertTrue(result["ingested"])

        # Verify experiment state was updated
        status = get_status(self.paths, "agent-submit-exp")
        self.assertIsNotNone(status)
        self.assertEqual(status["run_count"], 1)
        self.assertEqual(status["best_metric_value"], 42.0)
        self.assertIsNone(status["current_dispatch"])

        # Verify run artifacts
        run_dirs = list((self.paths.experiments / "agent-submit-exp" / "runs").iterdir())
        self.assertEqual(len(run_dirs), 1)
        self.assertTrue((run_dirs[0] / "RESULT.md").exists())
        self.assertTrue((run_dirs[0] / "metrics.json").exists())
        metrics = json.loads((run_dirs[0] / "metrics.json").read_text())
        self.assertEqual(metrics["source"], "dispatch-agent")
        self.assertEqual(metrics["value"], 42.0)


if __name__ == "__main__":
    unittest.main()
