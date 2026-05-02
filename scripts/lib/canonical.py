"""查询字符串规范化 + EIP-712 字节级原语 — 镜像 crates/emg-auth/src/canonical.rs。

签名前必须把 query 规范化为：
1. 按 RFC 3986 percent-decode 每个键值对的两侧。
2. 按 (key, value) 升序排序。
3. 按 RFC 3986 component encoding 重新 percent-encode（小写十六进制）。
4. 用 `&` 拼回，键值之间用 `=`，没有 query 时返回空串。

服务端把签名材料里的 `query` 字段当成上述规范化结果。任何漂移都会导致
`AUTH_SIGNATURE_INVALID`，所以这里逐字节复刻 Rust 实现，并在 tests/
里跑相同的已知答案向量验证。

另外暴露 `rust_decimal_serialize` — 投票内层 `EMGVote.voteHash` 需要把
向量先按 `rust_decimal::Decimal::serialize` 的 16 字节二进制格式打包，
再拼上 4 字节 LE u32 length prefix 后做 keccak256。两端字节漂移就拿不
到匹配的 hash，整个投票会被服务端 ecrecover 拒掉。
"""

from __future__ import annotations

import hashlib
import struct
from decimal import Decimal
from typing import List, Sequence, Tuple

# eth_hash 是同步 keccak256 的零依赖封装，pyproject 里固定了 0.7+。
from eth_hash.auto import keccak as _keccak


class CanonicalError(ValueError):
    """规范化失败 — 截断的 percent 转义、非十六进制字符或非 UTF-8 字节。"""


def keccak256(data: bytes) -> bytes:
    """返回 32 字节 Keccak-256 摘要。空输入也返回 32 字节摘要。"""
    return _keccak(data)


def eip712_body_hash(body: bytes) -> bytes:
    """EIP-712 信封里的 `bodyHash` 字段。

    空 body 返回 32 字节零（与服务端 `B256::ZERO` 哨兵一致），非空 body
    返回 `keccak256(body)`。**不要** 用 SHA-256；幂等性 cache 才用 SHA-256。
    """
    if not body:
        return b"\x00" * 32
    return keccak256(body)


def idempotency_body_hash(body: bytes) -> str:
    """SHA-256 摘要，与 EIP-712 的 keccak256 严格区分（spec §9.4 步骤 1）。

    **当前 skill 不直接调用本函数** —— 服务端用此摘要作为 idempotency
    cache key 的一部分（`(Principal, X-Idempotency-Key, sha256(body))`），
    客户端只需要原样复发同一个 body 即可命中缓存，不必自己算 hash。

    保留这个函数是因为：
      - spec §9.4 把它列为客户端"应当能算"的 reference 实现
      - tests/test_canonical.py 用它锚定服务端兼容性（防止未来误把
        keccak256 当成 idempotency hash —— 两个用不同算法是 spec 反复
        强调的反混淆点）
      - 将来如果加 client-side dedup / replay-protection 工具，可以直接复用
    """
    return "0x" + hashlib.sha256(body).hexdigest()


_UNRESERVED = (
    set(range(ord("0"), ord("9") + 1))
    | set(range(ord("A"), ord("Z") + 1))
    | set(range(ord("a"), ord("z") + 1))
    | {ord(c) for c in "-._~"}
)


def _hex_value(c: int) -> int:
    if 0x30 <= c <= 0x39:
        return c - 0x30
    if 0x61 <= c <= 0x66:
        return c - 0x61 + 10
    if 0x41 <= c <= 0x46:
        return c - 0x41 + 10
    raise CanonicalError("invalid hex in percent-escape")


def _percent_decode(s: str) -> str:
    # Rust 实现里输入是 &str.as_bytes() — 即 UTF-8 字节流。Python 这边先把
    # str encode 成 UTF-8，让裸 unicode 字符（比如 'ä'）按字节走，再解析
    # percent-escape 字节，最后整体 decode 回 utf-8 字符串。
    raw = s.encode("utf-8")
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x25:  # %
            if i + 2 >= n:
                raise CanonicalError("truncated percent-escape sequence")
            hi = _hex_value(raw[i + 1])
            lo = _hex_value(raw[i + 2])
            out.append((hi << 4) | lo)
            i += 3
        else:
            out.append(b)
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CanonicalError("percent-decoded bytes are not valid UTF-8") from e


def _percent_encode(s: str) -> str:
    out: List[str] = []
    for b in s.encode("utf-8"):
        if b in _UNRESERVED:
            out.append(chr(b))
        else:
            out.append("%{:02x}".format(b))
    return "".join(out)


