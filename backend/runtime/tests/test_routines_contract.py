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
        "routines-v1.md": "0f3ba80c9331914c18d5ff4b7b0358d2afd832a12f7d068213ab1a49f8f32fbf",
        "fixtures/routines-v1.json": "4b6ea7e917ef7df1e5a50240e6c2a87c0ba6437340de5ff750ea61697ebe6492",
        "routines-v1.lock.json": "8027382ca5494ec41a54eab18f3f534228a89d63bf31cf6c0112502d46d92bc2",
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
