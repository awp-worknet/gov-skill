"""nonce floor — atomic increment + bump_to behavior."""

import os
import pytest

from lib import nonce


@pytest.fixture
def isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVNET_NONCE_DIR", str(tmp_path))
    yield tmp_path


def test_first_call_returns_one(isolated_dir):
    p = "0x" + "1" * 40
    assert nonce.next_nonce(p) == 1


def test_subsequent_calls_strictly_greater(isolated_dir):
    p = "0x" + "2" * 40
    a = nonce.next_nonce(p)
    b = nonce.next_nonce(p)
    c = nonce.next_nonce(p)
    assert (a, b, c) == (1, 2, 3)


def test_address_case_insensitive(isolated_dir):
    upper = "0x" + "A" * 40
    lower = "0x" + "a" * 40
    assert nonce.next_nonce(upper) == 1
    # The lowercase form of the same address should share the floor — the second call returns 2
    assert nonce.next_nonce(lower) == 2


def test_bump_to_advances_when_server_ahead(isolated_dir):
    p = "0x" + "3" * 40
    assert nonce.next_nonce(p) == 1
    # Server stored=42 — next local call should return 43
    new_floor = nonce.bump_to(p, 42)
    assert new_floor == 43
    # Subsequent next_nonce reads read_floor + 1 = 44
    assert nonce.next_nonce(p) == 44


def test_bump_to_no_op_when_local_ahead(isolated_dir):
    p = "0x" + "4" * 40
    nonce.next_nonce(p)
    nonce.next_nonce(p)
    nonce.next_nonce(p)  # local floor = 3
    # Server is only at 1 — local advances to max(3, 1) + 1 = 4
    assert nonce.bump_to(p, 1) == 4


def test_reset_clears_file(isolated_dir):
    p = "0x" + "5" * 40
    nonce.next_nonce(p)
    nonce.reset(p)
    assert nonce.next_nonce(p) == 1


def test_reset_to_value(isolated_dir):
    p = "0x" + "6" * 40
    nonce.reset(p, 100)
    assert nonce.next_nonce(p) == 101


def test_corrupt_file_recovers_from_zero(isolated_dir, tmp_path):
    p = "0x" + "7" * 40
    nonce_file = tmp_path / f"{p.lower()}.json"
    nonce_file.write_text("not-json")
    # read_floor returns 0; next_nonce returns 1 and overwrites
    assert nonce.next_nonce(p) == 1
