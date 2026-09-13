from __future__ import annotations

import gzip
import json
from decimal import Decimal, InvalidOperation
from threading import Lock
import zlib
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from .identity import BrowserIdentity, default_identity
from .validation import ValidationError, normalize_address, normalize_market_id


class FairgroundAPIError(RuntimeError):
    """A sanitized error returned by the Fairground read API."""


INCENTIVES_URL = "https://incentives.fairground.fi"


def current_season_id_from_payload(payload: dict[str, Any]) -> int:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FairgroundAPIError("Fairground incentives returned no current season")
    current = data.get("current")
    if not isinstance(current, dict):
        raise FairgroundAPIError("Fairground incentives returned no current season")
    try:
        season_id = int(current.get("id"))
    except (TypeError, ValueError) as exc:
        raise FairgroundAPIError("Fairground incentives returned an invalid season") from exc
    if season_id <= 0:
        raise FairgroundAPIError("Fairground incentives returned an invalid season")
    return season_id


def season_points_from_payload(
    payload: dict[str, Any], *, owner: str | None = None
) -> int:
    data = payload.get("data")
    if isinstance(data, list):
        if not data or not isinstance(data[0], dict):
            return 0
        data = data[0]
    if not isinstance(data, dict):
        return 0
    if owner:
        raw_owner = data.get("owner")
        if not isinstance(raw_owner, str):
            return 0
        try:
            if normalize_address(raw_owner) != normalize_address(owner):
                return 0
        except ValidationError:
            return 0
    raw = data.get("total_score")
    if raw in (None, ""):
        return 0
    try:
        number = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return 0
    if not number.is_finite() or number < 0:
        return 0
    return int(number)


class _RawRequest(Request):
    """Keep header names as given.

    urllib.Request.add_header() runs key.capitalize(), so User-Agent becomes
    User-agent and sec-ch-ua becomes Sec-ch-ua. Chrome does not send that.
    has_header/get_header stay case-insensitive so urllib's Content-type /
    Content-length checks still work.
    """

    def add_header(self, key, val):  # noqa: ANN001
        name = self._existing_key(key) or self._canonical_name(key)
        self.headers[name] = val

    def add_unredirected_header(self, key, val):  # noqa: ANN001
        name = self._existing_key(key) or self._canonical_name(key)
        self.unredirected_hdrs[name] = val

    def has_header(self, header_name):  # noqa: ANN001
        target = header_name.lower()
        return any(key.lower() == target for key in self.headers) or any(
            key.lower() == target for key in self.unredirected_hdrs
        )

    def get_header(self, header_name, default=None):  # noqa: ANN001
        target = header_name.lower()
        for store in (self.unredirected_hdrs, self.headers):
            for key, value in store.items():
                if key.lower() == target:
                    return value
        return default

    def remove_header(self, header_name):  # noqa: ANN001
        target = header_name.lower()
        for store in (self.headers, self.unredirected_hdrs):
            for key in [item for item in store if item.lower() == target]:
                del store[key]

    def _existing_key(self, header_name: str) -> str:
        target = header_name.lower()
        for store in (self.headers, self.unredirected_hdrs):
            for key in store:
                if key.lower() == target:
                    return key
        return ""

    @staticmethod
    def _canonical_name(key: str) -> str:
        lowered = key.lower()
        if lowered == "content-length":
            return "Content-Length"
        if lowered == "host":
            return "Host"
        if lowered == "connection":
            return "Connection"
        if lowered == "proxy-authorization":
            return "Proxy-Authorization"
        return key


def _pop_header(headers: dict[str, str], name: str) -> str:
    target = name.lower()
    for key in list(headers):
        if key.lower() == target:
            return headers.pop(key)
    return ""


def split_proxy_tunnel_headers(headers: dict[str, str]) -> dict[str, str]:
    """Move proxy auth onto CONNECT. urllib names it Proxy-authorization."""
    tunnel: dict[str, str] = {}
    value = _pop_header(headers, "Proxy-Authorization")
    if value:
        tunnel["Proxy-Authorization"] = value
    return tunnel


def decode_http_body(body: bytes, content_encoding: str = "") -> bytes:
    """Decode gzip/deflate only when the payload actually is compressed.

    Some proxies leave Content-Encoding: gzip on already-decoded JSON.
    gzip.decompress then raises OSError, which urllib maps to a fake
    'API is unavailable' and kills every account before waitlist.
    """
    if not body:
        return body
    stripped = body.lstrip()
    if stripped[:1] in (b"{", b"["):
        return body
    if body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    encoding = (content_encoding or "").lower()
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except zlib.error:
            try:
                return zlib.decompress(body, -zlib.MAX_WBITS)
            except zlib.error:
                return body
    return body


