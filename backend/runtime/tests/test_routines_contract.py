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
        "routines-v1.md": "9e355d7b8ead4d675cd79fef766faa634069117acb0e9927ad02efd8fe202cdc",
        "fixtures/routines-v1.json": "70028e3e1935fc79b4d6bd4127facb4501c0a97492a808345921f423ec55cec8",
        "routines-v1.lock.json": "488bad850829212951991f78e34cfd00b3f575969385a35af36761a87c7278e8",
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
