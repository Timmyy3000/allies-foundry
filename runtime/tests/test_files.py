from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

import pytest

from allies_runtime.errors import IncomingFileError
from allies_runtime.files import (
    cleanup_profile_publication_spools,
    freeze_publication,
    parse_incoming_files,
    publication_spool_path,
    release_publication_spool,
    stage_incoming_files,
    validate_hermes_file_context,
)
from allies_runtime.foundry import (
    FoundryClaim,
    FoundryWorker,
    InvalidRequestError,
    LeaseConflictError,
    TerminalReceipt,
)
from allies_runtime.hermes import HermesEvent, _stream_request_body
from allies_runtime.publication_bridge import PublicationBridge


def _descriptor(content: bytes) -> dict[str, object]:
    return {
        "file_id": str(uuid4()),
        "name": "notes.txt",
        "media_type": "text/plain",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


async def _chunks(content: bytes):
    yield content


@pytest.mark.asyncio
async def test_stage_replays_receipt_and_preserves_an_edited_working_copy(tmp_path):
    workspace = tmp_path / "profile" / "workspace"
    workspace.mkdir(parents=True)
    content = b"verified attachment"
    descriptor = _descriptor(content)
    downloads = 0

    def fetch(_descriptor):
        nonlocal downloads
        downloads += 1
        return _chunks(content)

    command_id = str(uuid4())
    first = await stage_incoming_files(workspace, command_id, [descriptor], fetch)
    first_path = workspace / first.files[0].path
    assert first_path.read_bytes() == content
    repeated = await stage_incoming_files(workspace, command_id, [descriptor], fetch)
    assert repeated == first
    assert downloads == 1

    first_path.write_bytes(b"edited working copy")
    recovered = await stage_incoming_files(workspace, command_id, [descriptor], fetch)
    assert downloads == 2
    assert first_path.read_bytes() == b"edited working copy"
    assert recovered.files[0].path != first.files[0].path
    assert (workspace / recovered.files[0].path).read_bytes() == content


@pytest.mark.asyncio
async def test_stage_rejects_a_bad_digest_before_the_manifest_becomes_visible(tmp_path):
    workspace = tmp_path / "profile" / "workspace"
    workspace.mkdir(parents=True)
    descriptor = _descriptor(b"expected")

    with pytest.raises(IncomingFileError, match="digest"):
        await stage_incoming_files(
            workspace,
            str(uuid4()),
            [descriptor],
            lambda _descriptor: _chunks(b"wrong"),
        )

    attachments = workspace / "attachments"
    assert not attachments.exists() or not list(attachments.iterdir())


@pytest.mark.asyncio
async def test_stage_cancellation_never_commits_a_replacement_or_receipt(tmp_path):
    workspace = tmp_path / "profile" / "workspace"
    workspace.mkdir(parents=True)
    descriptor = _descriptor(b"one")
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 8

    with pytest.raises(IncomingFileError, match="lease was lost"):
        await stage_incoming_files(
            workspace,
            str(uuid4()),
            [descriptor],
            lambda _descriptor: _chunks(b"one"),
            cancelled=cancelled,
        )

    attachments = workspace / "attachments"
    assert not attachments.exists() or not list(attachments.iterdir())
    assert not (workspace.parent / ".allies-incoming-receipts").exists()


def test_hermes_context_is_bounded_and_content_free():
    descriptor = _descriptor(b"one")
    context = {
        "schema_version": "v1",
        "kind": "allies_incoming_files",
        "files": [{**descriptor, "path": "attachments/turn/file-notes.txt"}],
    }

    assert validate_hermes_file_context(context) == context
    context["files"][0]["content"] = "must not pass"
    with pytest.raises(IncomingFileError):
        validate_hermes_file_context(context)


def test_manifest_rejects_a_windows_reserved_file_name():
    descriptor = _descriptor(b"one")
    descriptor["name"] = "CON.txt"

    with pytest.raises(IncomingFileError):
        parse_incoming_files([descriptor])


@pytest.mark.asyncio
async def test_stage_accepts_character_bounded_names_without_using_them_as_paths(tmp_path):
    workspace = tmp_path / "profile" / "workspace"
    workspace.mkdir(parents=True)
    descriptor = _descriptor(b"one")
    descriptor["name"] = "a" * 255

    receipt = await stage_incoming_files(
        workspace,
        str(uuid4()),
        [descriptor],
        lambda _descriptor: _chunks(b"one"),
    )

    path = workspace / receipt.files[0].path
    assert path.name == descriptor["file_id"]
    assert path.read_bytes() == b"one"


def test_incoming_name_is_bounded_by_characters_not_utf8_bytes():
    descriptor = _descriptor(b"one")
    descriptor["name"] = "😀" * 255

    assert parse_incoming_files([descriptor])[0].name == descriptor["name"]

    descriptor["name"] = "a" * 256
    with pytest.raises(IncomingFileError):
        parse_incoming_files([descriptor])


def test_hermes_request_uses_a_structured_file_context():
    descriptor = _descriptor(b"one")
    context = {
        "schema_version": "v1",
        "kind": "allies_incoming_files",
        "files": [{**descriptor, "path": "attachments/turn/file-notes.txt"}],
    }

    body = json.loads(_stream_request_body("", None, context))
    assert body == {"message": "", "allies_file_context": context}


def test_hermes_context_uses_utf8_without_ascii_escape_expansion():
    files = []
    for _ in range(10):
        descriptor = _descriptor(b"one")
        descriptor["name"] = "😀" * 255
        files.append(
            {
                **descriptor,
                "path": f"attachments/turn/{descriptor['file_id']}",
            }
        )

    encoded = _stream_request_body(
        "",
        None,
        {"schema_version": "v1", "kind": "allies_incoming_files", "files": files},
    )

    assert b"\\ud83d" not in encoded
    assert len(encoded) < 16 * 1024


def test_publication_freezes_once_outside_the_model_workspace(tmp_path):
    workspace = tmp_path / "profiles" / "ally" / "workspace"
    workspace.mkdir(parents=True)
    result = workspace / "result.csv"
    result.write_bytes(b"name,value\nanswer,42\n")
    publication_id = str(uuid4())

    first = freeze_publication(workspace, publication_id, ["result.csv"])
    result.write_bytes(b"edited working copy")
    replay = freeze_publication(workspace, publication_id, ["result.csv"])
    spool = publication_spool_path(
        workspace, publication_id, first.files[0].source_version_id
    )

    assert replay == first
    assert spool.read_bytes() == b"name,value\nanswer,42\n"
    assert spool.is_relative_to(tmp_path / ".allies-publications" / "ally")
    assert not spool.is_relative_to(workspace)

    release_publication_spool(workspace, publication_id)
    assert not spool.exists()


def test_publication_rejects_links_and_profile_cleanup_is_scoped(tmp_path):
    workspace = tmp_path / "profiles" / "ally" / "workspace"
    workspace.mkdir(parents=True)
    source = workspace / "result.csv"
    source.write_bytes(b"ok")
    sibling = tmp_path / "profiles" / "other" / "workspace"
    sibling.mkdir(parents=True)
    (sibling / "keep.txt").write_bytes(b"keep")
    try:
        (workspace / "linked.csv").symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(IncomingFileError, match="unsafe"):
        freeze_publication(workspace, str(uuid4()), ["linked.csv"])

    publication_id = str(uuid4())
    freeze_publication(workspace, publication_id, ["result.csv"])
    cleanup_profile_publication_spools(tmp_path, "ally")

    assert not (tmp_path / ".allies-publications" / "ally").exists()
    assert (sibling / "keep.txt").read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_publication_bridge_freezes_then_uploads_without_a_model_call(tmp_path):
    workspace = tmp_path / "profiles" / "ally" / "workspace"
    workspace.mkdir(parents=True)
    source = workspace / "result.csv"
    source.write_bytes(b"stable result")
    publication_id = str(uuid4())
    cloud_file_id = str(uuid4())
    calls = []

    class ProfileStore:
        def workspace_path(self, profile_key):
            assert profile_key == "ally"
            return workspace

    class Foundry:
        async def create_publication_intent(self, *args):
            calls.append(("intent", args))
            return {"publication_id": publication_id, "state": "preparing"}

        async def freeze_publication_intent(self, *args):
            calls.append(("frozen", args))
            return {"publication_id": publication_id, "state": "frozen"}

        async def register_publication(self, *args):
            calls.append(("register", args))
            return {
                "publication_id": publication_id,
                "state": "uploading",
                "revision": 1,
                "files": [{"id": cloud_file_id, "generation": 1, "state": "pending"}],
            }

        async def upload_publication_file(self, *args):
            calls.append(("upload", args))
            return {"id": cloud_file_id, "generation": 1, "state": "validating"}

        async def get_publication(self, *args):
            calls.append(("get", args))
            return {
                "publication_id": publication_id,
                "state": "ready",
                "files": [{"name": "result.csv", "open_path": "/files/ready"}],
            }

    claim = FoundryClaim(
        attempt_id=str(uuid4()),
        execution_id=str(uuid4()),
        profile_id=str(uuid4()),
        hermes_profile_key="ally",
        model="gpt-test",
        conversation_id="conversation",
        session_id="session",
        stream_id="stream",
        lease_id=str(uuid4()),
        lease_token="lease",
        expires_at=None,
        payload={"message": ""},
        claim_id=str(uuid4()),
    )
    bridge = PublicationBridge(Foundry(), ProfileStore(), tmp_path)
    context = bridge.activate(claim)

    result = await bridge.publish(context, "publish-1", ["result.csv"])
    source.write_bytes(b"edited after freeze")

    assert result == {
        "publication_id": publication_id,
        "state": "ready",
        "files": [{"name": "result.csv", "open_path": "/files/ready"}],
    }
    assert [name for name, _args in calls] == [
        "intent",
        "frozen",
        "register",
        "upload",
        "get",
    ]
    upload = calls[3][1]
    assert upload[4] == b"stable result"

    bridge.deactivate(context)
    assert await bridge.publish(context, "publish-1", ["result.csv"]) == {
        "state": "failed",
        "retryable": True,
        "error_code": "publication_context_invalid",
    }


@pytest.mark.asyncio
async def test_worker_stages_files_before_it_invokes_hermes(tmp_path):
    workspace = tmp_path / "profile" / "workspace"
    workspace.mkdir(parents=True)
    content = b"verified attachment"
    descriptor = _descriptor(content)
    calls: list[str] = []

    class ProfileStore:
        def workspace_path(self, _profile_key):
            return workspace

    class Foundry:
        async def incoming_file_chunks(self, _attempt, _file, _lease):
            calls.append("fetch")
            yield content

        async def renew(self, _attempt, _lease):
            return None

        async def event(self, *_args, **_kwargs):
            calls.append("event")

        async def bind(self, *_args, **_kwargs):
            return "session"

        async def complete(self, *_args, **_kwargs):
            return TerminalReceipt("attempt", "succeeded", "receipt")

        async def fail(self, *_args, **_kwargs):
            pytest.fail("worker must not invoke the model after staging failed")

    class Hermes:
        async def stream_profile_incremental(self, *_args, **kwargs):
            calls.append("hermes")
            assert (
                kwargs["file_context"]["files"][0]["file_id"] == descriptor["file_id"]
            )

            async def events():
                yield HermesEvent(
                    "execution.completed",
                    "ally",
                    "session",
                    "run",
                    1,
                    {"run_id": "run", "status": "completed"},
                )

            return events()

    claim = FoundryClaim(
        attempt_id=str(uuid4()),
        execution_id=str(uuid4()),
        profile_id=str(uuid4()),
        hermes_profile_key="ally",
        model="gpt-test",
        conversation_id="conversation",
        session_id="session",
        stream_id="stream",
        lease_id=str(uuid4()),
        lease_token="lease",
        expires_at=None,
        payload={"message": "", "files": [descriptor]},
        claim_id=str(uuid4()),
    )
    worker = FoundryWorker(
        Foundry(),
        Hermes(),
        profile_store=ProfileStore(),
        file_input_enabled=True,
    )

    receipt = await worker.run_claim(claim)
    assert receipt.status == "succeeded"
    assert calls == ["fetch", "event", "hermes"]


@pytest.mark.asyncio
async def test_worker_lease_loss_during_download_cannot_commit_or_call_hermes(tmp_path):
    workspace = tmp_path / "profile" / "workspace"
    workspace.mkdir(parents=True)
    content = b"two chunks"
    descriptor = _descriptor(content)
    calls: list[str] = []

    class ProfileStore:
        def workspace_path(self, _profile_key):
            return workspace

    class Foundry:
        async def incoming_file_chunks(self, _attempt, _file, _lease):
            calls.append("fetch")
            yield content[:3]
            await asyncio.sleep(0.03)
            yield content[3:]

        async def renew(self, _attempt, _lease):
            raise LeaseConflictError("lease expired")

        async def stopped(self, *_args, **_kwargs):
            calls.append("stopped")
            return TerminalReceipt("attempt", "stopped", "receipt")

        async def fail(self, *_args, **_kwargs):
            pytest.fail("worker must not fail or requeue a stale staged file")

    class Hermes:
        async def stream_profile_incremental(self, *_args, **_kwargs):
            pytest.fail("worker must not invoke Hermes after lease loss")

    claim = FoundryClaim(
        attempt_id=str(uuid4()),
        execution_id=str(uuid4()),
        profile_id=str(uuid4()),
        hermes_profile_key="ally",
        model="gpt-test",
        conversation_id="conversation",
        session_id="session",
        stream_id="stream",
        lease_id=str(uuid4()),
        lease_token="lease",
        expires_at=None,
        payload={"message": "", "files": [descriptor]},
        claim_id=str(uuid4()),
    )
    worker = FoundryWorker(
        Foundry(),
        Hermes(),
        profile_store=ProfileStore(),
        file_input_enabled=True,
        renew_interval=0.01,
        lease_seconds=1,
        stop_safety_margin=0.1,
    )

    receipt = await worker.run_claim(claim)

    assert receipt.status == "stopped"
    assert calls == ["fetch", "stopped"]
    attachments = workspace / "attachments"
    assert not attachments.exists() or not list(attachments.iterdir())
    assert not (workspace.parent / ".allies-incoming-receipts").exists()


@pytest.mark.asyncio
async def test_worker_terminalizes_a_cloud_file_error_before_model_dispatch(tmp_path):
    workspace = tmp_path / "profile" / "workspace"
    workspace.mkdir(parents=True)
    descriptor = _descriptor(b"unavailable")
    calls: list[str] = []

    class ProfileStore:
        def workspace_path(self, _profile_key):
            return workspace

    class Foundry:
        async def incoming_file_chunks(self, *_args):
            raise InvalidRequestError("accepted file is unavailable")
            yield b""  # pragma: no cover - keeps this an async generator

        async def fail(self, *_args, **kwargs):
            calls.append(kwargs["code"])
            return TerminalReceipt("attempt", "failed", "receipt")

    class Hermes:
        async def stream_profile_incremental(self, *_args, **_kwargs):
            pytest.fail("worker must not invoke Hermes after a Cloud file error")

    claim = FoundryClaim(
        attempt_id=str(uuid4()),
        execution_id=str(uuid4()),
        profile_id=str(uuid4()),
        hermes_profile_key="ally",
        model="gpt-test",
        conversation_id="conversation",
        session_id="session",
        stream_id="stream",
        lease_id=str(uuid4()),
        lease_token="lease",
        expires_at=None,
        payload={"message": "", "files": [descriptor]},
        claim_id=str(uuid4()),
    )

    receipt = await FoundryWorker(
        Foundry(), Hermes(), profile_store=ProfileStore(), file_input_enabled=True
    ).run_claim(claim)

    assert receipt.status == "failed"
    assert calls == ["INVALID_REQUEST"]


@pytest.mark.asyncio
async def test_worker_rejects_files_for_a_routine_before_staging():
    descriptor = _descriptor(b"routine attachment")
    failures = []

    class Foundry:
        async def routine_result(self, *_args, **kwargs):
            failures.append(kwargs["outcome"])
            return TerminalReceipt("attempt", "failed", "receipt")

    claim = FoundryClaim(
        attempt_id=str(uuid4()),
        execution_id=str(uuid4()),
        profile_id=str(uuid4()),
        hermes_profile_key="ally",
        model="gpt-test",
        conversation_id="routine-conversation",
        session_id="session",
        stream_id="stream",
        lease_id=str(uuid4()),
        lease_token="lease",
        expires_at=None,
        payload={"execution_prompt": "run", "files": [descriptor]},
        claim_id=str(uuid4()),
        routine_id=str(uuid4()),
    )
    worker = FoundryWorker(
        Foundry(),
        object(),
        profile_store=object(),
        file_input_enabled=True,
    )

    receipt = await worker.run_claim(claim)
    assert receipt.status == "failed"
    assert failures == ["failed"]
