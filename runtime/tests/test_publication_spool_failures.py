from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from allies_runtime import files
from allies_runtime.errors import IncomingFileError
from allies_runtime.files import (
    cleanup_profile_publication_spools,
    cleanup_stale_publication_copies,
    freeze_publication,
    parse_incoming_files,
    prepare_publication_files,
    publication_spool_path,
    reconcile_publication_spools,
    recover_publication_manifests,
    release_publication_spool,
    stage_incoming_files,
    validate_hermes_file_context,
)


def _workspace(root, profile: str = "ally"):
    workspace = root / "profiles" / profile / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def _descriptor(content: bytes = b"content"):
    return {
        "file_id": str(uuid4()),
        "name": "result.csv",
        "media_type": "text/csv",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


@pytest.mark.skipif(files.os.name == "nt", reason="POSIX-only permission boundary")
def test_publication_spool_requires_root_owned_permissions(tmp_path, monkeypatch):
    actions = []
    root = tmp_path / ".allies-publications"
    spool = root / "ally"

    monkeypatch.setattr(files.os, "chown", lambda *args: actions.append(args))
    monkeypatch.setattr(files.os, "chmod", lambda *args: actions.append(args))

    assert files._publication_spool_root(tmp_path, "ally") == spool
    assert actions == [
        (root, 0, 0),
        (spool, 0, 0),
        (root, 0o700),
        (spool, 0o700),
    ]


@pytest.mark.skipif(files.os.name == "nt", reason="POSIX-only permission boundary")
def test_publication_spool_rejects_unavailable_root_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(
        files.os,
        "chown",
        lambda *_args: (_ for _ in ()).throw(OSError("operation not permitted")),
    )

    with pytest.raises(IncomingFileError, match="publication spool was unavailable"):
        files._publication_spool_root(tmp_path, "ally")


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_preparation_and_freeze_reject_bad_sources_and_release_reservations(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    (workspace / "empty.csv").write_bytes(b"")

    with pytest.raises(IncomingFileError, match="unsafe"):
        prepare_publication_files(workspace, ["empty.csv"])

    (workspace / "result.csv").write_bytes(b"content")
    monkeypatch.setattr(
        files,
        "_copy_publication_file",
        lambda *_args: (_ for _ in ()).throw(IncomingFileError("copy rejected")),
    )
    with pytest.raises(IncomingFileError, match="copy rejected"):
        freeze_publication(workspace, str(uuid4()), ["result.csv"])

    ledger = json.loads((tmp_path / ".allies-publication-ledger.json").read_text())
    assert ledger["records"] == {}

    monkeypatch.setattr(
        files,
        "_copy_publication_file",
        lambda *_args: (_ for _ in ()).throw(OSError("disk failed")),
    )
    with pytest.raises(IncomingFileError, match="could not be committed"):
        freeze_publication(workspace, str(uuid4()), ["result.csv"])

    assert json.loads((tmp_path / ".allies-publication-ledger.json").read_text())["records"] == {}


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_spool_paths_reject_missing_or_unsafe_state_and_release_is_idempotent(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "result.csv").write_bytes(b"content")
    manifest = freeze_publication(workspace, str(uuid4()), ["result.csv"])

    with pytest.raises(IncomingFileError, match="file was unavailable"):
        publication_spool_path(workspace, manifest.publication_id, str(uuid4()))

    frozen = publication_spool_path(
        workspace, manifest.publication_id, manifest.files[0].source_version_id
    )
    frozen.unlink()
    with pytest.raises(IncomingFileError, match="snapshot was unavailable"):
        publication_spool_path(
            workspace, manifest.publication_id, manifest.files[0].source_version_id
        )

    release_publication_spool(workspace, manifest.publication_id)
    release_publication_spool(workspace, manifest.publication_id)

    unsafe = freeze_publication(workspace, str(uuid4()), ["result.csv"])
    directory = tmp_path / ".allies-publications" / "ally" / unsafe.publication_id
    files.shutil.rmtree(directory)
    directory.write_text("not a spool", encoding="utf-8")
    with pytest.raises(IncomingFileError, match="unsafe"):
        release_publication_spool(workspace, unsafe.publication_id)


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_recovery_scans_bounded_valid_manifests_and_rejects_unsafe_roots(tmp_path):
    workspace = _workspace(tmp_path)
    assert recover_publication_manifests(workspace) == ()
    with pytest.raises(IncomingFileError, match="limit"):
        recover_publication_manifests(workspace, True)

    root = tmp_path / ".allies-publications" / "ally"
    root.mkdir(parents=True)
    (root / "not-a-publication.json").write_text("{}", encoding="utf-8")
    assert recover_publication_manifests(workspace) == ()

    (workspace / "first.csv").write_bytes(b"first")
    (workspace / "second.csv").write_bytes(b"second")
    freeze_publication(workspace, str(uuid4()), ["first.csv"])
    freeze_publication(workspace, str(uuid4()), ["second.csv"])
    assert len(recover_publication_manifests(workspace, 1)) == 1

    unsafe_root = _workspace(tmp_path / "unsafe")
    (unsafe_root.parents[2] / ".allies-publications").mkdir()
    unsafe = unsafe_root.parents[2] / ".allies-publications" / "ally"
    unsafe.write_text("not a directory", encoding="utf-8")
    with pytest.raises(IncomingFileError, match="unsafe"):
        recover_publication_manifests(unsafe_root)


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_profile_cleanup_removes_only_its_ledger_records(tmp_path):
    workspace = _workspace(tmp_path, "ally")
    (workspace / "result.csv").write_bytes(b"content")
    manifest = freeze_publication(workspace, str(uuid4()), ["result.csv"])

    with pytest.raises(IncomingFileError, match="identity"):
        cleanup_profile_publication_spools(tmp_path, "ally/other")

    cleanup_profile_publication_spools(tmp_path, "ally")

    assert not (tmp_path / ".allies-publications" / "ally").exists()
    assert manifest.publication_id not in json.loads(
        (tmp_path / ".allies-publication-ledger.json").read_text()
    )["records"]


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_reconciliation_repairs_a_wrong_ledger_record_and_bounds_invalid_input(tmp_path):
    assert reconcile_publication_spools(tmp_path) == 0
    with pytest.raises(IncomingFileError, match="limit"):
        reconcile_publication_spools(tmp_path, False)

    workspace = _workspace(tmp_path)
    (workspace / "result.csv").write_bytes(b"content")
    manifest = freeze_publication(workspace, str(uuid4()), ["result.csv"])
    ledger_path = tmp_path / ".allies-publication-ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["records"][manifest.publication_id].update(
        profile="wrong-profile", size=1, state="copying"
    )
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    (tmp_path / ".allies-publications" / "not-a-profile").write_text(
        "ignore", encoding="utf-8"
    )

    assert reconcile_publication_spools(tmp_path) == 1
    repaired = json.loads(ledger_path.read_text())["records"][manifest.publication_id]
    assert repaired["profile"] == "ally"
    assert repaired["state"] == "frozen"


def test_partial_cleanup_keeps_journaled_work_and_releases_only_deleted_copy_reservations(
    tmp_path,
):
    root = tmp_path / ".allies-publications" / "ally"
    root.mkdir(parents=True)
    journaled_id = str(uuid4())
    stale_id = str(uuid4())
    (root / f"{journaled_id}.json").write_text("{}", encoding="utf-8")
    stale = root / f".{stale_id}.partial.copy"
    stale.mkdir()
    files._write_ledger(
        tmp_path,
        {
            journaled_id: {
                "profile": "ally",
                "size": 3,
                "state": "copying",
                "updated_at": 0,
            },
            stale_id: {
                "profile": "ally",
                "size": 3,
                "state": "copying",
                "updated_at": 0,
            },
        },
    )

    with pytest.raises(IncomingFileError, match="limit"):
        cleanup_stale_publication_copies(tmp_path, limit=False)
    with pytest.raises(IncomingFileError, match="time"):
        cleanup_stale_publication_copies(tmp_path, now=True)

    assert cleanup_stale_publication_copies(tmp_path, now=24 * 60 * 60 + 1) == 1
    assert not stale.exists()
    assert journaled_id in json.loads(
        (tmp_path / ".allies-publication-ledger.json").read_text()
    )["records"]


def test_incoming_file_contract_rejects_malformed_metadata_and_context():
    descriptor = _descriptor()

    for value in ([], ["not a descriptor"], [{**descriptor, "file_id": "not-a-uuid"}]):
        with pytest.raises(IncomingFileError):
            parse_incoming_files(value)

    with pytest.raises(IncomingFileError):
        validate_hermes_file_context(None)
    with pytest.raises(IncomingFileError):
        validate_hermes_file_context(
            {"schema_version": "v2", "kind": "allies_incoming_files", "files": []}
        )
    with pytest.raises(IncomingFileError):
        validate_hermes_file_context(
            {"schema_version": "v1", "kind": "allies_incoming_files", "files": "bad"}
        )


@pytest.mark.asyncio
async def test_stage_rejects_non_streaming_or_invalid_stream_chunks(tmp_path):
    workspace = _workspace(tmp_path)
    descriptor = _descriptor(b"content")

    with pytest.raises(IncomingFileError, match="transport was invalid"):
        await stage_incoming_files(
            workspace, str(uuid4()), [descriptor], lambda _descriptor: object()
        )

    async def bad_chunks(_descriptor):
        yield b""

    with pytest.raises(IncomingFileError, match="transport was invalid"):
        await stage_incoming_files(workspace, str(uuid4()), [descriptor], bad_chunks)

    async def oversized_chunks(_descriptor):
        yield b"too many bytes"

    with pytest.raises(IncomingFileError, match="size did not match"):
        await stage_incoming_files(
            workspace, str(uuid4()), [descriptor], oversized_chunks
        )

    async def failing_chunks(_descriptor):
        raise OSError("network read failed")
        yield b"unreachable"

    with pytest.raises(IncomingFileError, match="transport failed"):
        await stage_incoming_files(workspace, str(uuid4()), [descriptor], failing_chunks)


@pytest.mark.asyncio
async def test_stage_cleans_a_committed_copy_when_its_receipt_cannot_be_written(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    descriptor = _descriptor(b"content")

    async def chunks(_descriptor):
        yield b"content"

    monkeypatch.setattr(
        files.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(IncomingFileError, match="staging failed"):
        await stage_incoming_files(workspace, str(uuid4()), [descriptor], chunks)

    attachments = workspace / "attachments"
    assert not attachments.exists() or not list(attachments.iterdir())


@pytest.mark.asyncio
async def test_stage_rejects_a_corrupt_durable_receipt(tmp_path):
    workspace = _workspace(tmp_path)
    command_id = str(uuid4())
    receipt = workspace.parent / ".allies-incoming-receipts" / f"{command_id}.json"
    receipt.parent.mkdir()
    receipt.write_text("not json", encoding="utf-8")

    async def chunks(_descriptor):
        yield b"content"

    with pytest.raises(IncomingFileError, match="receipt was invalid"):
        await stage_incoming_files(workspace, command_id, [_descriptor()], chunks)


def test_publication_copy_checks_paths_and_detects_source_changes(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    destination = tmp_path / "spool"
    destination.mkdir()

    for value in ("../result.csv", ["result.csv", "result.csv"], [1]):
        with pytest.raises(IncomingFileError):
            prepare_publication_files(workspace, value)
    with pytest.raises(IncomingFileError, match="source was unavailable"):
        files._copy_publication_file(workspace, Path("missing.csv"), destination, 1)

    source = workspace / "result.csv"
    source.write_bytes(b"content")
    monkeypatch.setattr(
        files.os,
        "open",
        lambda *_args: (_ for _ in ()).throw(OSError("open failed")),
    )
    with pytest.raises(IncomingFileError, match="source was unavailable"):
        files._copy_publication_file(workspace, Path("result.csv"), destination, 1)

    monkeypatch.undo()
    checks = iter((True, False))
    monkeypatch.setattr(files.os.path, "samestat", lambda *_args: next(checks))
    with pytest.raises(IncomingFileError, match="changed during copy"):
        files._copy_publication_file(workspace, Path("result.csv"), destination, 1)
    assert not list(destination.iterdir())


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_publication_manifest_reader_rejects_corrupt_durable_journals(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "result.csv").write_bytes(b"content")
    manifest = freeze_publication(workspace, str(uuid4()), ["result.csv"])
    journal = tmp_path / ".allies-publications" / "ally" / f"{manifest.publication_id}.json"
    base = json.loads(journal.read_text())

    journal.write_text("not json", encoding="utf-8")
    with pytest.raises(IncomingFileError, match="journal was invalid"):
        files._read_publication_manifest(journal, manifest.publication_id, None)
    journal.unlink()
    journal.mkdir()
    with pytest.raises(IncomingFileError, match="journal was invalid"):
        files._read_publication_manifest(journal, manifest.publication_id, None)
    journal.rmdir()

    variants = [{}]
    wrong_schema = deepcopy(base)
    wrong_schema["schema"] = "v0"
    variants.append(wrong_schema)
    bad_paths = deepcopy(base)
    bad_paths["source_paths"] = "result.csv"
    variants.append(bad_paths)
    missing_rows = deepcopy(base)
    missing_rows["files"] = "bad"
    variants.append(missing_rows)
    malformed_row = deepcopy(base)
    malformed_row["files"] = [{}]
    variants.append(malformed_row)
    bad_uuid = deepcopy(base)
    bad_uuid["files"][0]["source_version_id"] = "not-a-uuid"
    variants.append(bad_uuid)
    unsafe_name = deepcopy(base)
    unsafe_name["files"][0]["name"] = ""
    variants.append(unsafe_name)
    changed_digest = deepcopy(base)
    changed_digest["manifest_sha256"] = "b" * 64
    variants.append(changed_digest)

    for value in variants:
        journal.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(IncomingFileError, match="journal was invalid"):
            files._read_publication_manifest(journal, manifest.publication_id, None)

    journal.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(IncomingFileError, match="paths changed"):
        files._read_publication_manifest(
            journal, manifest.publication_id, (Path("other.csv"),)
        )


def test_publication_ledger_reader_rejects_invalid_reservations(tmp_path):
    ledger = tmp_path / ".allies-publication-ledger.json"
    cases = [
        "not json",
        {"schema": "v0", "records": {}},
        {"schema": "v1", "records": []},
        {"schema": "v1", "records": {"id": "bad"}},
        {
            "schema": "v1",
            "records": {
                "id": {"profile": 1, "size": False, "state": "bad", "updated_at": -1}
            },
        },
    ]

    for value in cases:
        ledger.write_text(
            value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
        )
        with pytest.raises(IncomingFileError, match="reservation journal was invalid"):
            files._read_ledger(tmp_path)


def test_publication_reservation_enforces_each_shared_volume_bound(tmp_path, monkeypatch):
    def record(profile="ally", size=1, state="frozen"):
        return {"profile": profile, "size": size, "state": state, "updated_at": 0}

    same_root = tmp_path / "same"
    files._write_ledger(same_root, {"same": record()})
    files._reserve_publication(same_root, "ally", "same", 1)
    with pytest.raises(IncomingFileError, match="conflicted"):
        files._reserve_publication(same_root, "ally", "same", 2)

    profile_count_root = tmp_path / "profile-count"
    files._write_ledger(
        profile_count_root,
        {f"record-{index}": record() for index in range(files.MAX_PUBLICATION_MANIFESTS)},
    )
    with pytest.raises(IncomingFileError, match="capacity"):
        files._reserve_publication(profile_count_root, "ally", "new", 1)

    incomplete_root = tmp_path / "incomplete"
    files._write_ledger(
        incomplete_root,
        {f"copy-{index}": record(state="copying") for index in range(files.MAX_PUBLICATION_INCOMPLETE)},
    )
    with pytest.raises(IncomingFileError, match="capacity"):
        files._reserve_publication(incomplete_root, "ally", "new", 1)

    profile_size_root = tmp_path / "profile-size"
    files._write_ledger(
        profile_size_root, {"full": record(size=files.MAX_PROFILE_PUBLICATION_BYTES)}
    )
    with pytest.raises(IncomingFileError, match="capacity"):
        files._reserve_publication(profile_size_root, "ally", "new", 1)

    volume_size_root = tmp_path / "volume-size"
    files._write_ledger(
        volume_size_root,
        {"full": record(profile="other", size=files.MAX_VOLUME_PUBLICATION_BYTES)},
    )
    with pytest.raises(IncomingFileError, match="capacity"):
        files._reserve_publication(volume_size_root, "ally", "new", 1)

    free_space_root = tmp_path / "free-space"
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(IncomingFileError, match="capacity"):
        files._reserve_publication(free_space_root, "ally", "new", 1)


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_publication_spool_safety_checks_reject_unsafe_cleanup_and_bad_inputs(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    (workspace / "result.csv").write_bytes(b"content")
    monkeypatch.setattr(
        files,
        "_publication_source",
        lambda *_args: (_ for _ in ()).throw(OSError("lost source")),
    )
    with pytest.raises(IncomingFileError, match="source was unavailable"):
        prepare_publication_files(workspace, ["result.csv"])
    monkeypatch.undo()

    root = tmp_path / ".allies-publications"
    root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(IncomingFileError, match="unsafe"):
        reconcile_publication_spools(tmp_path)
    with pytest.raises(IncomingFileError, match="unsafe"):
        cleanup_stale_publication_copies(tmp_path)


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_publication_freeze_and_journal_transitions_reject_stale_or_missing_state(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    (workspace / "result.csv").write_bytes(b"content")
    publication_id = str(uuid4())
    stale = tmp_path / ".allies-publications" / "ally" / publication_id
    stale.mkdir(parents=True)

    with pytest.raises(IncomingFileError, match="spool state was invalid"):
        freeze_publication(workspace, publication_id, ["result.csv"])
    assert json.loads((tmp_path / ".allies-publication-ledger.json").read_text())["records"] == {}

    files._reserve_publication(tmp_path, "ally", publication_id, 1)
    files._mark_publication_frozen(tmp_path, publication_id)
    files._release_publication(tmp_path, publication_id)
    files._release_publication(tmp_path, publication_id)


def test_publication_metadata_and_receipt_safety_reject_total_overflow_and_directories(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    receipt = workspace.parent / ".allies-incoming-receipts" / "directory.json"
    receipt.mkdir(parents=True)
    with pytest.raises(IncomingFileError, match="receipt was invalid"):
        files._read_receipt(receipt)

    with pytest.raises(IncomingFileError, match="workspace was unavailable"):
        prepare_publication_files(tmp_path, ["result.csv"])


def test_publication_and_incoming_metadata_bounds_are_enforced_without_files(
    tmp_path, monkeypatch
):
    descriptor = _descriptor()
    oversized = []
    for _index in range(3):
        row = dict(descriptor)
        row["file_id"] = str(uuid4())
        row["size"] = files.MAX_PUBLICATION_FILE_BYTES
        oversized.append(row)
    with pytest.raises(IncomingFileError, match="invalid"):
        parse_incoming_files(oversized)

    with pytest.raises(IncomingFileError, match="context was invalid"):
        validate_hermes_file_context(
            {
                "schema_version": "v1",
                "kind": "allies_incoming_files",
                "files": [{**descriptor, "path": "outside.csv"}],
            }
        )

    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(
        files.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(IncomingFileError, match="could not be committed"):
        files._write_receipt(receipt, {"schema": "v1"})


def test_incoming_receipt_recovery_rejects_each_untrusted_component(tmp_path):
    workspace = _workspace(tmp_path)
    descriptor = parse_incoming_files([_descriptor()])[0]
    command_id = str(uuid4())
    digest = files._manifest_sha256((descriptor,))
    row = {
        **descriptor.manifest_value(),
        "path": f"attachments/turn/{descriptor.file_id}",
    }
    receipt = {
        "schema": files._RECEIPT_SCHEMA,
        "command_id": command_id,
        "manifest_sha256": digest,
        "files": [row],
    }

    assert files._receipt_manifest({}, command_id, digest, (descriptor,)) is None
    changed_command = dict(receipt, command_id=str(uuid4()))
    with pytest.raises(IncomingFileError, match="conflicted"):
        files._receipt_manifest(changed_command, command_id, digest, (descriptor,))
    assert (
        files._receipt_manifest(
            dict(receipt, files="bad"), command_id, digest, (descriptor,)
        )
        is None
    )
    assert (
        files._receipt_manifest(
            dict(receipt, files=[{}]), command_id, digest, (descriptor,)
        )
        is None
    )
    mismatched = dict(row, name="other.csv")
    with pytest.raises(IncomingFileError, match="conflicted"):
        files._receipt_manifest(
            dict(receipt, files=[mismatched]), command_id, digest, (descriptor,)
        )
    assert (
        files._receipt_manifest(
            dict(receipt, files=[dict(row, path="outside.csv")]),
            command_id,
            digest,
            (descriptor,),
        )
        is None
    )

    staged = files.StagedFile(
        descriptor=descriptor, path=f"attachments/turn/{descriptor.file_id}"
    )
    assert not files._staged_files_match(workspace, (staged,))
    target = workspace / staged.path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"wrong")
    assert not files._staged_files_match(workspace, (staged,))


def test_publication_copy_and_reservation_helpers_fence_unavailable_state(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    source = workspace / "result.csv"
    source.write_bytes(b"content")
    destination = tmp_path / "destination"
    destination.mkdir()

    monkeypatch.setattr(files.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(IncomingFileError, match="capacity"):
        files._copy_publication_file(workspace, Path("result.csv"), destination, 1)
    assert not list(destination.iterdir())

    (workspace / "plain").write_bytes(b"not a directory")
    with pytest.raises(IncomingFileError, match="unsafe"):
        files._publication_source(workspace, Path("plain/child.csv"))
    with pytest.raises(IncomingFileError, match="reservation was unavailable"):
        files._mark_publication_frozen(tmp_path, str(uuid4()))
    with pytest.raises(IncomingFileError, match="timed out"):
        files._check_deadline(0, lambda: 2, 1)


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_reconciliation_completes_an_interrupted_durable_spool_release(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    (workspace / "result.csv").write_bytes(b"content")
    manifest = freeze_publication(workspace, str(uuid4()), ["result.csv"])
    spool_root = tmp_path / ".allies-publications" / "ally"
    original = files._complete_publication_release

    def interrupted_release(root, publication_id):
        files.shutil.rmtree(root / publication_id)
        raise OSError("process stopped")

    monkeypatch.setattr(files, "_complete_publication_release", interrupted_release)
    with pytest.raises(OSError, match="process stopped"):
        release_publication_spool(workspace, manifest.publication_id)

    ledger_path = tmp_path / ".allies-publication-ledger.json"
    assert json.loads(ledger_path.read_text())["records"][manifest.publication_id][
        "state"
    ] == "releasing"
    assert (spool_root / f"{manifest.publication_id}.json").exists()

    monkeypatch.setattr(files, "_complete_publication_release", original)
    assert reconcile_publication_spools(tmp_path) == 0
    assert not (spool_root / manifest.publication_id).exists()
    assert not (spool_root / f"{manifest.publication_id}.json").exists()
    assert json.loads(ledger_path.read_text())["records"] == {}


@pytest.mark.usefixtures("root_owned_publication_spool")
def test_reconciliation_releases_a_completed_spool_before_ledger_cleanup(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "result.csv").write_bytes(b"content")
    manifest = freeze_publication(workspace, str(uuid4()), ["result.csv"])
    files._mark_publication_releasing(tmp_path, "ally", manifest.publication_id)
    files.shutil.rmtree(tmp_path / ".allies-publications")

    assert reconcile_publication_spools(tmp_path) == 0
    ledger = json.loads((tmp_path / ".allies-publication-ledger.json").read_text())
    assert ledger["records"] == {}


def test_reconciliation_rejects_releasing_ledger_path_escape(tmp_path):
    files._write_ledger(
        tmp_path,
        {
            str(uuid4()): {
                "profile": "..",
                "size": 1,
                "state": "releasing",
                "updated_at": 0,
            }
        },
    )

    with pytest.raises(IncomingFileError, match="reservation journal was invalid"):
        reconcile_publication_spools(tmp_path)


def test_reconciliation_rejects_an_unreadable_spool_root(tmp_path, monkeypatch):
    root = tmp_path / ".allies-publications"
    root.mkdir()
    original_iterdir = Path.iterdir

    def unreadable_iterdir(path):
        if path == root:
            raise OSError("device unavailable")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", unreadable_iterdir)
    with pytest.raises(IncomingFileError, match="publication spool was unavailable"):
        reconcile_publication_spools(tmp_path)


def test_reconciliation_skips_an_unreadable_profile_spool(tmp_path, monkeypatch):
    profile = tmp_path / ".allies-publications" / "ally"
    profile.mkdir(parents=True)
    original_glob = Path.glob

    def unreadable_glob(path, pattern):
        if path == profile:
            raise OSError("device unavailable")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", unreadable_glob)
    assert reconcile_publication_spools(tmp_path) == 0
