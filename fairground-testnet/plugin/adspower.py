"""AdsPower Local API client for Soft Hub /4.

Transport matches official Local API:
https://localapi-doc-en.adspower.com/docs/Rdw7Iu
Never logs API keys or profile IDs. Endpoints must stay on loopback.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

def scrub_secrets(text: str, extra: list[str] | None = None) -> str:
    result = str(text or "")
    for secret in extra or []:
        if secret and len(str(secret)) >= 6:
            result = result.replace(str(secret), "[REDACTED]")
    return result

LOCAL_HOSTS = frozenset(
    {
        "127.0.0.1",
        "localhost",
        "::1",
        "local.adspower.net",
        "local.adspower.com",
    }
)
DEFAULT_API_BASE = "http://local.adspower.com:50325"
_LOCAL_API_FILES = (
    "Library/Application Support/adspower_global/cwd_global/source/local_api",
    ".config/adspower_global/cwd_global/source/local_api",
)
_API_LOCK = threading.Lock()
_LAST_API_MONO = 0.0
_MIN_API_INTERVAL_SEC = 0.6
_START_LOCK = threading.Lock()
_LAST_START_MONO = 0.0
_MIN_START_INTERVAL_SEC = 1.8
_START_MAX_RETRIES = 6
_GET_MAX_RETRIES = 5
KEY_MESSAGE = (
    "AdsPower API key не совпал с Local API. "
    "Вставь ключ из AdsPower → Settings → Local API"
)


class AdsPowerError(RuntimeError):
    """Stable, secret-free AdsPower failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class AdsPowerSession:
    ws_url: str
    started_by_us: bool

    def __repr__(self) -> str:
        return f"AdsPowerSession(started_by_us={self.started_by_us})"


def ads_code(payload: dict[str, Any] | None) -> int:
    """AdsPower uses code=0 for success. `0 or -1` is -1 — never coalesce that way."""
    if not isinstance(payload, dict):
        return -1
    raw = payload.get("code")
    if raw is None or raw == "":
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _msg(payload: dict[str, Any] | None) -> str:
    return str((payload or {}).get("msg") or "").lower()


def _is_rate_limit(payload: dict[str, Any] | None) -> bool:
    text = _msg(payload)
    return "too many" in text or "rate" in text


def _is_key_error(payload: dict[str, Any] | None) -> bool:
    text = _msg(payload)
    return "api key" in text or "api-key" in text


def _failure(payload: dict[str, Any] | None, *, default: str) -> AdsPowerError | None:
    if ads_code(payload) == 0:
        return None
    if _is_key_error(payload):
        return AdsPowerError("adspower_unavailable", KEY_MESSAGE)
    if _is_rate_limit(payload):
        return AdsPowerError("adspower_unavailable", "AdsPower API rate limit — подожди секунду")
    return AdsPowerError("adspower_unavailable", default)


def normalize_api_key(value: str | None) -> str:
    key = (value or "").strip().strip('"').strip("'")
    key = key.replace("\u200b", "").replace("\ufeff", "")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def discover_api_base() -> str:
    from pathlib import Path

    home = Path.home()
    for relative in _LOCAL_API_FILES:
        path = home / relative
        try:
            text = path.read_text(encoding="utf-8").strip().split()[0]
        except Exception:
            continue
        if not text:
            continue
        try:
            return assert_local_http_base(text)
        except AdsPowerError:
            continue
    return DEFAULT_API_BASE


def normalize_profile_id(value: str | None) -> str:
    return (value or "").strip()


def _profile_ids(row: dict[str, Any]) -> set[str]:
    return {
        normalize_profile_id(str(row.get(key) or ""))
        for key in ("user_id", "profile_id")
        if normalize_profile_id(str(row.get(key) or ""))
    }


def _list_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        rows = data.get("list")
    elif isinstance(data, list):
        rows = data
    else:
        rows = None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def match_profile_row(payload: dict[str, Any] | None, profile_id: str) -> dict[str, Any] | None:
    pid = normalize_profile_id(profile_id)
    if not pid:
        return None
    matches = [row for row in _list_rows(payload) if pid in _profile_ids(row)]
    if not matches:
        return None
    return matches[0]


def _ambiguous(payload: dict[str, Any] | None, profile_id: str) -> bool:
    pid = normalize_profile_id(profile_id)
    matches = [row for row in _list_rows(payload) if pid in _profile_ids(row)]
    return len(matches) > 1


