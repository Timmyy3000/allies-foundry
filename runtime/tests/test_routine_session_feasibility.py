import asyncio
import importlib.util
import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess
from typing import ClassVar

import pytest

from allies_runtime.hermes import stable_session_identifiers

RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _load_harness_module(name: str, filename: str):
    path = RUNTIME_ROOT / "hermes-image" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LAUNCH = _load_harness_module("cld012_launch_routine_probe", "launch_routine_probe.py")
SMOKE = _load_harness_module("cld012_smoke_routine_sessions", "smoke_routine_sessions.py")


def test_stable_separate_session_ids_preserve_profile_and_conversation_identity():
    profile_id = "ally-v1-cld012-synthetic-profile"
    conversations = (
        "cld012-main-conversation",
        "cld012-routine-conversation-a",
        "cld012-routine-conversation-b",
    )
    identifiers = [
        stable_session_identifiers(profile_id, conversation)
        for conversation in conversations
    ]

    assert len({item.candidate_id for item in identifiers}) == 3
    assert len({item.session_key for item in identifiers}) == 3
    assert identifiers[0] == stable_session_identifiers(profile_id, conversations[0])
    assert identifiers[0] != stable_session_identifiers(
        "ally-v1-cld012-other-profile", conversations[0]
    )
    assert all(item.candidate_id.startswith("allies-s-") for item in identifiers)
    assert all(item.session_key.startswith("allies-k-") for item in identifiers)


@pytest.mark.parametrize(
    "failed_check",
    ["real_session_turns", "event_identity_attribution", "history_canary_isolation"],
)
def test_service_status_prefers_post_preflight_failure_over_blocked_barrier(failed_check):
    checks = [
        {"name": "authenticated_readiness", "status": "pass"},
        {"name": "model_preflight", "status": "pass"},
        {"name": failed_check, "status": "fail"},
        {"name": "server_observable_barrier", "status": "blocked"},
    ]

    assert SMOKE._service_status(checks) == "CAPABILITY_FAILED"


def test_service_status_keeps_setup_blocked_when_only_barrier_is_missing():
    checks = [
        {"name": "authenticated_readiness", "status": "pass"},
        {"name": "model_preflight", "status": "pass"},
        {"name": "server_observable_barrier", "status": "blocked"},
    ]

    assert SMOKE._service_status(checks) == "SETUP_BLOCKED"


def test_owned_network_command_is_unexposed_and_allows_egress():
    command = LAUNCH.build_network_command("cld012-network")

    assert command == [
        "docker",
        "network",
        "create",
        "--label",
        "cld012.owner=probe",
        "cld012-network",
    ]
    assert "--internal" not in command
    assert "--publish" not in command
    assert "-p" not in command


@pytest.mark.parametrize(
    ("inspect_output", "expected"),
    [
        (LAUNCH.SOURCE_COMMIT, True),
        (f"  {LAUNCH.SOURCE_COMMIT}\n", True),
        (f"prefix{LAUNCH.SOURCE_COMMIT}", False),
        (f"{LAUNCH.SOURCE_COMMIT}suffix", False),
    ],
)
def test_image_source_revision_requires_exact_normalized_output(
    inspect_output, expected
):
    result = CompletedProcess(
        ["docker", "image", "inspect"], 0, stdout=inspect_output, stderr=""
    )

    assert (
        LAUNCH._succeeded_with_exact_output(result, LAUNCH.SOURCE_COMMIT)
        is expected
    )


@pytest.mark.parametrize(
    ("readiness_output", "expected"),
    [("READY\n", True), ("NOT_READY\n", False)],
)
def test_readiness_requires_exact_ready_output(
    monkeypatch, readiness_output, expected
):
    clock = iter((0.0, 0.1, 1.0, 1.0))
    monkeypatch.setattr(LAUNCH.time, "monotonic", lambda: next(clock))

    def runner(command, **kwargs):
        return CompletedProcess(command, 0, stdout=readiness_output, stderr="")

    assert LAUNCH._wait_for_readiness(runner, "container", 1.0) is expected


