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

服务端 EMGVote 实际形态在 2026-05-08 deployment 后变了（见
`docs/SKILL_API_LATEST.md` §2.3）。`docs/openapi.yaml` 还没跟上。
所以本模块同时支持三种形态：

- `latest_2026_05`（默认）：6 字段，含 `principal/market_id/vote_revision/
  vote_hash/prediction_hash/timestamp`，snake_case，对应**当前生产服务器**。
- `main_spec`：5 字段（principal/epoch/voteHash/predictionHash/nonce, uint256），
  对应旧 `01-MAIN-SPEC.md` §3。
- `openapi`：4 字段（无 principal，epoch/nonce uint64，camelCase），对应
  `02-openapi.yaml` 的 SignedVoteRequest.signature 描述（已被 LATEST 覆盖）。

`GOVNET_VOTE_TYPED_DATA_VARIANT` 环境变量切换。生产应当用默认。
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
# 三个变体见模块 docstring 的 "EMGVote 形态开关"。

# 当前生产形态（2026-05-08 deployment 起，见 SKILL_API_LATEST.md §2.3）。
EMG_VOTE_TYPES_LATEST_2026_05 = {
    "EIP712Domain": EMG_REQUEST_TYPES["EIP712Domain"],
    "EMGVote": [
        {"name": "principal", "type": "address"},
        {"name": "market_id", "type": "uint64"},
        {"name": "vote_revision", "type": "uint64"},
        {"name": "vote_hash", "type": "bytes32"},
        {"name": "prediction_hash", "type": "bytes32"},
        {"name": "timestamp", "type": "uint256"},
    ],
}

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
    return os.environ.get("GOVNET_VOTE_TYPED_DATA_VARIANT", "latest_2026_05").lower()


def _vote_types() -> Dict:
    v = _vote_variant()
    if v == "openapi":
        return EMG_VOTE_TYPES_OPENAPI
    if v == "main_spec":
        return EMG_VOTE_TYPES_MAIN_SPEC
    return EMG_VOTE_TYPES_LATEST_2026_05


# 旧名兼容 — 早期代码 import 这个，后续改成调 `_vote_types()`。
EMG_VOTE_TYPES = EMG_VOTE_TYPES_LATEST_2026_05


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
    market_id: int,
    vote_hash: bytes,
    prediction_hash: bytes,
    auth_info: Dict,
    vote_revision: Optional[int] = None,
    timestamp: Optional[int] = None,
    # 旧形态兼容参数
    epoch: Optional[int] = None,
    nonce: Optional[int] = None,
) -> Dict:
    """构造投票内层 EMGVote typed data。

    投票需要两个签名：外层 EMGRequest 走标准 transport，内层 EMGVote 包含
    投票完整性绑定，其签名进 POST body 的 `signature` 字段。

    形态由 `GOVNET_VOTE_TYPED_DATA_VARIANT` 决定（见模块 docstring）：
      - `latest_2026_05`（默认 / 当前生产）：用 market_id + vote_revision +
        timestamp，必传 `vote_revision` + `timestamp`，`epoch`/`nonce` 被忽略。
      - `main_spec`：5 字段，把 market_id 当 epoch、把 vote_revision 当 nonce
        发出；`timestamp` 被忽略；如果调用方只传了 epoch/nonce 也接受。
      - `openapi`：4 字段，同 main_spec 但不带 principal。

    所有形态都接受 `market_id` 作为 epoch/market 的统一名字 — 旧 `epoch=`
    参数仍然可用，作为向后兼容入口。
    """
    types = _vote_types()
    field_names = {f["name"] for f in types["EMGVote"]}

    # 统一映射：epoch / market_id 二选一
    market = market_id if market_id is not None else epoch
    if market is None:
        raise ValueError("must pass market_id (or legacy epoch=)")
    # vote_revision / nonce 二选一
    revision = vote_revision if vote_revision is not None else nonce
    if revision is None:
        raise ValueError("must pass vote_revision (or legacy nonce=)")

    message: Dict[str, str] = {}
    if "principal" in field_names:
        message["principal"] = principal
    if "market_id" in field_names:
        message["market_id"] = str(int(market))
    if "epoch" in field_names:
        message["epoch"] = str(int(market))
    if "vote_revision" in field_names:
        message["vote_revision"] = str(int(revision))
    if "nonce" in field_names:
        message["nonce"] = str(int(revision))
    if "vote_hash" in field_names:
        message["vote_hash"] = "0x" + vote_hash.hex()
    if "voteHash" in field_names:
        message["voteHash"] = "0x" + vote_hash.hex()
    if "prediction_hash" in field_names:
        message["prediction_hash"] = "0x" + prediction_hash.hex()
    if "predictionHash" in field_names:
        message["predictionHash"] = "0x" + prediction_hash.hex()
    if "timestamp" in field_names:
        if timestamp is None:
            raise ValueError("latest_2026_05 EMGVote requires timestamp")
        message["timestamp"] = str(int(timestamp))

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
    """`awp-wallet receive` → 0x-hex 校验和地址。

    多版本兼容：
    - 优先尝试 `--json` 模式（新版 awp-wallet）。返回的 JSON 字段名可能
      是 `address` 或 `eoaAddress`，两个都接。
    - 老版没有 `--json`：回落到无参 `receive`，从 stdout 里 grep 出第一
      个看起来像 0x-prefixed 40-hex 的 token。
    """
    import re

    # 第一次尝试 --json
    try:
        out = _run_wallet(["receive", "--json"])
        try:
            data = json.loads(out)
            addr = data.get("address") or data.get("eoaAddress")
            if addr:
                return addr
        except json.JSONDecodeError:
            pass
    except WalletError:
        pass

    # 回落：plain text 模式，正则抽地址
    out = _run_wallet(["receive"])
    match = re.search(r"0x[0-9a-fA-F]{40}", out)
    if match:
        return match.group(0)
    raise WalletError(
        f"could not parse wallet address from `awp-wallet receive` output: {out!r}"
    )


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
    market_id: int,
    vote_hash: bytes,
    prediction_hash: bytes,
    auth_info: Dict,
    vote_revision: Optional[int] = None,
    timestamp: Optional[int] = None,
    epoch: Optional[int] = None,
    nonce: Optional[int] = None,
) -> str:
    """投票内层签名 — 返回 65 字节 0x-hex。POST body 里放在 `signature` 字段。

    `latest_2026_05` 形态（默认）需要 `vote_revision` + `timestamp`；旧形态
    `main_spec` / `openapi` 把 vote_revision 当作 nonce 使用，`timestamp` 被
    忽略。`epoch` 是 `market_id` 的旧别名，仅做向后兼容入参。
    """
    typed_data = build_emg_vote_typed_data(
        principal=principal,
        market_id=market_id,
        vote_hash=vote_hash,
        prediction_hash=prediction_hash,
        vote_revision=vote_revision,
        timestamp=timestamp,
        epoch=epoch,
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
