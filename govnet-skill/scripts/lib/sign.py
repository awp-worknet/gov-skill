"""EMG-SIG-V1 EIP-712 签名 — typed data 构造 + awp-wallet 桥接。

`sign_emg_request()` 是对外的唯一入口。它做三件事：
1. 用 keccak256 计算 bodyHash（空 body 用 32 字节零）。
2. 构造与 `crates/emg-auth/src/eip712.rs` 完全一致的 typed data JSON。
3. 通过 `awp-wallet sign-typed-data --data <json>` 拿到 65 字节签名。

我们 **不** 在 Python 里直接持有私钥；签名 ALWAYS 通过 awp-wallet。
但为了在 tests/ 里跑已知答案数字摘要测试，我们还提供了
`compute_eip712_digest()`，它本地用 eth_account 计算同样的摘要 — 这条路径
不会接触私钥，纯粹用于交叉验证 typed data 构造。

# EMGVote 形态开关
spec 内部冲突未消除前需要双形态支持：
- `main_spec`（默认）：5 字段含 principal，epoch/nonce 用 uint256
  （MAIN-SPEC §3 + worked example §15）
- `openapi`：4 字段不含 principal，epoch/nonce 用 uint64
  （02-openapi.yaml 的 SignedVoteRequest.signature 描述）

调用方可通过环境变量 `GOVNET_VOTE_TYPED_DATA_VARIANT=openapi` 切换。生
产部署前必须与服务端 `crates/emg-auth/` 实际定义对齐 — 形态错了 vote
会被 ecrecover 拒掉。
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Dict, Optional

from .canonical import eip712_body_hash


# 与 crates/emg-auth/src/eip712.rs 中的 sol! 宏一致。字段顺序、名称、类型
# 都是 typed-data 哈希的一部分，**绝对不能改动**。
EMG_REQUEST_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "EMGRequest": [
        {"name": "principal", "type": "address"},
        {"name": "method", "type": "string"},
        {"name": "path", "type": "string"},
        {"name": "query", "type": "string"},
        {"name": "bodyHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint256"},
        {"name": "timestamp", "type": "uint256"},
    ],
}

# 投票内层 typed data — primaryType 不同，domain 相同。
# 两个变体见模块 docstring 的 "EMGVote 形态开关"。
EMG_VOTE_TYPES_MAIN_SPEC = {
    "EIP712Domain": EMG_REQUEST_TYPES["EIP712Domain"],
    "EMGVote": [
        {"name": "principal", "type": "address"},
        {"name": "epoch", "type": "uint256"},
        {"name": "voteHash", "type": "bytes32"},
        {"name": "predictionHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint256"},
    ],
}

EMG_VOTE_TYPES_OPENAPI = {
    "EIP712Domain": EMG_REQUEST_TYPES["EIP712Domain"],
    "EMGVote": [
        {"name": "epoch", "type": "uint64"},
        {"name": "voteHash", "type": "bytes32"},
        {"name": "predictionHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint64"},
    ],
}


def _vote_variant() -> str:
    return os.environ.get("GOVNET_VOTE_TYPED_DATA_VARIANT", "main_spec").lower()


def _vote_types() -> Dict:
    if _vote_variant() == "openapi":
        return EMG_VOTE_TYPES_OPENAPI
    return EMG_VOTE_TYPES_MAIN_SPEC


# 旧名兼容 — 早期代码 import 这个，后续改成调 `_vote_types()`。
EMG_VOTE_TYPES = EMG_VOTE_TYPES_MAIN_SPEC


def _domain(auth_info: Dict) -> Dict:
    """从 /v1/auth/info 的响应里抽出 EIP-712 domain 四元组。

    服务端返回 `eip712_domain: { name, version, chainId, verifyingContract }`，
    但旧版本可能扁平化在顶层。两种形状都接受。
    """
    if "eip712_domain" in auth_info:
        d = auth_info["eip712_domain"]
    else:
        d = auth_info
    return {
        "name": d.get("name", "EMG"),
        "version": d.get("version", "1"),
        "chainId": int(d["chainId"]),
        "verifyingContract": d["verifyingContract"],
    }


def build_emg_request_typed_data(
    *,
    principal: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    nonce: int,
    timestamp: int,
    auth_info: Dict,
) -> Dict:
    """构造 awp-wallet sign-typed-data 期望的 JSON 结构。

    `path` 必须是 POST-strip 路径 — 服务端 axum router 用 `nest("/v1", …)`
    剥去前缀后，认证中间件看到的就是 `/orders` 而不是 `/v1/orders`。
    例外：WS handshake 用 method `WS_HELLO`、path `/v1/ws`（dispatch 那
    边读的是完整 URI 字面量）。
    """
    body_hash_hex = "0x" + eip712_body_hash(body).hex()
    return {
        "domain": _domain(auth_info),
        "primaryType": "EMGRequest",
        "types": EMG_REQUEST_TYPES,
        "message": {
            "principal": principal,
            "method": method.upper(),
            "path": path,
            "query": query,
            "bodyHash": body_hash_hex,
            "nonce": str(int(nonce)),
            "timestamp": str(int(timestamp)),
        },
    }


def build_emg_vote_typed_data(
    *,
    principal: str,
    epoch: int,
    vote_hash: bytes,
    prediction_hash: bytes,
    nonce: int,
    auth_info: Dict,
) -> Dict:
    """构造投票内层 EMGVote typed data。

    投票需要两个签名：外层 EMGRequest 走标准 transport，内层 EMGVote
    绑定 (epoch, voteHash, predictionHash, nonce) 一起放进 POST body。

    形态由 `GOVNET_VOTE_TYPED_DATA_VARIANT` 环境变量决定（见模块 docstring）。
    `principal` 字段在 `openapi` 形态下被静默丢弃。
    """
    types = _vote_types()
    message: Dict[str, str] = {
        "epoch": str(int(epoch)),
        "voteHash": "0x" + vote_hash.hex(),
        "predictionHash": "0x" + prediction_hash.hex(),
        "nonce": str(int(nonce)),
    }
    if "principal" in {f["name"] for f in types["EMGVote"]}:
        message["principal"] = principal
    return {
        "domain": _domain(auth_info),
        "primaryType": "EMGVote",
        "types": types,
        "message": message,
    }


# --- awp-wallet 桥接 ---------------------------------------------------------


class WalletError(RuntimeError):
    """awp-wallet 进程返回非零或输出无法解析。"""


def _run_wallet(args, *, stdin: Optional[str] = None) -> str:
    """统一调用 awp-wallet 的薄封装。环境变量 `AWP_WALLET` 可覆盖二进制路径。"""
    bin_name = os.environ.get("AWP_WALLET", "awp-wallet")
    try:
        proc = subprocess.run(
            [bin_name, *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise WalletError(
            f"awp-wallet not on PATH (set AWP_WALLET or install from "
            f"https://github.com/awp-core/awp-wallet)"
        ) from e
    if proc.returncode != 0:
        raise WalletError(
            f"awp-wallet {' '.join(args)} exited {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def wallet_address() -> str:
    """`awp-wallet receive --json` → 0x-hex 校验和地址。"""
    out = _run_wallet(["receive", "--json"])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise WalletError(f"awp-wallet receive --json returned non-JSON: {out!r}") from e
    return data["address"]


def wallet_sign_typed_data(typed_data: Dict) -> str:
    """`awp-wallet sign-typed-data --data <json>` → 65 字节 0x-hex 签名。"""
    out = _run_wallet(["sign-typed-data", "--data", json.dumps(typed_data)])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise WalletError(
            f"awp-wallet sign-typed-data returned non-JSON: {out!r}"
        ) from e
    sig = data.get("signature")
    if not sig or not sig.startswith("0x"):
        raise WalletError(f"awp-wallet sign-typed-data missing signature in {data}")
    return sig


def sign_emg_request(
    *,
    principal: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    nonce: int,
    timestamp: int,
    auth_info: Dict,
    actor: Optional[str] = None,
) -> Dict[str, str]:
    """端到端：构造 typed data → 调 awp-wallet 签名 → 返回五元组 header。

    `actor` 默认等于 `principal`。当一个 Manager 代签时，调用方应显式
    传入 Manager 的地址 — 服务端会查询 `AWPRegistry.delegates`。
    """
    typed_data = build_emg_request_typed_data(
        principal=principal,
        method=method,
        path=path,
        query=query,
        body=body,
        nonce=nonce,
        timestamp=timestamp,
        auth_info=auth_info,
    )
    signature = wallet_sign_typed_data(typed_data)
    headers = {
        "X-EMG-Principal": principal,
        "X-EMG-Nonce": str(int(nonce)),
        "X-EMG-Timestamp": str(int(timestamp)),
        "X-EMG-Signature": signature,
    }
    if actor is not None and actor.lower() != principal.lower():
        headers["X-EMG-Actor"] = actor
    return headers


def sign_emg_vote(
    *,
    principal: str,
    epoch: int,
    vote_hash: bytes,
    prediction_hash: bytes,
    nonce: int,
    auth_info: Dict,
) -> str:
    """投票内层签名 — 返回 65 字节 0x-hex。POST body 里把它放在 `signature` 字段。"""
    typed_data = build_emg_vote_typed_data(
        principal=principal,
        epoch=epoch,
        vote_hash=vote_hash,
        prediction_hash=prediction_hash,
        nonce=nonce,
        auth_info=auth_info,
    )
    return wallet_sign_typed_data(typed_data)


# --- 本地摘要计算（仅用于已知答案测试，不走私钥） ---------------------------


def compute_eip712_digest(typed_data: Dict) -> bytes:
    """本地复算 EIP-712 摘要 `keccak256("\\x19\\x01" || domainSeparator || hashStruct)`。

    用于 tests/test_sign.py 里把构造出来的 typed data 喂给 `eth_account` 的
    `encode_typed_data` — 如果摘要等于 REFERENCE_DIGEST_HEX，就证明字段顺
    序、类型、编码全部对齐 Rust 参考实现。**不会** 触发签名，所以无私钥访问。

    注意 eth_account 0.13.x 的 `encode_typed_data` 必须用 `full_message=`
    关键字参数；返回的 `SignableMessage` 暴露 `header`（domain separator）
    和 `body`（hashStruct），合并即得 EIP-712 摘要。
    """
    from eth_account.messages import encode_typed_data
    from .canonical import keccak256

    sm = encode_typed_data(full_message=typed_data)
    return keccak256(b"\x19\x01" + sm.header + sm.body)
