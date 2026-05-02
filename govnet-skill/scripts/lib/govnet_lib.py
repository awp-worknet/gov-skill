"""REST 客户端、错误映射、Decimal/价格格式化 — 所有脚本共享的工具层。

设计目标：
- 公开读 (`fetch`) 不签名，私有读/写 (`signed_request`) 自动注入五元组 header。
- 所有错误统一抛 `EmgError` — 调用方只需要 try/except 一次。
- 配合 `nonce.bump_to` + `auth-info` 缓存自动处理 `NONCE_TOO_LOW` 重试。
- 强制 HTTPS / WSS — 防止中间人剥掉 EMG-SIG 头。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .canonical import build_query
from .sign import sign_emg_request, wallet_address


# 默认 endpoint — 可通过环境变量覆盖。所有 URL 必须是 HTTPS / WSS。
API_BASE = os.environ.get("GOVNET_API_BASE", "https://api.gov.works").rstrip("/")
WS_URL = os.environ.get("GOVNET_WS_URL", "wss://api.gov.works/v1/ws")

# 网络请求默认超时（秒）。流式订阅在 ws.py 里另行设置。
HTTP_TIMEOUT = float(os.environ.get("GOVNET_HTTP_TIMEOUT", "10"))

# Auth-info 缓存目录。
def _auth_info_path() -> Path:
    base = os.environ.get("GOVNET_AUTH_DIR")
    p = Path(base).expanduser() if base else Path.home() / ".govnet"
    p.mkdir(parents=True, exist_ok=True)
    return p / "auth-info.json"


def _enforce_https(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https",):
        raise EmgError(
            "INSECURE_TRANSPORT",
            f"{url}: skill refuses plaintext HTTP — "
            "set GOVNET_API_BASE to an https:// URL",
        )


# --- 异常 -------------------------------------------------------------------


class EmgError(RuntimeError):
    """统一的协议层异常 — `code` 来自服务端 §9.5.1 codebook。"""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        title: str = "",
        status: int = 0,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.title = title or code
        self.status = status
        self.body = body or {}
        self.headers = headers or {}
        super().__init__(f"{code} ({status}): {detail or title}")


def _problem_to_error(status: int, raw: bytes, headers: Dict[str, str]) -> EmgError:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {"detail": raw[:200].decode("utf-8", "replace")}
    code = body.get("code") or body.get("error") or f"HTTP_{status}"
    return EmgError(
        code,
        body.get("detail", ""),
        title=body.get("title", ""),
        status=status,
        body=body,
        headers=headers,
    )


# --- HTTP --------------------------------------------------------------------


def _request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: Optional[float] = None,
) -> Tuple[int, bytes, Dict[str, str]]:
    """裸 HTTP 调用 — 不解析 JSON，只做错误码翻译。"""
    _enforce_https(url)
    req = urllib.request.Request(url, data=body, method=method.upper())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            data = resp.read()
            hdrs = {k: v for k, v in resp.headers.items()}
            return resp.status, data, hdrs
    except urllib.error.HTTPError as e:
        data = e.read()
        hdrs = {k: v for k, v in e.headers.items()}
        raise _problem_to_error(e.code, data, hdrs) from None
    except urllib.error.URLError as e:
        raise EmgError("NETWORK_ERROR", str(e.reason)) from e


def fetch(method: str, path: str, *, params: Optional[Dict] = None, body: Optional[Any] = None) -> Any:
    """公开读 — 拼 URL + 调用 `_request` + 解析 JSON。

    `path` 必须以 `/` 开头并包含 `/v1` 前缀（外发的真实 URL）。`params`
    会经规范化后 append 为 query string。`body` 若是 dict 会 JSON 编码。
    返回解析后的 JSON 或 None（204）。
    """
    if not path.startswith("/"):
        path = "/" + path
    qs = build_query(params or {})
    url = f"{API_BASE}{path}"
    if qs:
        url += "?" + qs

    raw_body: Optional[bytes] = None
    headers: Dict[str, str] = {"Accept": "application/json", "User-Agent": "govnet-skill/0.1"}
    if body is not None:
        raw_body = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    status, data, _hdrs = _request(method, url, headers=headers, body=raw_body)
    if status == 204 or not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EmgError("MALFORMED_JSON", f"server returned non-JSON: {data[:200]!r}") from e


# --- auth info 缓存 ----------------------------------------------------------


def get_auth_info(*, force_refresh: bool = False) -> Dict[str, Any]:
    """`GET /v1/auth/info` 并缓存到 `~/.govnet/auth-info.json`。

    强制刷新场景：`AUTH_SIGNATURE_INVALID`（可能服务端换了 verifyingContract）
    或 `AUTH_NONCE_TOO_LOW`（顺便核对 chainId 没飘）。
    """
    cache = _auth_info_path()
    if not force_refresh and cache.exists():
        try:
            return json.loads(cache.read_text("utf-8"))
        except json.JSONDecodeError:
            pass  # fall through 重新获取
    info = fetch("GET", "/v1/auth/info")
    cache.write_text(json.dumps(info), "utf-8")
    return info


# --- 签名请求（自动 nonce + 重试） -------------------------------------------


def signed_request(
    method: str,
    sign_path: str,
    full_path: str,
    *,
    query_params: Optional[Dict] = None,
    body: Optional[Any] = None,
    principal: Optional[str] = None,
    actor: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    auto_retry_nonce: bool = True,
) -> Any:
    """签名 → 发送 → 解析 — 私有读/写的统一入口。

    - `sign_path`：写进 EIP-712 信封 `path` 字段的值（POST-strip 形式）。
      多数情况就是 `full_path` 去掉 `/v1` 前缀。
    - `full_path`：实际拼到 `API_BASE` 后面的路径。
    - `query_params`：dict；会被 `build_query` 规范化后 append 到 URL，
      同时也作为签名材料里的 `query` 字段。
    - `body`：dict → JSON。`bytes` 直接透传。`None` 表示空 body。
    - `principal`：缺省调用 `awp-wallet receive` 拿；`actor` 默认与
      principal 一致。
    - `idempotency_key`：写操作建议每个逻辑动作生成一个 UUIDv7；重试
      时复用同一个 key（服务端缓存 24h 响应）。
    - `auto_retry_nonce`：碰到 `NONCE_TOO_LOW` 时刷新 auth-info、bump
      本地下界、重试一次。

    返回解析后的响应 JSON。
    """
    from . import nonce as nonce_mod  # 延迟导入避免循环

    if principal is None:
        principal = wallet_address()

    auth_info = get_auth_info()
    qs = build_query(query_params or {})

    if isinstance(body, (bytes, bytearray)):
        raw_body = bytes(body)
    elif body is None:
        raw_body = b""
    else:
        raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")

    def _attempt(n: int) -> Any:
        ts = int(time.time())
        headers = sign_emg_request(
            principal=principal,
            method=method,
            path=sign_path,
            query=qs,
            body=raw_body,
            nonce=n,
            timestamp=ts,
            auth_info=auth_info,
            actor=actor,
        )
        if raw_body:
            headers["Content-Type"] = "application/json"
        headers.setdefault("Accept", "application/json")
        headers["User-Agent"] = "govnet-skill/0.1"
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        url = f"{API_BASE}{full_path}"
        if qs:
            url += "?" + qs
        status, data, resp_headers = _request(
            method, url, headers=headers, body=raw_body or None
        )
        if status == 204 or not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise EmgError(
                "MALFORMED_JSON",
                f"server returned non-JSON: {data[:200]!r}",
            ) from e

    nonce = nonce_mod.next_nonce(principal)
    try:
        return _attempt(nonce)
    except EmgError as err:
        if not auto_retry_nonce:
            raise
        if err.code in ("AUTH_NONCE_TOO_LOW", "NONCE_TOO_LOW", "NONCE_CONFLICT"):
            # 刷新 auth-info 顺便能拿到 server stored nonce（如果服务端在
            # /v1/auth/info 里返回了的话）；最坏情况 +1 已知 floor 重试。
            fresh = get_auth_info(force_refresh=True)
            stored = int(
                err.body.get("details", {}).get("stored")
                or fresh.get("nonce", 0)
                or 0
            )
            new = nonce_mod.bump_to(principal, max(stored, nonce))
            return _attempt(new)
        # 5xx + nonce-burned 表示服务端已消费 nonce — 推一下下界再交给上层重试
        if err.headers.get("X-EMG-Nonce-Burned", "").lower() == "true":
            nonce_mod.bump_to(principal, nonce)
        raise


# --- 显示辅助 ---------------------------------------------------------------


_FOUR = Decimal("0.0001")
_EIGHT = Decimal("0.00000001")


def fmt_dec(value, places: int = 4) -> str:
    """把 wire 上的 string-decimal 量化到 `places` 位用于展示。

    用 `ROUND_HALF_EVEN`（银行家舍入）— 与服务端结算的 rust_decimal 默
    认行为一致，避免显示层和结算层产生 ±1 ULP 的视觉差异。
    """
    if value is None:
        return "—"
    d = Decimal(str(value))
    quant = Decimal(10) ** -places
    return str(d.quantize(quant, rounding=ROUND_HALF_EVEN))


def fmt_price(value) -> str:
    """价格统一展示 4 位小数。"""
    return fmt_dec(value, 4)


def fmt_amount(value) -> str:
    """数量按 8 位小数展示，与服务端 quantity step 一致。"""
    return fmt_dec(value, 8)


# --- 阶段归一化 -------------------------------------------------------------


_PHASE_ALIASES = {
    "pending": "pending",
    "voting_and_trading": "voting_and_trading",
    "votingandtrading": "voting_and_trading",
    "trading_only": "trading_only",
    "tradingonly": "trading_only",
    "settlement": "settling",
    "settling": "settling",
    "weekly_report": "completed",
    "reporting": "completed",
    "completed": "completed",
}


def normalize_phase(s: str) -> str:
    """把服务端可能出现的两种风格（snake_case / CamelCase）压成单一形式。"""
    if not s:
        return ""
    return _PHASE_ALIASES.get(s.lower(), s.lower())


# --- 退出码 -----------------------------------------------------------------


def emit_error(err: EmgError) -> int:
    """把 `EmgError` 序列化成单行 JSON 写到 stdout，返回非零退出码。

    每个脚本顶层都用：
        try:
            ...
        except EmgError as e:
            sys.exit(emit_error(e))
    """
    payload = {
        "error": err.code,
        "title": err.title,
        "detail": err.detail,
        "status": err.status,
    }
    print(json.dumps(payload), flush=True)
    # 401/409 → 2；429 → 3；5xx → 4；其它 → 1
    if err.status == 401:
        return 2
    if err.status == 429:
        return 3
    if 500 <= err.status < 600:
        return 4
    if err.status == 409:
        return 5
    return 1


def fetch_market(market_id: int) -> dict:
    """优先 `/v1/markets/{id}`（含 worknets[]），404 时回落 `/v1/epochs/{id}`。

    OpenAPI 没显式定义 `/v1/markets/{id}`，但 MAIN-SPEC §4.1 + §15 要求；
    实际服务器是否暴露要看部署。fallback 后的 EpochInfo 没有 worknets[]，
    脚本里展示的 worknet 名字会退化到 `id N` —— 调用方应 catch 这种情况
    并按需另调 `/v1/worknets`。
    """
    try:
        return fetch("GET", f"/v1/markets/{market_id}")
    except EmgError as e:
        if e.status != 404:
            raise
    return fetch("GET", f"/v1/epochs/{market_id}")


def confirm(prompt: str, *, yes: bool) -> bool:
    """统一的 confirm-before-irreversible 钩子。

    - `yes=True` 时直接放行（CLI 标志 `--yes` 或非交互场景）。
    - 否则把 prompt 写到 stderr，从 stdin 读取一行；只接受 `y` / `Y`。
    - stdin 不是 tty 且没传 `--yes` 时返回 False — 调用方应中止。
    """
    if yes:
        return True
    if not sys.stdin.isatty():
        sys.stderr.write(prompt)
        sys.stderr.write("\n[ABORT] non-interactive shell and --yes not supplied\n")
        return False
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans == "y"