def canonicalize_query(query: str) -> str:
    """规范化查询字符串。空输入或只有 `?` 返回 ``""``。

    重要：`+` 视为字面量加号（编码为 `%2b`），不要套用 form-urlencoded 的
    "加号即空格" 语义。规则与服务端 `crates/emg-auth/src/canonical.rs` 完全对齐。
    """
    if query.startswith("?"):
        query = query[1:]
    if not query:
        return ""
    pairs: List[Tuple[str, str]] = []
    for kv in query.split("&"):
        eq = kv.find("=")
        if eq == -1:
            raw_k, raw_v = kv, ""
        else:
            raw_k, raw_v = kv[:eq], kv[eq + 1 :]
        pairs.append((_percent_decode(raw_k), _percent_decode(raw_v)))
    pairs.sort()
    return "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in pairs)


def rust_decimal_serialize(d: Decimal) -> bytes:
    """复刻 `rust_decimal::Decimal::serialize` 的 16 字节小端二进制 layout。

    实际字节顺序（与 paupino/rust-decimal 1.x 的 `serialize()` 对齐，已用
    真 Rust 程序在 cargo 1.94 上交叉验证）：
        bytes[0..4]   = flags (u32 LE)：bits 16..23 = scale (0..28)，bit 31 = sign
        bytes[4..8]   = lo    (mantissa 低 32 位)
        bytes[8..12]  = mid   (mantissa 中 32 位)
        bytes[12..16] = hi    (mantissa 高 32 位)

    `Decimal("0.5").serialize()` 在 Rust 端给出 `00000100 05000000 …`
    —— flags 在最前面，**不是** 最后。早期版本的实现把它写反了，votes
    会被服务端 ecrecover 全部拒掉。

    rust_decimal 的 mantissa 是无符号 96-bit，scale 描述小数点左移位数。
    Python `Decimal.as_tuple()` 给出 `(sign, digits, exponent)`，把 digits
    拼成整数即得 mantissa；scale = -exponent（要在 [0, 28] 范围）。

    特殊处理：负零 `-0` 在 rust_decimal 内部归一化为正零（mantissa==0 时
    丢弃 sign bit），我们在编码时同样归一化，避免与服务端 hash 漂移。

    与 ASCII 文本格式严格不同 —— 这是投票 voteHash / predictionHash 的
    字节级合约，**不可以** 用 `format(d, 'f').encode()` 替代。
    """
    sign, digits, exponent = d.as_tuple()
    if exponent in ("F", "n", "N"):
        raise ValueError(f"non-finite Decimal {d!r} cannot be rust_decimal-encoded")
    scale = -int(exponent)
    if not 0 <= scale <= 28:
        raise ValueError(
            f"Decimal {d!r} has scale {scale}; rust_decimal supports 0..28"
        )
    mantissa = 0
    for digit in digits:
        mantissa = mantissa * 10 + digit
    if mantissa.bit_length() > 96:
        raise ValueError(f"Decimal {d!r} mantissa exceeds 96 bits")
    lo = mantissa & 0xFFFFFFFF
    mid = (mantissa >> 32) & 0xFFFFFFFF
    hi = (mantissa >> 64) & 0xFFFFFFFF
    flags = (scale & 0xFF) << 16
    # rust_decimal 把 -0 归一化为 +0；mantissa==0 时 sign bit 必须置 0
    if sign and mantissa != 0:
        flags |= 0x80000000
    return struct.pack("<IIII", flags, lo, mid, hi)


def canonical_decimal_vector(vec: Sequence[Decimal]) -> bytes:
    """`canonical_bytes(VoteVector)` — 4 字节 LE u32 length + N × rust_decimal 16 字节。

    服务端 `crates/emg-core/src/canonical_vote.rs` 的等价实现。
    `keccak256(canonical_decimal_vector(vec))` 即 EMGVote.voteHash 字段。
    """
    out = bytearray()
    out += struct.pack("<I", len(vec))
    for d in vec:
        out += rust_decimal_serialize(d)
    return bytes(out)


def build_query(params: dict) -> str:
    """从 dict 构造规范化查询字符串。值为 ``None`` 的键被跳过。

    用于 GET 请求 — 既要把 query 拼到 URL 后面发送，也要喂给签名函数。
    返回值不含前导 `?`。
    """
    if not params:
        return ""
    pairs: List[Tuple[str, str]] = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        pairs.append((str(k), str(v)))
    pairs.sort()
    return "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in pairs)
