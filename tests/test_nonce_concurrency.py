"""Concurrent nonce allocation — confirms fcntl.flock really stops two processes from getting the same value.

Without a lock, two processes each do read_floor() → stale value, +1, write
back → both use the same nonce, the server rejects the second. With flock,
N concurrent processes should produce N consecutive integers with no duplicates.
"""

import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest


def _child(args):
    nonce_dir, principal = args
    os.environ["GOVNET_NONCE_DIR"] = str(nonce_dir)
    # The subprocess needs to add lib to its path again
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from lib import nonce  # type: ignore
    return nonce.next_nonce(principal)


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl unavailable on Windows")
def test_concurrent_next_nonce_is_unique(tmp_path):
    """50 concurrent processes each allocate one nonce → should get exactly the set 1..50 (no duplicates, no losses)."""
    principal = "0x" + "ab" * 20
    n = 50
    with mp.get_context("fork").Pool(processes=10) as pool:
        results = pool.map(_child, [(str(tmp_path), principal)] * n)
    assert len(set(results)) == n, f"duplicate nonces: {sorted(results)}"
    assert sorted(results) == list(range(1, n + 1)), f"got {sorted(results)}"


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl unavailable on Windows")
def test_concurrent_bump_to_does_not_lose_floor(tmp_path):
    """Set the floor to 100, then concurrent next_nonce should return 101..150."""
    principal = "0x" + "cd" * 20
    os.environ["GOVNET_NONCE_DIR"] = str(tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from lib import nonce  # type: ignore
    nonce.reset(principal, 100)

    n = 50
    with mp.get_context("fork").Pool(processes=10) as pool:
        results = pool.map(_child, [(str(tmp_path), principal)] * n)
    assert sorted(results) == list(range(101, 101 + n))