def test_launcher_validates_bounded_references_and_redacts_commands(tmp_path):
    upstream = tmp_path / "upstream.sock"
    upstream.touch()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(LAUNCH.socket, "AF_UNIX", object(), raising=False)
    environment = {
        "CLD012_MODEL_PROVIDER": "openai",
        "CLD012_UPSTREAM_CREDENTIAL_SOCKET": str(upstream),
    }
    image = "allies/hermes@sha256:" + "a" * 64
    credential_ref = "vault://cld012/hermes"
    model_profile_ref = "vault://cld012/model"

    validated = LAUNCH.validate_inputs(
        image=image,
        credential_ref=credential_ref,
        model_profile_ref=model_profile_ref,
        setup_timeout_seconds=5,
        probe_timeout_seconds=5,
        environment=environment,
    )
    assert validated[:3] == (image, credential_ref, model_profile_ref)

    command = LAUNCH.build_run_command(
        image=image,
        container_name="container",
        network_name="network",
        data_root=tmp_path / "data",
        socket_root=tmp_path / "socket",
        runtime_root=RUNTIME_ROOT,
        probe_path=RUNTIME_ROOT / "hermes-image" / "smoke_routine_sessions.py",
        profile_id="profile",
        credential_ref=credential_ref,
    )
    redacted = LAUNCH.redact_command(
        command
        + [
            "--credential-ref",
            credential_ref,
            "--model-profile-ref",
            model_profile_ref,
            "MODEL_PROVIDER_API_KEY=fixture-value",
        ]
    )
    assert credential_ref not in redacted
    assert model_profile_ref not in redacted
    assert "fixture-value" not in redacted
    assert sum("<redacted-reference>" in item for item in redacted) == 4

    try:
        with pytest.raises(LAUNCH.LaunchBlocked, match="oversized"):
            LAUNCH.validate_inputs(
                image=image,
                credential_ref="x" * (LAUNCH.MAX_REFERENCE_BYTES + 1),
                model_profile_ref=model_profile_ref,
                setup_timeout_seconds=5,
                probe_timeout_seconds=5,
                environment=environment,
            )
    finally:
        monkeypatch.undo()


def _unix_query(path: Path, payload: bytes) -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(1)
        client.connect(str(path))
        client.sendall(payload)
        received = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                return bytes(received)
            received.extend(chunk)
    finally:
        client.close()


def test_credential_proxy_forwards_only_allowlisted_refs_and_bounds_responses(
    tmp_path, monkeypatch
):
    class FakeClient:
        def __init__(self, response):
            self.response = response
            self.sent: list[bytes] = []

        def settimeout(self, timeout):
            return None

        def connect(self, path):
            return None

        def sendall(self, value):
            self.sent.append(value)

        def recv(self, limit):
            value, self.response = self.response[:limit], self.response[limit:]
            return value

        def close(self):
            return None

    clients: list[FakeClient] = []

    def fake_socket(*args):
        response = (
            b"fixture-response\n"
            if len(clients) == 0
            else b"x" * (LAUNCH.MAX_CREDENTIAL_RESPONSE_BYTES + 1) + b"\n"
        )
        client = FakeClient(response)
        clients.append(client)
        return client

    monkeypatch.setattr(LAUNCH.socket, "AF_UNIX", object(), raising=False)
    monkeypatch.setattr(LAUNCH.socket, "socket", fake_socket)
    proxy = LAUNCH.CredentialSocketProxy(
        tmp_path / "proxy.sock",
        tmp_path / "upstream.sock",
        0.5,
        ("vault://bootstrap", "vault://profile", "vault://oversize"),
    )
    assert proxy._resolve(b"vault://bootstrap\n") == b"fixture-response\n"
    assert proxy._resolve(b"vault://unknown\n") is None
    assert proxy._resolve(b"vault://bootstrap-changed\n") is None
    assert (
        proxy._resolve(b"x" * (LAUNCH.MAX_CREDENTIAL_REQUEST_BYTES + 1) + b"\n")
        is None
    )
    assert proxy._resolve(b"vault://oversize\n") is None
    assert [client.sent for client in clients] == [
        [b"vault://bootstrap\n"],
        [b"vault://oversize\n"],
    ]