def _has_fingerprint(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    fp = row.get("fingerprint_config")
    if isinstance(fp, dict) and (fp.get("ua") or fp.get("language") or fp.get("browser_kernel_config")):
        return True
    return bool(str(row.get("ua") or "").strip())


def _merge_profile_rows(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if key == "fingerprint_config":
            left = base.get("fingerprint_config") if isinstance(base.get("fingerprint_config"), dict) else {}
            right = value if isinstance(value, dict) else {}
            merged[key] = {**left, **right} if (left or right) else value
        elif value not in (None, "", [], {}):
            merged[key] = value
    if not _has_fingerprint(merged) and _has_fingerprint(base):
        merged["fingerprint_config"] = base.get("fingerprint_config")
    return merged


def find_duplicate_profile_accounts(
    pairs: list[tuple[str, str]],
) -> list[list[str]]:
    """Return groups of account ids that share the same non-empty profile id."""
    buckets: dict[str, list[str]] = {}
    for account_id, profile_id in pairs:
        pid = normalize_profile_id(profile_id)
        if not pid:
            continue
        buckets.setdefault(pid, []).append(account_id)
    return [ids for ids in buckets.values() if len(ids) > 1]


def assert_local_http_base(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise AdsPowerError("unsafe_endpoint", "AdsPower API должен быть http(s)")
    host = (parsed.hostname or "").lower()
    if host not in LOCAL_HOSTS:
        raise AdsPowerError("unsafe_endpoint", "AdsPower API не локальный")
    if parsed.username or parsed.password:
        raise AdsPowerError("unsafe_endpoint", "AdsPower API URL не должен содержать credentials")
    return url.rstrip("/")


def assert_local_cdp_ws(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss", "http", "https"}:
        raise AdsPowerError("unsafe_endpoint", "AdsPower вернул небезопасный browser endpoint")
    host = (parsed.hostname or "").lower()
    if host not in LOCAL_HOSTS:
        raise AdsPowerError("unsafe_endpoint", "AdsPower CDP endpoint не локальный")
    if not ws_url:
        raise AdsPowerError("adspower_unavailable", "AdsPower start не вернул puppeteer ws")
    return ws_url


class AdsPowerClient:
    def __init__(
        self,
        api_key: str,
        *,
        api_base: str | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        key = normalize_api_key(api_key)
        if len(key) < 4:
            raise AdsPowerError("missing_secret", "Не задан AdsPower API key")
        self._api_key = key
        self.api_base = assert_local_http_base(api_base or discover_api_base())
        self.timeout_seconds = timeout_seconds
        self._http = requests.Session()
        self._http.trust_env = False
        self._http.headers.update({"Authorization": f"Bearer {key}"})

    def close(self) -> None:
        try:
            self._http.headers.pop("Authorization", None)
        except Exception:
            pass
        try:
            self._http.close()
        except Exception:
            pass
        self._api_key = ""

    def health(self) -> None:
        payload = self._get("/status", params=None)
        error = _failure(payload, default="AdsPower Local API недоступен — запусти AdsPower")
        if error is not None:
            raise error
        # /status does not check the API key. Authenticate once before workers.
        payload = self._get("/api/v1/user/list", params={"page": 1, "page_size": 1})
        error = _failure(payload, default="AdsPower Local API недоступен — запусти AdsPower")
        if error is not None:
            raise error

    def require_ready_profile(self, profile_id: str) -> dict[str, Any]:
        return self.get_profile(profile_id)

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        """Query profile metadata + fingerprint. Does not start Chrome."""
        pid = normalize_profile_id(profile_id)
        if len(pid) < 4:
            raise AdsPowerError("profile_missing", "У аккаунта нет AdsPower profile ID")
        row: dict[str, Any] | None = None
        v2: dict[str, Any] = {}
        try:
            v2 = self._post(
                "/api/v2/browser-profile/list",
                json_body={"profile_id": [pid], "page": 1, "limit": 10},
            )
        except AdsPowerError:
            v2 = {}
        if ads_code(v2) == 0:
            row = match_profile_row(v2, pid)
            if row is not None and _ambiguous(v2, pid):
                raise AdsPowerError(
                    "profile_ambiguous",
                    "AdsPower вернул несколько профилей на один ID",
                )
        v1 = self._get(
            "/api/v1/user/list",
            params={"user_id": pid, "page": 1, "page_size": 10},
        )
        error = _failure(v1, default="AdsPower не нашёл профиль")
        if error is not None and row is None:
            raise error
        if ads_code(v1) == 0:
            if _ambiguous(v1, pid):
                raise AdsPowerError(
                    "profile_ambiguous",
                    "AdsPower вернул несколько профилей на один ID",
                )
            row_v1 = match_profile_row(v1, pid)
            if row is None:
                row = row_v1
            elif row_v1:
                row = _merge_profile_rows(row_v1, row)
        if not row:
            raise AdsPowerError("profile_missing", "AdsPower не нашёл профиль")
        return row

    def is_active(self, profile_id: str) -> bool:
        payload = self._get(
            "/api/v1/browser/active",
            params={"user_id": normalize_profile_id(profile_id)},
        )
        if ads_code(payload) != 0:
            return False
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status = str((data or {}).get("status") or "").lower()
        return status == "active"

    def start_or_attach(self, profile_id: str) -> AdsPowerSession:
        pid = normalize_profile_id(profile_id)
        active = self._active_ws(pid)
        if active:
            return AdsPowerSession(ws_url=active, started_by_us=False)
        ws = self._start(pid)
        return AdsPowerSession(ws_url=ws, started_by_us=True)

    def stop_if_started(self, profile_id: str, started_by_us: bool) -> None:
        if not started_by_us:
            return
        self.stop_profile(profile_id)

    def stop_profile(self, profile_id: str) -> None:
        try:
            self._get(
                "/api/v1/browser/stop",
                params={"user_id": normalize_profile_id(profile_id)},
            )
        except Exception:
            return

    def _active_ws(self, profile_id: str) -> str | None:
        payload = self._get(
            "/api/v1/browser/active",
            params={"user_id": profile_id},
        )
        if ads_code(payload) != 0:
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if str((data or {}).get("status") or "").lower() != "active":
            return None
        ws = (data or {}).get("ws") if isinstance(data, dict) else None
        url = ""
        if isinstance(ws, dict):
            url = str(ws.get("puppeteer") or "")
        return assert_local_cdp_ws(url) if url else None

    def _start(self, profile_id: str) -> str:
        global _LAST_START_MONO
        last_error = "adspower_unavailable"
        params = {
            "user_id": profile_id,
            "open_tabs": 1,
            "ip_tab": 0,
            "headless": 0,
        }
        for attempt in range(1, _START_MAX_RETRIES + 1):
            with _START_LOCK:
                now = time.monotonic()
                wait = _MIN_START_INTERVAL_SEC - (now - _LAST_START_MONO)
                if wait > 0:
                    time.sleep(wait)
                try:
                    payload = self._get("/api/v1/browser/start", params=params)
                except AdsPowerError as exc:
                    _LAST_START_MONO = time.monotonic()
                    last_error = exc.code
                    payload = {"code": -1, "msg": exc.code}
                else:
                    _LAST_START_MONO = time.monotonic()
            code = ads_code(payload if isinstance(payload, dict) else None)
            if code == 0:
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                ws = (data or {}).get("ws") if isinstance(data, dict) else None
                url = ""
                if isinstance(ws, dict):
                    url = str(ws.get("puppeteer") or "")
                if not url:
                    raise AdsPowerError("adspower_unavailable", "AdsPower start не вернул puppeteer ws")
                return assert_local_cdp_ws(url)
            msg = str((payload or {}).get("msg") or last_error).lower()
            transient = any(
                token in msg for token in ("too many", "rate", "timeout", "busy", "connection")
            )
            if transient and attempt < _START_MAX_RETRIES:
                time.sleep(min(8.0, 1.4 * attempt + random.uniform(0.3, 1.1)))
                continue
            raise AdsPowerError("adspower_unavailable", "Не удалось запустить AdsPower-профиль")
        raise AdsPowerError("adspower_unavailable", "Не удалось запустить AdsPower-профиль")

    def _get(self, path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json_body: dict[str, Any] | None) -> dict[str, Any]:
        return self._request("POST", path, json_body=json_body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        global _LAST_API_MONO
        url = f"{self.api_base}{path}"
        last: dict[str, Any] = {}
        verb = (method or "GET").upper()
        for attempt in range(1, _GET_MAX_RETRIES + 1):
            with _API_LOCK:
                wait = _MIN_API_INTERVAL_SEC - (time.monotonic() - _LAST_API_MONO)
                if wait > 0:
                    time.sleep(wait)
                try:
                    if verb == "POST":
                        resp = self._http.post(
                            url,
                            params=params,
                            json=json_body or {},
                            timeout=self.timeout_seconds,
                        )
                    else:
                        resp = self._http.get(url, params=params, timeout=self.timeout_seconds)
                except requests.RequestException:
                    _LAST_API_MONO = time.monotonic()
                    if attempt < _GET_MAX_RETRIES:
                        time.sleep(min(4.0, 0.8 * attempt))
                        continue
                    raise AdsPowerError(
                        "adspower_unavailable",
                        "Локальный AdsPower API недоступен",
                    ) from None
                else:
                    _LAST_API_MONO = time.monotonic()
            try:
                payload = resp.json() if resp.content else {}
            except Exception:
                raise AdsPowerError("adspower_unavailable", "AdsPower вернул не JSON") from None
            if not isinstance(payload, dict):
                raise AdsPowerError("adspower_unavailable", "AdsPower вернул неожиданный ответ")
            if resp.status_code in {401, 403}:
                raise AdsPowerError("adspower_unavailable", KEY_MESSAGE)
            if resp.status_code >= 500:
                raise AdsPowerError("adspower_unavailable", "AdsPower API недоступен")
            if "msg" in payload:
                payload["msg"] = scrub_secrets(str(payload.get("msg") or ""), [self._api_key])
            last = payload
            if _is_rate_limit(payload) and attempt < _GET_MAX_RETRIES:
                time.sleep(min(6.0, 1.2 * attempt + random.uniform(0.2, 0.8)))
                continue
            return payload
        return last
