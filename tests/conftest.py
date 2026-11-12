from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_bytes():
    return lambda name: (FIX / name).read_bytes()


@pytest.fixture
def fixture_text():
    return lambda name: (FIX / name).read_text(encoding="utf-8")