def _patch_launcher_setup(monkeypatch, tmp_path, calls):
    class DummyProxy:
        def __init__(self, *args, **kwargs):
            self.closed = False

        def start(self):
            return None

        def close(self):
            self.closed = True

    monkeypatch.setattr(LAUNCH, "CredentialSocketProxy", DummyProxy)
    monkeypatch.setattr(LAUNCH.shutil, "which", lambda name: "docker")
    monkeypatch.setattr(
        LAUNCH, "_materialize_profile", lambda *args, **kwargs: "profile"
    )
    monkeypatch.setattr(
        LAUNCH,
        "validate_inputs",
        lambda **kwargs: (
            kwargs["image"],
            kwargs["credential_ref"],
            kwargs["model_profile_ref"],
            tmp_path / "upstream.sock",
        ),
    )

    def result(command, returncode=0, stdout=""):
        return CompletedProcess(command, returncode, stdout=stdout, stderr="")

    def runner(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["network", "create"]:
            return result(command)
        if command[1:3] == ["run", "--detach"]:
            return result(command)
        if command[1:3] == ["rm", "--force"] or command[1:3] == ["network", "rm"]:
            return result(command)
        if command[1:2] == ["version"]:
            return result(command, stdout="27.0")
        if command[1:3] == ["image", "inspect"]:
            return result(command, stdout=LAUNCH.SOURCE_COMMIT)
        return result(command)

    return runner


def test_run_probe_uses_supplied_environment_for_profile_materialization(
    monkeypatch, tmp_path
):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(LAUNCH, "_wait_for_readiness", lambda *args: False)
    monkeypatch.delenv("CLD012_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("CLD012_MODEL_BASE_URL", raising=False)
    materialized = {}

    def materialize(data_root, socket_path, model_profile_ref, *, provider, base_url):
        materialized.update(provider=provider, base_url=base_url)
        return "profile"

    monkeypatch.setattr(LAUNCH, "_materialize_profile", materialize)
    environment = {
        "CLD012_MODEL_PROVIDER": "custom-provider",
        "CLD012_MODEL_BASE_URL": "https://provider.invalid/v1",
        "CLD012_UPSTREAM_CREDENTIAL_SOCKET": str(tmp_path / "upstream.sock"),
    }
    image = "allies/hermes@sha256:" + "0" * 64

    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=runner,
        environment=environment,
    )

    assert report["status"] == "SETUP_BLOCKED"
    assert materialized == {
        "provider": "custom-provider",
        "base_url": "https://provider.invalid/v1",
    }


def test_run_probe_gates_probe_on_readiness(monkeypatch, tmp_path):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(LAUNCH, "_wait_for_readiness", lambda *args: False)
    image = "allies/hermes@sha256:" + "b" * 64

    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=runner,
    )

    assert report["status"] == "SETUP_BLOCKED"
    assert report["readiness"] == "failed"
    assert not any(
        "--mode" in command and "service" in command for command in calls
    )


def test_run_probe_timeout_does_not_cleanup_uncreated_resources(monkeypatch, tmp_path):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)

    def timeout_runner(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["network", "create"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return runner(command, **kwargs)

    image = "allies/hermes@sha256:" + "c" * 64
    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=timeout_runner,
    )

    assert report["status"] == "SETUP_BLOCKED"
    assert report["cleanup"] == "not_needed"
    assert not any(command[1:3] == ["rm", "--force"] for command in calls)
    assert not any(command[1:3] == ["network", "rm"] for command in calls)


def test_run_probe_reports_cleanup_failure(monkeypatch, tmp_path):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(LAUNCH, "_wait_for_readiness", lambda *args: True)
    image = "allies/hermes@sha256:" + "d" * 64

    def cleanup_failure_runner(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["rm", "--force"] or command[1:3] == ["network", "rm"]:
            return CompletedProcess(command, 1, stdout="", stderr="cleanup failed")
        if command[1:2] == ["exec"]:
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "mode": "service",
                        "status": "CAPABILITY_PASSED",
                        "checks": [
                            {"name": name, "status": "pass"}
                            for name in LAUNCH.CLASS_B_REQUIRED_CHECKS
                        ],
                    }
                ),
                stderr="",
            )
        return runner(command, **kwargs)

    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=cleanup_failure_runner,
    )

    assert report["status"] == "CLEANUP_INCOMPLETE"
    assert report["cleanup"] == "failed"


