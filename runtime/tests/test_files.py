from __future__ import annotations

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
from allies_runtime.foundry import FoundryClaim, FoundryWorker, TerminalReceipt
from allies_runtime.hermes import HermesEvent, _stream_request_body


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


def test_hermes_request_uses_a_structured_file_context():
    descriptor = _descriptor(b"one")
    context = {
        "schema_version": "v1",
        "kind": "allies_incoming_files",
        "files": [{**descriptor, "path": "attachments/turn/file-notes.txt"}],
    }

    body = json.loads(_stream_request_body("", None, context))
    assert body == {"message": "", "allies_file_context": context}


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
