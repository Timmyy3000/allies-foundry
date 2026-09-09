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
        "routines-v1.md": "ba05f7ee14a958524462cc31e83a05fd9f16b242efc6fea180a12ae40015547d",
        "fixtures/routines-v1.json": "bc2fa9979a89f71ec57544bc07aa326e1bdc7b084f9c0ab1cee4c934a67d4a0a",
        "routines-v1.lock.json": "ba9c2f5b3caf20ebbeae8e26c99740eb74402d84253341a137e5761e447d67cc",
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
