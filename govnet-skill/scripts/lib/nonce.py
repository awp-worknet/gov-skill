"""Per-Principal nonce floor — 进程间原子分配。

每次签名都要把 EIP-712 信封里的 `nonce` 字段设为严格大于服务端 stored 值
的整数。我们在 `~/.govnet/nonces/<principal>.json` 维护本地下界，保证两
个并发的 skill 调用不会复用同一个 nonce：

- 创建/自增：用一个 sibling lock 文件 (`<principal>.lock`) + `fcntl.flock`
  做进程间互斥锁；锁内做 read-modify-write，然后用 `os.replace` 原子覆盖
  数据文件。两个并发 skill 进程 **不会** 拿到同一个 nonce —— 后到的会
  block 直到先到的写完。
- `fsync` 确保 crash 时不会留下半写状态。
- 与服务端漂移：`AUTH_NONCE_TOO_LOW` / `NONCE_TOO_LOW` 时调用方应
  re-fetch `/v1/auth/info` 拿到 server stored，调用 `bump_to(value)` 把
  本地下界推上去再重试 —— `bump_to` 同样走 flock 路径。
- Windows: `fcntl` 不可用 —— 占位实现回落到无锁，旧的"两个进程同 nonce"
  风险仍在。生产部署应在 Linux/macOS 上跑。

`GOVNET_NONCE_DIR` 可覆盖默认 `~/.govnet/nonces/`。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl  # POSIX-only — Windows 拿不到此模块
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover
    _HAS_FCNTL = False


def _nonce_dir() -> Path:
    base = os.environ.get("GOVNET_NONCE_DIR")
    if base:
        path = Path(base).expanduser()
    else:
        path = Path.home() / ".govnet" / "nonces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _nonce_path(principal: str) -> Path:
    # 全部小写 — 同一个地址的不同大小写形式应该共享同一个 nonce floor。
    return _nonce_dir() / f"{principal.lower()}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, payload: dict) -> None:
    """通过 `os.replace` 写入 — 同 mount point 下的 rename 是原子的。"""
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{time.time_ns()}")
    data = json.dumps(payload).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


@contextlib.contextmanager
def _principal_lock(principal: str) -> Iterator[None]:
    """跨进程互斥 — 进入时 LOCK_EX，退出时自动释放。

    用 sibling `<principal>.lock` 文件（不是数据文件本身），避免 `os.replace`
    覆盖时把 inode 换掉导致锁丢失。Windows 没 fcntl 时降级为无锁。
    """
    if not _HAS_FCNTL:
        yield
        return
    lock_path = _nonce_path(principal).with_suffix(".lock")
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_floor(principal: str) -> int:
    """返回本地存储的 nonce 下界。文件不存在时返回 0（首次签名用 1）。

    不持锁 —— 调用方需要自己保证读后没有其它进程改写。`next_nonce` /
    `bump_to` 在持锁状态下调用本函数。
    """
    path = _nonce_path(principal)
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as f:
            return int(json.load(f)["nonce"])
    except (json.JSONDecodeError, KeyError, ValueError):
        # 文件被腐蚀 — 抹掉重来比静默忽略安全。
        return 0


def next_nonce(principal: str) -> int:
    """读取当前下界 → +1 → 持久化 → 返回新值。

    `_principal_lock` 保证两个并发 skill 进程不会读到相同的旧值。两个并
    发调用现在等同于 sequential：A 拿到 1，B 拿到 2，绝不重复。锁的范围
    不跨网络请求，所以 stale lock（进程崩溃）会随 fd close 自动释放。
    """
    with _principal_lock(principal):
        new = read_floor(principal) + 1
        _atomic_write(
            _nonce_path(principal),
            {"nonce": new, "updated_at": _now_iso()},
        )
    return new


def bump_to(principal: str, server_stored: int) -> int:
    """把本地下界抬到 `server_stored`（如果它更高），然后再 +1 返回。

    在 `AUTH_NONCE_TOO_LOW` 重试路径上用 — 服务端 `/v1/auth/info` 会回
    传当前 stored nonce；本函数原子地把本地推上去再分配下一个。
    """
    with _principal_lock(principal):
        current = read_floor(principal)
        floor = max(current, int(server_stored)) + 1
        _atomic_write(
            _nonce_path(principal),
            {"nonce": floor, "updated_at": _now_iso()},
        )
    return floor


def reset(principal: str, value: Optional[int] = None) -> None:
    """重置（用于测试或显式同步）。`value=None` 删除文件。"""
    with _principal_lock(principal):
        path = _nonce_path(principal)
        if value is None:
            if path.exists():
                path.unlink()
            return
        _atomic_write(path, {"nonce": int(value), "updated_at": _now_iso()})
