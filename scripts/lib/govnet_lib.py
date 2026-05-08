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
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .canonical import build_query
from .sign import sign_emg_request, wallet_address


# 默认 endpoint — 可通过环境变量覆盖。所有 URL 必须是 HTTPS / WSS。
API_BASE = os.environ.get("GOVNET_API_BASE", "https://api.gov.works/v1").rstrip("/")
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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝任何 30x 重定向 —— 防签名 header 被代发到攻击者控制的目标。

    威胁模型：urllib 默认的 HTTPRedirectHandler 会跟随 301/302/303/307/308，
    并且**把全部 header 一起转发**（包括我们的 X-EMG-* 五元组签名头）。
    如果生产域名被中间人或 misconfig DNS 指向 `attacker.example`，攻击者
    返回一个看似无害的 302 就能拿到我们的合法签名，replay 到真服务器。

    `_enforce_https` 只查初始 URL，不查 redirect target。装这个 handler
    保证我们 **永不** 跟随重定向 —— 服务端真要换地址，应该走 DNS / load
    balancer / 客户端配置 (`GOVNET_API_BASE`)，不应该靠 30x。
    """
    def http_error_301(self, req, fp, code, msg, headers):
        raise EmgError(
            "INSECURE_REDIRECT",
            f"server returned {code} redirect to {headers.get('Location', '?')}; "
            "skill refuses to forward signed headers across redirects",
            status=code,
        )
    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


# 用模块级 opener 替换默认 —— 影响 `_request` 里所有 urlopen 调用
_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: Optional[float] = None,
) -> Tuple[int, bytes, Dict[str, str]]:
    """裸 HTTP 调用 — 不解析 JSON，只做错误码翻译。

    用本模块的 `_OPENER`（禁用了 redirect）而不是 `urllib.request.urlopen`
    全局 opener，避免 redirect 携带签名 header 泄露到第三方域名。
    """
    _enforce_https(url)
    req = urllib.request.Request(url, data=body, method=method.upper())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout or HTTP_TIMEOUT) as resp:
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
        # 紧凑 JSON 与 signed_request 一致 —— 让来日万一从 fetch 改走签名路径
        # 时不会因为 separator 不同而 bodyHash 漂移
        raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    status, data, _hdrs = _request(method, url, headers=headers, body=raw_body)
    if status == 204 or not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EmgError("MALFORMED_JSON", f"server returned non-JSON: {data[:200]!r}") from e


# --- 自动翻页 ---------------------------------------------------------------


def paginate_all(
    fetch_page,
    *,
    initial_params: Optional[Dict] = None,
    max_pages: int = 100,
    data_key: str = "data",
    pagination_key: str = "pagination",
    cursor_key: str = "next_cursor",
    has_more_key: str = "has_more",
) -> Dict[str, Any]:
    """跟着 cursor 翻完所有页，把所有 `data[]` 拼回一个数组。

    `fetch_page(params: dict) -> dict` 是调用方提供的回调，应返回服务端的
    分页响应（含 `data[]` 和 `pagination.{next_cursor, has_more}`）。本函数
    不关心是 `fetch()` 还是 `signed_request()` 拉的，公开/私有列表都能复用。

    停止条件优先级（OpenAPI `Pagination` schema 把 `has_more` 标 required，
    `next_cursor` 是 nullable + 非 required，所以 `has_more` 才是权威信号）：
        1. `has_more` 显式 false → 停（无视 cursor 是否为空）
        2. 没有可用 cursor → 停（即便 has_more 不一致也无法前进）
        3. 否则继续

    `max_pages` 是防呆 —— 数据量异常大或服务端 cursor 出 bug 死循环时
    强制截断；超过会在返回的 dict 里加 `truncated_at_max_pages: true` +
    `next_cursor`，调用方/agent 自己判断要不要接力翻更多。

    返回 `{data: [...合并...], pagination: {...最后一页的...}, page_count: N}`，
    保留服务端原本响应的其它字段（除了 data 是合并的）。
    """
    params = dict(initial_params or {})
    aggregated: list = []
    last_resp: Dict[str, Any] = {}
    last_cursor: Optional[str] = None
    pages = 0
    while pages < max_pages:
        last_resp = fetch_page(params)
        pages += 1
        items = last_resp.get(data_key, []) or []
        aggregated.extend(items)
        pagination = last_resp.get(pagination_key) or {}
        last_cursor = pagination.get(cursor_key)
        has_more = pagination.get(has_more_key)
        # has_more 显式 false → 服务端确定地告诉我们没了
        if has_more is False:
            break
        # 没 cursor 可用 → 即便 has_more 是 None / true，我们也无法继续
        if not last_cursor:
            break
        params = dict(params)
        params["cursor"] = last_cursor
    out: Dict[str, Any] = {data_key: aggregated, "page_count": pages}
    # 截断时把接力 cursor 暴露给调用方
    if pages == max_pages and last_cursor:
        out["truncated_at_max_pages"] = True
        out["next_cursor"] = last_cursor
    if pagination_key in last_resp:
        out[pagination_key] = last_resp[pagination_key]
    return out


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


# --- 服务端时钟探测 ---------------------------------------------------------


def fetch_server_time() -> int:
    """`GET /v1/auth/time` —— 仅返回 server 当前 Unix 秒。轻量 clock probe。

    用途：
    - `AUTH_TIMESTAMP_OUT_OF_WINDOW` 错误时校准本地，重签后重试。
    - 离线脚本启动时主动测漂移，超 30s 直接提示用户 NTP sync。

    无 auth，无缓存（每次 fresh）。失败 raise `EmgError`。
    """
    resp = fetch("GET", "/v1/auth/time")
    return int(resp["server_time_unix"])


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

    # `_clock_offset` 在 AUTH_TIMESTAMP_OUT_OF_WINDOW 重试路径里被覆盖：
    # 拿服务端 now − 本地 now 的差值，让重试时签名的 timestamp 落进窗口。
    clock_offset = {"delta": 0}

    def _attempt(n: int) -> Any:
        ts = int(time.time()) + clock_offset["delta"]
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
        if err.code == "AUTH_TIMESTAMP_OUT_OF_WINDOW":
            # 客户端时钟偏移：从 /v1/auth/time 取服务端 now，把 (server − local)
            # 差值压进 clock_offset，让 _attempt 下一次签名出来的 timestamp
            # 落在服务端 ±30s 窗口内。如果偏移仍 > 30s（很罕见，意味着 NTP
            # 可能 stuck）retry 还会失败；那时 EmgError detail 会显式提示。
            try:
                srv_time = fetch_server_time()
            except EmgError:
                raise err
            clock_offset["delta"] = srv_time - int(time.time())
            new_nonce = nonce_mod.next_nonce(principal)
            try:
                return _attempt(new_nonce)
            except EmgError as retry_err:
                if retry_err.code == "AUTH_TIMESTAMP_OUT_OF_WINDOW":
                    raise EmgError(
                        retry_err.code,
                        f"clock skew {clock_offset['delta']}s vs server even after correction; "
                        "your NTP daemon may be stuck or the server's clock is itself drifting",
                        title=retry_err.title,
                        status=retry_err.status,
                        body=retry_err.body,
                        headers=retry_err.headers,
                    ) from retry_err
                raise
        if err.code == "AUTH_EIP712_DOMAIN_MISMATCH":
            # 服务端 chainId 或 verifyingContract 变了。强制 refresh + retry。
            nonlocal_auth = get_auth_info(force_refresh=True)
            # _attempt 闭包里的 auth_info 是引用上层的，需要重 build。
            # 简化路径：清缓存 + raise EmgError 让上层重新调 signed_request。
            # 但这违反 "auto retry once" 约定 —— 这里就在闭包内重写一次：
            new_nonce = nonce_mod.next_nonce(principal)
            # auth_info 已通过 get_auth_info 缓存层更新；下次 _attempt 读
            # 全局缓存即可（_attempt 闭包持的是同一对象引用，已被覆盖）
            auth_info.clear()
            auth_info.update(nonlocal_auth)
            return _attempt(new_nonce)
        if err.status == 429:
            # 服务端 rate-limit；honor Retry-After header（秒），上限 60s 防呆
            wait = _parse_retry_after(err.headers.get("Retry-After"))
            time.sleep(wait)
            new_nonce = nonce_mod.next_nonce(principal)
            return _attempt(new_nonce)
        # 5xx + nonce-burned 表示服务端已消费 nonce — 推一下下界再交给上层重试
        if err.headers.get("X-EMG-Nonce-Burned", "").lower() == "true":
            nonce_mod.bump_to(principal, nonce)
        raise


def _parse_retry_after(header: Optional[str], default: float = 1.0, cap: float = 60.0) -> float:
    """解析 `Retry-After` —— 既支持 delta-seconds（数字），也支持 HTTP-date。

    异常输入回 default；任何上限超过 `cap` 秒强制截断，防止 misconfigured
    服务端把客户端挂到下个世纪。一次重试就好，多了打回上层。
    """
    if not header:
        return default
    try:
        secs = float(header.strip())
        return min(max(secs, 0.0), cap)
    except ValueError:
        pass
    try:
        # HTTP-date format: "Wed, 21 Oct 2026 07:28:00 GMT" — `parsedate_to_datetime`
        # 要么返回 datetime，要么 raise (TypeError/ValueError)；不会返回 None
        from email.utils import parsedate_to_datetime
        target = parsedate_to_datetime(header)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, 0.0), cap)
    except (TypeError, ValueError):
        return default


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