def test_run_probe_requires_service_stage_evidence_before_capability(
    monkeypatch, tmp_path
):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(LAUNCH, "_wait_for_readiness", lambda *args: True)
    image = "allies/hermes@sha256:" + "e" * 64

    def incomplete_probe_runner(command, **kwargs):
        calls.append(command)
        if command[1:2] == ["exec"]:
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps({"mode": "service", "status": "CAPABILITY_PASSED"}),
                stderr="",
            )
        return runner(command, **kwargs)

    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=incomplete_probe_runner,
    )

    assert report["status"] == "SETUP_BLOCKED"
    assert report["model_preflight"] == "failed"


def test_run_probe_rejects_client_only_overlap_without_server_barrier(
    monkeypatch, tmp_path
):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(LAUNCH, "_wait_for_readiness", lambda *args: True)
    image = "allies/hermes@sha256:" + "f" * 64

    def client_only_probe_runner(command, **kwargs):
        calls.append(command)
        if command[1:2] == ["exec"]:
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "mode": "service",
                        "status": "CAPABILITY_PASSED",
                        "checks": [
                            {"name": "authenticated_readiness", "status": "pass"},
                            {"name": "model_preflight", "status": "pass"},
                            {"name": "real_session_turns", "status": "pass"},
                            {"name": "main_and_routine_overlap", "status": "pass"},
                            {
                                "name": "main_completion_while_routine_active",
                                "status": "pass",
                            },
                        ],
                    }
                ),
                stderr="",
            )
        return runner(command, **kwargs)

    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=client_only_probe_runner,
    )

    assert report["status"] == "SETUP_BLOCKED"
    assert report["capability"] == "pending"


def test_run_probe_rejects_duplicate_class_b_check_names(monkeypatch, tmp_path):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(LAUNCH, "_wait_for_readiness", lambda *args: True)
    image = "allies/hermes@sha256:" + "1" * 64

    def duplicate_check_runner(command, **kwargs):
        calls.append(command)
        if command[1:2] == ["exec"]:
            checks = [
                {"name": name, "status": "pass"}
                for name in LAUNCH.CLASS_B_REQUIRED_CHECKS
            ]
            checks.append({"name": "server_observable_barrier", "status": "pass"})
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "mode": "service",
                        "status": "CAPABILITY_PASSED",
                        "checks": checks,
                    }
                ),
                stderr="",
            )
        return runner(command, **kwargs)

    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=duplicate_check_runner,
    )

    assert report["status"] == "SETUP_BLOCKED"
    assert report["model_preflight"] == "failed"


def test_run_probe_accepts_complete_capability_failed_report_with_exit_one(
    monkeypatch, tmp_path
):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(LAUNCH, "_wait_for_readiness", lambda *args: True)
    image = "allies/hermes@sha256:" + "2" * 64

    def capability_failed_runner(command, **kwargs):
        calls.append(command)
        if command[1:2] == ["exec"]:
            return CompletedProcess(
                command,
                1,
                stdout=json.dumps(
                    {
                        "mode": "service",
                        "status": "CAPABILITY_FAILED",
                        "checks": [
                            {"name": "authenticated_readiness", "status": "pass"},
                            {"name": "model_preflight", "status": "pass"},
                            {"name": "real_session_turns", "status": "fail"},
                            {"name": "server_observable_barrier", "status": "pass"},
                            {"name": "main_and_routine_overlap", "status": "pass"},
                            {
                                "name": "main_completion_while_routine_active",
                                "status": "pass",
                            },
                        ],
                    }
                ),
                stderr="capability failed",
            )
        return runner(command, **kwargs)

    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=capability_failed_runner,
    )

    assert report["status"] == "CAPABILITY_FAILED"
    assert report["model_preflight"] == "passed"
    assert report["capability"] == "failed"