Transport = Callable[[Request, float], tuple[int, bytes]]


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward a request to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _do_open_preserve_case(handler, http_class, req, **http_conn_args):  # noqa: ANN001
    """Same as urllib AbstractHTTPHandler.do_open, without name.title()."""
    host = req.host
    if not host:
        raise URLError("no host given")
    connection = http_class(host, timeout=req.timeout, **http_conn_args)
    connection.set_debuglevel(handler._debuglevel)
    headers = dict(req.unredirected_hdrs)
    headers.update({key: value for key, value in req.headers.items() if key not in headers})
    headers["Connection"] = "close"
    if req._tunnel_host:
        connection.set_tunnel(req._tunnel_host, headers=split_proxy_tunnel_headers(headers))
    try:
        try:
            connection.request(
                req.get_method(),
                req.selector,
                req.data,
                headers,
                encode_chunked=req.has_header("Transfer-encoding"),
            )
        except OSError as exc:
            raise URLError(exc) from exc
        response = connection.getresponse()
    except Exception:
        connection.close()
        raise
    if connection.sock:
        connection.sock.close()
        connection.sock = None
    response.url = req.get_full_url()
    response.msg = response.reason
    return response


class _RawHTTPHandler(HTTPHandler):
    def do_open(self, http_class, req, **http_conn_args):  # noqa: ANN001
        return _do_open_preserve_case(self, http_class, req, **http_conn_args)


class _RawHTTPSHandler(HTTPSHandler):
    def do_open(self, http_class, req, **http_conn_args):  # noqa: ANN001
        return _do_open_preserve_case(self, http_class, req, **http_conn_args)


