"""REST client, error mapping, Decimal/price formatting — shared utilities used by all scripts.

Design goals:
- Public reads (`fetch`) skip signing; private reads/writes (`signed_request`) auto-inject the five-tuple header.
- All errors funnel through `EmgError` — callers only need a single try/except.
- Combined with `nonce.bump_to` + the `auth-info` cache, `NONCE_TOO_LOW` retries happen automatically.
- HTTPS / WSS enforced — prevents a man-in-the-middle from stripping the EMG-SIG headers.
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


# Default endpoints — overridable via environment variables. All URLs must be HTTPS / WSS.
API_BASE = os.environ.get("GOVNET_API_BASE", "https://api.gov.works/v1").rstrip("/")
WS_URL = os.environ.get("GOVNET_WS_URL", "wss://api.gov.works/v1/ws")

# Default network request timeout (seconds). Streaming subscriptions set their own in ws.py.
HTTP_TIMEOUT = float(os.environ.get("GOVNET_HTTP_TIMEOUT", "10"))

# Auth-info cache directory.
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


# --- Exceptions -------------------------------------------------------------


class EmgError(RuntimeError):
    """Unified protocol-layer exception — `code` comes from the server's §9.5.1 codebook."""

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
    """Reject any 30x redirect — prevents signed headers from being relayed to an attacker-controlled target.

    Threat model: urllib's default HTTPRedirectHandler follows 301/302/303/307/308
    and **forwards all headers** (including our X-EMG-* five-tuple signature
    headers). If the production domain is MITM'd or a misconfigured DNS points
    at `attacker.example`, the attacker can return a benign-looking 302 to
    capture our valid signature and replay it against the real server.

    `_enforce_https` only checks the initial URL, not the redirect target.
    Installing this handler guarantees we **never** follow a redirect — if the
    server really wants to move, it should do so via DNS / load balancer /
    client config (`GOVNET_API_BASE`), not via a 30x.
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


# Module-level opener replaces the default — affects every urlopen call inside `_request`.
_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: Optional[float] = None,
) -> Tuple[int, bytes, Dict[str, str]]:
    """Raw HTTP call — does not parse JSON, only translates error codes.

    Uses this module's `_OPENER` (redirects disabled) instead of the global
    `urllib.request.urlopen` opener, to keep redirects from leaking signed
    headers to third-party domains.
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
    """Public read — assemble URL, call `_request`, parse JSON.

    `path` starts with `/` but **does not include the `/v1` prefix** (API_BASE
    already contains `/v1`). For example `fetch("GET", "/auth/info")` →
    URL `https://api.gov.works/v1/auth/info`. `params` is normalized then
    appended as a query string. `body`, if a dict, is JSON-encoded. Returns
    the parsed JSON or None (204).

    Note: `API_BASE` defaults to `https://api.gov.works/v1`; if `GOVNET_API_BASE`
    is overridden to a URL without `/v1` (self-hosted / reverse-proxy scenarios),
    callers must adjust the path accordingly.
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
        # Compact JSON matches signed_request — so if we ever switch a fetch
        # path to use signing, the bodyHash won't drift due to separator differences.
        raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    status, data, _hdrs = _request(method, url, headers=headers, body=raw_body)
    if status == 204 or not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EmgError("MALFORMED_JSON", f"server returned non-JSON: {data[:200]!r}") from e


# --- Auto-pagination --------------------------------------------------------


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
    """Walk every page following the cursor and concatenate each `data[]` into one array.

    `fetch_page(params: dict) -> dict` is a callback supplied by the caller;
    it should return the server's pagination response (containing `data[]` and
    `pagination.{next_cursor, has_more}`). This function does not care whether
    `fetch()` or `signed_request()` produced the page, so public/private
    listings can both reuse it.

    Stop-condition priority (the OpenAPI `Pagination` schema marks `has_more`
    required while `next_cursor` is nullable + not required, so `has_more`
    is the authoritative signal):
        1. `has_more` explicitly false → stop (regardless of whether the cursor is empty)
        2. No usable cursor → stop (even with inconsistent has_more we can't advance)
        3. Otherwise continue

    `max_pages` is a safety cap — when the data set is unusually large or the
    server's cursor logic falls into an infinite loop, we force a stop;
    exceeding it sets `truncated_at_max_pages: true` plus `next_cursor` in the
    returned dict so the caller/agent can decide whether to keep paging.

    Returns `{data: [...merged...], pagination: {...from the last page...}, page_count: N}`,
    preserving any other fields the server returned (besides the merged data).
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
        # has_more explicitly false → server is telling us definitively there's nothing left
        if has_more is False:
            break
        # No usable cursor → even if has_more is None / true, we can't advance
        if not last_cursor:
            break
        params = dict(params)
        params["cursor"] = last_cursor
    out: Dict[str, Any] = {data_key: aggregated, "page_count": pages}
    # On truncation, expose the relay cursor to the caller
    if pages == max_pages and last_cursor:
        out["truncated_at_max_pages"] = True
        out["next_cursor"] = last_cursor
    if pagination_key in last_resp:
        out[pagination_key] = last_resp[pagination_key]
    return out


# --- auth info cache ---------------------------------------------------------


def get_auth_info(*, force_refresh: bool = False) -> Dict[str, Any]:
    """`GET /v1/auth/info` and cache to `~/.govnet/auth-info.json`.

    Force-refresh scenarios: `AUTH_SIGNATURE_INVALID` (the server may have
    rotated verifyingContract) or `AUTH_NONCE_TOO_LOW` (also good for
    confirming chainId hasn't drifted).
    """
    cache = _auth_info_path()
    if not force_refresh and cache.exists():
        try:
            return json.loads(cache.read_text("utf-8"))
        except json.JSONDecodeError:
            pass  # fall through and re-fetch
    info = fetch("GET", "/auth/info")
    cache.write_text(json.dumps(info), "utf-8")
    return info


# --- Server clock probe -----------------------------------------------------


def fetch_server_time() -> int:
    """`GET /v1/auth/time` — returns only the server's current Unix seconds. Lightweight clock probe.

    Use cases:
    - On `AUTH_TIMESTAMP_OUT_OF_WINDOW`, recalibrate locally and retry after re-signing.
    - At offline-script startup, probe drift; > 30s should prompt the user to NTP-sync.

    No auth, no caching (always fresh). Failures raise `EmgError`.
    """
    resp = fetch("GET", "/auth/time")
    return int(resp["server_time_unix"])


# --- Signed request (auto nonce + retry) -----------------------------------


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
    """Sign → send → parse — the unified entry point for private reads/writes.

    - `sign_path`: the value written into the EIP-712 envelope's `path` field
      (POST-strip form, the path the server's axum router sees after
      `nest("/v1", …)` strips the prefix).
    - `full_path`: the path appended to `API_BASE`, **without** the `/v1`
      prefix (API_BASE already contains `/v1`). In most cases
      `full_path == sign_path`.
    - `query_params`: dict; normalized via `build_query` and appended to the
      URL, while also serving as the `query` field in signing material.
    - `body`: dict → JSON. `bytes` is forwarded verbatim. `None` means empty body.
    - `principal`: defaults to calling `awp-wallet receive`; `actor` defaults
      to the same value as principal.
    - `idempotency_key`: writes should generate one UUIDv7 per logical action;
      reuse the same key on retry (the server caches the response for 24h).
    - `auto_retry_nonce`: on `NONCE_TOO_LOW`, refresh auth-info, bump the
      local floor, and retry once.

    Returns the parsed response JSON.
    """
    from . import nonce as nonce_mod  # deferred import to avoid circular dependency

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

    # `_clock_offset` is overwritten on the AUTH_TIMESTAMP_OUT_OF_WINDOW retry
    # path: take the (server now − local now) delta so the retry signature's
    # timestamp falls inside the window.
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
            # Refreshing auth-info also gets us the server-stored nonce (if
            # the server returns it via /v1/auth/info); worst case, retry
            # with floor + 1.
            fresh = get_auth_info(force_refresh=True)
            stored = int(
                err.body.get("details", {}).get("stored")
                or fresh.get("nonce", 0)
                or 0
            )
            new = nonce_mod.bump_to(principal, max(stored, nonce))
            return _attempt(new)
        if err.code == "AUTH_TIMESTAMP_OUT_OF_WINDOW":
            # Client clock skew: pull the server's now from /v1/auth/time,
            # write the (server − local) delta into clock_offset so _attempt's
            # next signature has a timestamp inside the server's ±30s window.
            # If the offset still > 30s (rare, suggests NTP is stuck), the
            # retry will fail and EmgError detail will explicitly say so.
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
            # Server changed chainId or verifyingContract. Force refresh + retry.
            nonlocal_auth = get_auth_info(force_refresh=True)
            # _attempt's closure-captured auth_info refers to the outer object;
            # we need to rebuild it. Simple path: clear cache + raise EmgError
            # so the upper layer re-invokes signed_request. But that violates
            # the "auto retry once" contract — so instead we rewrite the
            # closure's reference in place:
            new_nonce = nonce_mod.next_nonce(principal)
            # auth_info has already been updated by the get_auth_info cache layer;
            # the next _attempt reads the global cache (the closure holds the
            # same object reference, which we've now overwritten in place).
            auth_info.clear()
            auth_info.update(nonlocal_auth)
            return _attempt(new_nonce)
        if err.status == 429:
            # Server rate-limit; honor Retry-After header (seconds), capped at 60s as a safety net
            wait = _parse_retry_after(err.headers.get("Retry-After"))
            time.sleep(wait)
            new_nonce = nonce_mod.next_nonce(principal)
            return _attempt(new_nonce)
        # 5xx + nonce-burned means the server already consumed the nonce — bump the floor before letting the upper layer retry
        if err.headers.get("X-EMG-Nonce-Burned", "").lower() == "true":
            nonce_mod.bump_to(principal, nonce)
        raise


def _parse_retry_after(header: Optional[str], default: float = 1.0, cap: float = 60.0) -> float:
    """Parse `Retry-After` — supports both delta-seconds (numeric) and HTTP-date.

    Bad input falls back to default; any value above `cap` seconds is forcibly
    truncated, preventing a misconfigured server from parking the client into
    the next century. One retry is enough; anything more bubbles up.
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
        # either returns a datetime or raises (TypeError/ValueError); it does not return None
        from email.utils import parsedate_to_datetime
        target = parsedate_to_datetime(header)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, 0.0), cap)
    except (TypeError, ValueError):
        return default


# --- Display helpers --------------------------------------------------------


_FOUR = Decimal("0.0001")
_EIGHT = Decimal("0.00000001")


def fmt_dec(value, places: int = 4) -> str:
    """Quantize a wire-format string-decimal to `places` digits for display.

    Uses `ROUND_HALF_EVEN` (banker's rounding) — matches the server's
    rust_decimal default settlement behavior, avoiding a ±1 ULP visual delta
    between the display layer and the settlement layer.
    """
    if value is None:
        return "—"
    d = Decimal(str(value))
    quant = Decimal(10) ** -places
    return str(d.quantize(quant, rounding=ROUND_HALF_EVEN))


def fmt_price(value) -> str:
    """Prices are uniformly displayed with 4 decimal places."""
    return fmt_dec(value, 4)


def fmt_amount(value) -> str:
    """Amounts use 8 decimal places, matching the server's quantity step."""
    return fmt_dec(value, 8)


# --- Phase normalization ----------------------------------------------------


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
    """Collapse the two styles the server might emit (snake_case / CamelCase) to a single form."""
    if not s:
        return ""
    return _PHASE_ALIASES.get(s.lower(), s.lower())


# --- Exit codes -------------------------------------------------------------


def emit_error(err: EmgError) -> int:
    """Serialize `EmgError` as a single-line JSON to stdout and return a non-zero exit code.

    Every script's top-level uses:
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
    # 401/409 → 2; 429 → 3; 5xx → 4; otherwise → 1
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
    """Try `/markets/{id}` first (includes worknets[]); on 404 fall back to `/epochs/{id}`.

    `/v1/markets/{id}` has been live in production since the 2026-05-08
    deployment (see `docs/SKILL_API_LATEST.md` §1.1); the fallback path is
    kept for self-hosted deployments or environments rolled back to an older
    version. The fallback EpochInfo does not include worknets[], so the
    worknet name shown in scripts degrades to `id N` — callers should catch
    this case and call `/worknets` separately as needed.
    """
    try:
        return fetch("GET", f"/markets/{market_id}")
    except EmgError as e:
        if e.status != 404:
            raise
    return fetch("GET", f"/epochs/{market_id}")


def confirm(prompt: str, *, yes: bool) -> bool:
    """Unified confirm-before-irreversible hook.

    - When `yes=True`, pass through unconditionally (CLI flag `--yes` or
      non-interactive scenarios).
    - Otherwise write the prompt to stderr and read one line from stdin;
      only `y` / `Y` is accepted.
    - When stdin is not a tty and `--yes` was not supplied, return False —
      the caller should abort.
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
