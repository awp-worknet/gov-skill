"""并发 nonce 分配 — 确认 fcntl.flock 真正阻止两个进程拿到同一个值。

不持锁的情况下，两个进程各自 read_floor()→旧值, +1, 写回 → 都会用同
一个 nonce，服务端拒第二个。加了 flock 后，N 个并发进程拿到的应该是
N 个连续整数，没有重复。
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
    # subprocess 里需要重新把 lib 加进 path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from lib import nonce  # type: ignore
    return nonce.next_nonce(principal)


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl unavailable on Windows")
def test_concurrent_next_nonce_is_unique(tmp_path):
    """50 个并发进程各分配一个 nonce → 应得 1..50 的精确集合（无重复无丢失）。"""
    principal = "0x" + "ab" * 20
    n = 50
    with mp.get_context("fork").Pool(processes=10) as pool:
        results = pool.map(_child, [(str(tmp_path), principal)] * n)
    assert len(set(results)) == n, f"duplicate nonces: {sorted(results)}"
    assert sorted(results) == list(range(1, n + 1)), f"got {sorted(results)}"


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl unavailable on Windows")
def test_concurrent_bump_to_does_not_lose_floor(tmp_path):
    """先把 floor 设到 100，并发 next_nonce 应得 101..150。"""
    principal = "0x" + "cd" * 20
    os.environ["GOVNET_NONCE_DIR"] = str(tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from lib import nonce  # type: ignore
    nonce.reset(principal, 100)

    n = 50
    with mp.get_context("fork").Pool(processes=10) as pool:
        results = pool.map(_child, [(str(tmp_path), principal)] * n)
    assert sorted(results) == list(range(101, 101 + n))
