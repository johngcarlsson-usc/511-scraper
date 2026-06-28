import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def open511_payload() -> dict:
    return _load("open511_sample.json")


@pytest.fixture
def wzdx_payload() -> dict:
    return _load("wzdx_sample.json")


@pytest.fixture
def ibi511_events() -> list:
    return _load("ibi511_events.json")


@pytest.fixture
def ibi511_speeds() -> list:
    return _load("ibi511_speeds.json")