def test_run_probe_accepts_early_capability_failed_report_with_exit_one(
    monkeypatch, tmp_path
):
    calls = []
    runner = _patch_launcher_setup(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(LAUNCH, "_wait_for_readiness", lambda *args: True)
    image = "allies/hermes@sha256:" + "3" * 64

    def early_capability_failed_runner(command, **kwargs):
        calls.append(command)
        if command[1:2] == ["exec"]:
            return CompletedProcess(
                command,
                1,
                stdout=json.dumps(
                    {
                        "mode": "service",
                        "status": "CAPABILITY_FAILED",
                        "checks": [
                            {"name": "authenticated_readiness", "status": "pass"},
                            {"name": "model_preflight", "status": "pass"},
                            {"name": "session_creation", "status": "fail"},
                        ],
                    }
                ),
                stderr="capability failed",
            )
        return runner(command, **kwargs)

    report = LAUNCH.run_probe(
        image=image,
        credential_ref="vault://cld012/hermes",
        model_profile_ref="vault://cld012/model",
        setup_timeout_seconds=1,
        probe_timeout_seconds=1,
        runner=early_capability_failed_runner,
    )

    assert report["status"] == "CAPABILITY_FAILED"
    assert report["model_preflight"] == "passed"
    assert report["capability"] == "failed"


def test_validated_capability_failed_accepts_post_turn_assertions():
    payload = {
        "mode": "service",
        "status": "CAPABILITY_FAILED",
        "checks": [
            {"name": "authenticated_readiness", "status": "pass"},
            {"name": "model_preflight", "status": "pass"},
            {"name": "real_session_turns", "status": "pass"},
            {"name": "server_observable_barrier", "status": "pass"},
            {"name": "main_and_routine_overlap", "status": "pass"},
            {"name": "main_completion_while_routine_active", "status": "pass"},
            {"name": "event_identity_attribution", "status": "fail"},
            {"name": "history_canary_isolation", "status": "pass"},
            {"name": "memory_file_observations", "status": "pass"},
        ],
    }

    assert LAUNCH._validated_probe_report(payload, 1) is not None


def test_markers_recalled_matches_each_requested_marker():
    results = [
        {"status": "ok", "count": 2, "results": [{"content": "A B"}]},
        {"status": "ok", "count": 0, "results": []},
    ]

    assert not SMOKE._markers_recalled(results, ("A", "B"))
    assert SMOKE._markers_recalled(
        [
            {"status": "ok", "count": 1, "results": [{"content": "A"}]},
            {"status": "ok", "count": 1, "results": [{"content": "B"}]},
        ],
        ("A", "B"),
    )


@pytest.mark.parametrize("status", ["tool_error", "memory_unavailable", "error"])
def test_recall_requires_success_status(status):
    assert not SMOKE._recall_succeeded(
        {"status": status, "count": 1, "results": [{"content": "A"}]}
    )


def test_concurrent_writes_use_bounded_futures_and_fail_closed_on_timeout():
    calls: list[str] = []
    release = threading.Event()

    class Provider:
        def __init__(self, blocked=False):
            self.blocked = blocked

        def handle_tool_call(self, name, arguments):
            calls.append(arguments["content"])
            if self.blocked:
                release.wait()
            return json.dumps({"status": "stored"})

    results, completed = SMOKE._run_concurrent_writes(
        Provider(), Provider(blocked=True), 0.02
    )
    assert not completed
    assert len(results) <= 2
    assert set(calls) == set(SMOKE.OFFLINE_CONTENTION_MARKERS)
    release.set()
    time.sleep(0.05)
    assert calls


def test_offline_checks_bound_blocking_initial_remember(tmp_path):
    class Provider:
        instances: ClassVar[list[object]] = []

        def __init__(self):
            self.release = threading.Event()
            self.session_id = None
            self.__class__.instances.append(self)

        def initialize(self, session_id, **kwargs):
            self.session_id = session_id

        def status(self):
            return {
                "available": True,
                "mode": "narrow_tools",
                "profile_keyed": True,
                "shared_surface": False,
                "profile_db_root": self.session_id,
            }

        def get_tool_names(self):
            return ["mnemosyne_recall", "mnemosyne_remember"]

        def handle_tool_call(self, name, arguments):
            if (
                name == "mnemosyne_remember"
                and arguments.get("content") == "CLD012_OFFLINE_FACT_A"
            ):
                self.release.wait()
            return json.dumps({"status": "stored"})

        def get_tool_schemas(self):
            return []

        def shutdown(self):
            return None

    checks = []
    started = time.monotonic()
    try:
        SMOKE._offline_memory_checks(
            Provider,
            tmp_path,
            checks,
            deadline=started + 0.03,
        )
        assert time.monotonic() - started < 0.5
        by_name = {check["name"]: check for check in checks}
        assert by_name["shared_profile_distinct_writes"]["status"] == "fail"
        assert by_name["shared_profile_concurrent_writes"]["status"] == "blocked"
        assert by_name["fresh_shared_session_recall"]["status"] == "blocked"
    finally:
        for provider in Provider.instances:
            provider.release.set()


def test_offline_checks_bound_blocking_fresh_recall(tmp_path):
    class Provider:
        instances: ClassVar[list[object]] = []

        def __init__(self):
            self.release = threading.Event()
            self.session_id = None
            self.__class__.instances.append(self)

        def initialize(self, session_id, **kwargs):
            self.session_id = session_id

        def status(self):
            return {
                "available": True,
                "mode": "narrow_tools",
                "profile_keyed": True,
                "shared_surface": False,
                "profile_db_root": self.session_id,
            }

        def get_tool_names(self):
            return ["mnemosyne_recall", "mnemosyne_remember"]

        def handle_tool_call(self, name, arguments):
            if name == "mnemosyne_recall":
                if self.session_id == "cld012-shared-session-fresh":
                    self.release.wait()
                marker = arguments["query"]
                return json.dumps(
                    {"status": "ok", "count": 1, "results": [{"content": marker}]}
                )
            return json.dumps({"status": "stored"})

        def get_tool_schemas(self):
            return []

        def shutdown(self):
            return None

    checks = []
    started = time.monotonic()
    try:
        SMOKE._offline_memory_checks(
            Provider,
            tmp_path,
            checks,
            deadline=started + 0.03,
        )
        assert time.monotonic() - started < 0.5
        by_name = {check["name"]: check for check in checks}
        assert by_name["shared_profile_concurrent_writes"]["status"] == "pass"
        assert by_name["fresh_shared_session_recall"]["status"] == "fail"
        assert by_name["fresh_shared_session_contention_recall"]["status"] == "blocked"
        assert by_name["second_profile_cannot_read_shared_fact"]["status"] == "blocked"
    finally:
        for provider in Provider.instances:
            provider.release.set()


def test_server_concurrency_oracle_rejects_serialized_client_overlap():
    class SerializedClient:
        def __init__(self):
            self.server_lock = asyncio.Lock()

        async def stream_profile(self, name):
            async with self.server_lock:
                await asyncio.sleep(0.01)
                return object()

    async def collect():
        client = SerializedClient()
        intervals = {}
        results = {}

        async def one(name):
            started = time.monotonic()
            results[name] = await client.stream_profile(name)
            intervals[name] = (started, time.monotonic())

        await asyncio.gather(one("main"), one("routine-a"), one("routine-b"))
        return results, intervals

    results, intervals = asyncio.run(collect())
    assert intervals["main"][0] <= intervals["routine-a"][1]
    assert intervals["routine-a"][0] <= intervals["main"][1]

    checks = SMOKE._server_observable_concurrency_checks(results, intervals)

    assert all(check["status"] == "blocked" for check in checks)
    assert all(
        check["detail"] == SMOKE.SERVER_OBSERVABLE_BARRIER_PREREQUISITE
        for check in checks
    )