class FairgroundClient:
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024
    _season_id: int | None = None
    _season_lock = Lock()

    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 15,
        *,
        transport: Transport | None = None,
        proxy_url: str | None = None,
        identity: BrowserIdentity | None = None,
        chain_id: int = 421614,
        cookie_header: str = "",
        browser_fetch: Callable[[str, dict[str, str], bytes], tuple[int, bytes]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self.proxy_url = proxy_url
        self.identity = identity or default_identity()
        self.chain_id = str(int(chain_id))
        self.cookie_header = str(cookie_header or "")
        self.browser_fetch = browser_fetch
        # Never honour ambient HTTP(S)_PROXY. Either use an explicit per-wallet
        # proxy URL, or disable proxies entirely for single-account local runs.
        if proxy_url:
            proxy_map = {"http": proxy_url, "https": proxy_url}
        else:
            proxy_map = {}
        self._opener: OpenerDirector = build_opener(
            ProxyHandler(proxy_map),
            _RejectRedirects(),
            _RawHTTPHandler(),
            _RawHTTPSHandler(),
        )

    def _default_transport(self, request: Request, timeout: float) -> tuple[int, bytes]:
        with self._opener.open(request, timeout=timeout) as response:
            body = response.read(self.MAX_RESPONSE_BYTES + 1)
            encoding = ""
            try:
                encoding = str(response.headers.get("Content-Encoding") or "")
            except Exception:
                encoding = ""
            return response.status, decode_http_body(body, encoding)

    def _urllib_call(
        self,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
        method: str,
    ) -> tuple[int, bytes]:
        request = _RawRequest(url, method=method, data=data, headers=headers)
        transport = self._transport or self._default_transport
        try:
            return transport(request, float(self.timeout_seconds))
        except HTTPError as exc:
            body = b""
            try:
                body = exc.read(self.MAX_RESPONSE_BYTES + 1)
            except Exception:
                body = b""
            encoding = ""
            try:
                encoding = str(exc.headers.get("Content-Encoding") or "")
            except Exception:
                encoding = ""
            return int(exc.code), decode_http_body(body, encoding)
        except (URLError, TimeoutError, OSError) as exc:
            raise FairgroundAPIError(
                f"Fairground API is unavailable ({type(exc).__name__})"
            ) from exc

    def _urllib_post(
        self, url: str, headers: dict[str, str], data: bytes
    ) -> tuple[int, bytes]:
        return self._urllib_call(url, headers, data, "POST")

    def _with_chain(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.setdefault("chainId", self.chain_id)
        return body

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        identity_headers = self.identity.headers()
        inserted_type = False
        for key, value in identity_headers.items():
            if key == "Accept" and not inserted_type:
                headers["Content-Type"] = "application/json"
                inserted_type = True
            headers[key] = value
        if not inserted_type:
            headers["Content-Type"] = "application/json"
        headers["Connect-Protocol-Version"] = "1"
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        return headers

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not path.startswith("/") or ".." in path or urlparse(path).netloc:
            raise FairgroundAPIError("Unsafe API path")
        data = json.dumps(self._with_chain(payload), separators=(",", ":")).encode("utf-8")
        url = f"{self.base_url}{path}"
        headers = self._headers()
        status = 0
        body = b""
        if self.browser_fetch is not None:
            try:
                status, body = self.browser_fetch(url, headers, data)
            except FairgroundAPIError:
                raise
            except Exception:
                status, body = self._urllib_post(url, headers, data)
        else:
            status, body = self._urllib_post(url, headers, data)

        if status < 200 or status >= 300:
            raise FairgroundAPIError(f"Fairground API returned HTTP {status}")
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise FairgroundAPIError("Fairground API response exceeded the safety limit")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise FairgroundAPIError("Fairground API returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise FairgroundAPIError("Fairground API returned an unexpected payload")
        return parsed

    def _get_headers(self) -> dict[str, str]:
        headers = dict(self.identity.headers())
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        return headers

    def _incentives_get(self, path: str) -> dict[str, Any]:
        parsed_path = urlparse(path)
        if (
            not path.startswith("/api/v1/")
            or ".." in path
            or parsed_path.scheme
            or parsed_path.netloc
        ):
            raise FairgroundAPIError("Unsafe incentives path")
        url = f"{INCENTIVES_URL}{path}"
        headers = self._get_headers()
        status, body = self._urllib_call(url, headers, None, "GET")
        if status == 404:
            return {}
        if status < 200 or status >= 300:
            raise FairgroundAPIError(f"Fairground API returned HTTP {status}")
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise FairgroundAPIError("Fairground API response exceeded the safety limit")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise FairgroundAPIError("Fairground API returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise FairgroundAPIError("Fairground API returned an unexpected payload")
        return parsed

    def get_current_season_id(self) -> int:
        cached = type(self)._season_id
        if cached:
            return cached
        with type(self)._season_lock:
            if type(self)._season_id:
                return type(self)._season_id
            payload = self._incentives_get("/api/v1/seasons/current")
            season_id = current_season_id_from_payload(payload)
            type(self)._season_id = season_id
            return season_id

    def get_season_points(self, address: str, *, season_id: int | None = None) -> int:
        owner = normalize_address(address)
        sid = int(season_id) if season_id is not None else self.get_current_season_id()
        if sid <= 0:
            raise FairgroundAPIError("Invalid season id")
        payload = self._incentives_get(
            f"/api/v1/user_scores?owner={owner}&season_id={sid}&page=1&page_size=1"
        )
        if not payload:
            return 0
        return season_points_from_payload(payload, owner=owner)

    def get_markets(self) -> dict[str, Any]:
        return self._post("/market_service.v1.MarketService/GetMarkets", {})

    def get_market_summary(self, market_id: object) -> dict[str, Any]:
        market = normalize_market_id(market_id)
        return self._post(
            "/market_service.v1.MarketService/GetMarketSummary",
            {"market": {"marketId": market}},
        )

    def get_market_config(self, market_id: object) -> dict[str, Any]:
        market = normalize_market_id(market_id)
        return self._post(
            "/market_service.v1.MarketService/GetMarketConfigs",
            {"market": {"marketId": market}},
        )

    def get_price(self, market_id: object) -> dict[str, Any]:
        market = normalize_market_id(market_id)
        return self._post(
            "/price_service.v1.PriceService/GetPrice",
            {"market": {"marketId": market}},
        )

    def get_orders(self, address: str) -> dict[str, Any]:
        owner = normalize_address(address)
        return self._post(
            "/order_service.v1.OrderService/GetOrders",
            {"address": owner, "limit": 50},
        )

    def get_portfolio(self, address: str) -> dict[str, Any]:
        owner = normalize_address(address)
        return self._post(
            "/portfolio.v1.PortfolioService/GetPortfolio",
            {"owner": owner},
        )

    def get_estimated_fees(
        self, market_id: object, margin: object, leverage: object
    ) -> dict[str, Any]:
        market = normalize_market_id(market_id)
        return self._post(
            "/order_service.v1.OrderService/GetEstimatedFees",
            {
                "marketId": market,
                "margin": float(margin),
                "leverage": float(leverage),
                "size": 0,
                "price": 0,
            },
        )

    def get_open_positions(self, address: str) -> dict[str, Any]:
        owner = normalize_address(address)
        return self._post(
            "/position_service.v1.PositionService/GetOpenPositions",
            {"address": owner},
        )

    def get_user_fills(self, address: str, *, limit: int = 50) -> dict[str, Any]:
        owner = normalize_address(address)
        safe_limit = max(1, min(int(limit), 100))
        return self._post(
            "/fill_history_service.v1.FillHistoryService/GetUserFills",
            {"address": owner, "limit": safe_limit},
        )
