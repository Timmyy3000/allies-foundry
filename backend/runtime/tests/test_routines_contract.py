import hashlib
import json
from pathlib import Path

import pytest

from runtime.exceptions import RuntimeValidationError
from runtime.routine_contracts import parse_routine_message, routine_fingerprint

CONTRACT_ROOT = Path(__file__).resolve().parents[3] / "docs" / "contracts"


def fixture() -> dict:
    return json.loads(
        (CONTRACT_ROOT / "fixtures" / "routines-v1.json").read_text(encoding="utf-8")
    )


def test_released_contract_tuple_is_byte_stable():
    expected = {
        "routines-v1.md": "f05ab0a1baf63551f426c288f0144484c813b5cda535bf1cf5d614fb7a22ea84",
        "fixtures/routines-v1.json": "2660d30ee73e8f3cebf94340ea1169019e3c937fd01a30bff6d0e16ecd54ab35",
        "routines-v1.lock.json": "9005d25a8186d325a2efdcc42ec9e6cfe4d2d6c445088c9ba7a6b4d87429cb04",
    }
    for relative, digest in expected.items():
        assert hashlib.sha256((CONTRACT_ROOT / relative).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    ("section", "name"),
    (
        ("dispatch", "command"),
        ("dispatch", "receipt"),
        ("result", "event"),
        ("result", "receipt"),
        ("approval", "requested"),
        ("approval", "decision"),
        ("approval", "receipt"),
        ("approval", "cancel_wait"),
    ),
)
def test_fixture_message_is_strictly_typed_and_fingerprint_stable(section, name):
    message = fixture()[section][name]
    parsed = parse_routine_message(message)
    assert parsed.fingerprint == routine_fingerprint(parsed)


def test_result_preserves_dispatch_title_and_revision_snapshot():
    dispatch = parse_routine_message(fixture()["dispatch"]["command"])
    result = parse_routine_message(fixture()["result"]["event"])

    assert result.routine_revision == dispatch.routine_revision
    assert result.title_snapshot == dispatch.title_snapshot


def test_revision14_schedule_and_identity_vectors_are_explicit():
    released = fixture()
    assert released["contract"]["content_revision"] == 14
    assert [
        released["management"][operation]["receipt"]["schedule_generation"]
        for operation in ("create", "update", "pause", "resume")
    ] == [1, 2, 3, 4]

    required_dispatch = set(released["correlation"]["required_dispatch"])
    assert released["correlation"]["required_dispatch_receipt"] == [
        "execution_id",
        "attempt_id",
        "generation",
    ]
    assert required_dispatch.isdisjoint(
        released["correlation"]["required_dispatch_receipt"]
    )
    assert released["dispatch"]["command"]["schedule"]["timezone"] == "Europe/Berlin"
    assert all(
        message["service_identity"] == "foundry-service"
        for section in ("result", "approval")
        for name, message in released[section].items()
        if isinstance(message, dict) and message.get("producer") == "foundry"
    )

    constraint_cases = [
        case for case in released["cases"] if case["case_id"].startswith("constraint-")
    ]
    assert len(constraint_cases) == 3
    assert all(
        "expected_constraint_outcome" in case
        and "expected_result_code" not in case
        for case in constraint_cases
    )


def test_unknown_fields_and_kinds_fail_closed():
    message = dict(fixture()["dispatch"]["command"])
    message["unexpected"] = True
    with pytest.raises(RuntimeValidationError):
        parse_routine_message(message)
    message = dict(fixture()["dispatch"]["command"])
    message["kind"] = "execution.command"
    with pytest.raises(RuntimeValidationError):
        parse_routine_message(message)


def test_routine_prompt_and_event_envelope_bounds_are_utf8_bytes():
    message = dict(fixture()["dispatch"]["command"])
    message["execution_prompt"] = "é" * (16 * 1024)
    message["fingerprint"] = routine_fingerprint(message)
    with pytest.raises(RuntimeValidationError):
        parse_routine_message(message)
