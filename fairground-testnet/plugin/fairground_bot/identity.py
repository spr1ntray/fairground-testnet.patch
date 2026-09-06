"""HTTP identity from an AdsPower profile — no browser start.

Fairground API and incentives see User-Agent, Client Hints, Accept-Language,
Origin and the proxy IP. AdsPower also stores timezone, screen, cores, DNT —
those go on the identity object. Only the fields that actually travel over
HTTP are put on requests. Canvas/WebGL/fonts never leave the Ads browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)
FAIRGROUND_ORIGIN = "https://fairground.fi"
_CHROME_RE = re.compile(r"(?:Chrome|CriOS)/(\d+)")
_EDGE_RE = re.compile(r"Edg(?:e|A|iOS)?/(\d+)")
_SECRET_FIELD = ("pass", "user", "cookie", "proxy", "fakey", "secret", "token", "auth")
_FP_KEYS = (
    "fingerprint_config",
    "fingerprintConfig",
    "finger_print_config",
    "fp_config",
    "browser_fingerprint",
)
# Chromium GREASE + brand shuffle. Seed is the major version.
# https://source.chromium.org/chromium/chromium/src/+/main:components/embedder_support/user_agent_utils.cc
_GREASE_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASE_VERSIONS = ("8", "99", "24")
_BRAND_ORDERS = (
    (0, 1, 2),
    (0, 2, 1),
    (1, 0, 2),
    (1, 2, 0),
    (2, 0, 1),
    (2, 1, 0),
)


@dataclass(frozen=True)
class BrowserIdentity:
    user_agent: str
    accept_language: str
    sec_ch_ua: str
    sec_ch_ua_mobile: str
    sec_ch_ua_platform: str
    chrome_major: int
    platform: str
    timezone: str
    screen: str
    hardware_concurrency: str
    device_memory: str
    dnt: str
    fallback: bool
    profile_fields: tuple[str, ...] = ()
    source: str = "ads"

    def headers(self) -> dict[str, str]:
        # Order and names follow Chrome HTTP/1.1 fetch() to a same-site API.
        # Low-entropy Client Hints only — full-version-list / Device-Memory
        # need Accept-CH and real build numbers, so they stay off the wire.
        out: dict[str, str] = {}
        if self.sec_ch_ua:
            out["sec-ch-ua-platform"] = self.sec_ch_ua_platform or '"Windows"'
            out["User-Agent"] = self.user_agent
            out["sec-ch-ua"] = self.sec_ch_ua
            out["sec-ch-ua-mobile"] = self.sec_ch_ua_mobile or "?0"
        else:
            out["User-Agent"] = self.user_agent
        out["Accept"] = "*/*"
        out["Origin"] = FAIRGROUND_ORIGIN
        out["Sec-Fetch-Site"] = "same-site"
        out["Sec-Fetch-Mode"] = "cors"
        out["Sec-Fetch-Dest"] = "empty"
        out["Referer"] = f"{FAIRGROUND_ORIGIN}/421614"
        out["Accept-Encoding"] = "gzip, deflate"
        out["Accept-Language"] = self.accept_language
        if self.dnt and self.chrome_major < 127:
            out["DNT"] = self.dnt
        return {key: value for key, value in out.items() if value}

    def summary(self) -> dict[str, Any]:
        return {
            "chrome": self.chrome_major,
            "platform": self.platform,
            "source": self.source,
            "fallback": self.fallback,
            "language": self.accept_language.split(",", 1)[0],
            "timezone": self.timezone or None,
            "screen": self.screen or None,
            "cores": self.hardware_concurrency or None,
            "memory": self.device_memory or None,
        }


def identity_from_profile(row: dict[str, Any] | None) -> BrowserIdentity:
    data = row if isinstance(row, dict) else {}
    fp = _fingerprint_dict(data)

    ua = _first_str(
        fp.get("ua"),
        fp.get("user_agent"),
        data.get("ua"),
        data.get("user_agent"),
        data.get("useragent"),
    )
    kernel = fp.get("browser_kernel_config") if isinstance(fp.get("browser_kernel_config"), dict) else {}
    kernel_version = str(kernel.get("version") or "").strip()
    if not ua and kernel_version.isdigit():
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{kernel_version}.0.0.0 Safari/537.36"
        )
    fallback = not bool(ua)
    if not ua:
        ua = DEFAULT_UA

    chrome_major, brand = _chrome_brand(ua, kernel_version)
    platform, mobile = _platform(ua)
    language = _accept_language(fp.get("language") or data.get("language"))
    timezone = _first_str(fp.get("timezone"), data.get("timezone"))
    screen = _screen(fp.get("screen_resolution") or fp.get("screen") or data.get("screen_resolution"))
    cores = _first_str(fp.get("hardware_concurrency"), data.get("hardware_concurrency"))
    memory = _first_str(fp.get("device_memory"), data.get("device_memory"))
    dnt = _dnt(fp.get("do_not_track") or data.get("do_not_track"))

    return BrowserIdentity(
        user_agent=ua,
        accept_language=language,
        sec_ch_ua=_sec_ch_ua(chrome_major, brand),
        sec_ch_ua_mobile=mobile,
        sec_ch_ua_platform=f'"{platform}"',
        chrome_major=chrome_major,
        platform=platform,
        timezone=timezone,
        screen=screen,
        hardware_concurrency=cores,
        device_memory=memory,
        dnt=dnt,
        fallback=fallback,
        profile_fields=_public_fields(data),
        source="ads",
    )


def identity_from_pool(seed: str) -> BrowserIdentity:
    from .fingerprint_pool import load_bank, pick_profile

    ident = identity_from_profile({"fingerprint_config": pick_profile(seed)})
    source = "bank" if load_bank() else "pool"
    return replace(ident, source=source, fallback=False, profile_fields=(source,))


def bank_identities(seeds: list[str]) -> dict[str, BrowserIdentity]:
    """One bank row per seed. Walks on collision so two wallets never share a UA."""
    from .fingerprint_pool import load_bank, pick_unique

    source = "bank" if load_bank() else "pool"
    profiles = pick_unique(seeds)
    out: dict[str, BrowserIdentity] = {}
    for seed in seeds:
        ident = identity_from_profile({"fingerprint_config": profiles[seed or "fairground"]})
        out[seed] = replace(ident, source=source, fallback=False, profile_fields=(source,))
    return out


def resolve_identity(seed: str, ads_row: dict[str, Any] | None = None) -> BrowserIdentity:
    if ads_row:
        ident = identity_from_profile(ads_row)
        if not ident.fallback and ident.user_agent:
            return replace(ident, source="ads")
    return identity_from_pool(seed)


def default_identity() -> BrowserIdentity:
    return identity_from_pool("fairground-default")


def _fingerprint_dict(row: dict[str, Any]) -> dict[str, Any]:
    for key in _FP_KEYS:
        value = row.get(key)
        if isinstance(value, dict) and value:
            return value
        if isinstance(value, str) and value.startswith("{"):
            try:
                import json

                parsed = json.loads(value)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and parsed:
                return parsed
    return {}


def _public_fields(row: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for key in row.keys():
        lower = str(key).lower()
        if any(token in lower for token in _SECRET_FIELD):
            continue
        names.append(str(key))
    return tuple(sorted(names)[:24])


def _first_str(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in {"none", "null", "n/a", "ua_auto", "default"}:
            return text
    return ""


def _chrome_brand(ua: str, kernel_version: str) -> tuple[int, str]:
    edge = _EDGE_RE.search(ua)
    chrome = _CHROME_RE.search(ua)
    major = 143
    if chrome:
        major = int(chrome.group(1))
    elif kernel_version.isdigit():
        major = int(kernel_version)
    brand = "Google Chrome"
    if edge and (not chrome or int(edge.group(1)) >= (chrome and int(chrome.group(1)) or 0)):
        brand = "Microsoft Edge"
        major = int(edge.group(1))
    return major, brand


def _platform(ua: str) -> tuple[str, str]:
    if "Android" in ua:
        return "Android", "?1"
    if "iPhone" in ua or "iPad" in ua:
        return "iOS", "?1"
    if "Mac OS X" in ua or "Macintosh" in ua:
        return "macOS", "?0"
    if "Linux" in ua:
        return "Linux", "?0"
    return "Windows", "?0"


def _grease_brand(major: int) -> tuple[str, str]:
    brand = (
        "Not"
        + _GREASE_CHARS[major % len(_GREASE_CHARS)]
        + "A"
        + _GREASE_CHARS[(major + 1) % len(_GREASE_CHARS)]
        + "Brand"
    )
    version = _GREASE_VERSIONS[major % len(_GREASE_VERSIONS)]
    return brand, version


def _sec_ch_ua(major: int, brand: str) -> str:
    grease_brand, grease_version = _grease_brand(major)
    items = [
        f'"{grease_brand}";v="{grease_version}"',
        f'"Chromium";v="{major}"',
        f'"{brand}";v="{major}"',
    ]
    order = _BRAND_ORDERS[major % len(_BRAND_ORDERS)]
    shuffled = [""] * 3
    for index, slot in enumerate(order):
        shuffled[slot] = items[index]
    return ", ".join(shuffled)


def _accept_language(language: Any) -> str:
    parts: list[str] = []
    if isinstance(language, str) and language.strip():
        parts = [item.strip() for item in language.split(",") if item.strip()]
    elif isinstance(language, (list, tuple)):
        parts = [str(item).strip() for item in language if str(item).strip()]
    cleaned: list[str] = []
    for item in parts:
        token = item.split(";", 1)[0].strip()
        if token and token not in cleaned:
            cleaned.append(token)
    if not cleaned:
        cleaned = ["en-US", "en"]
    out = [cleaned[0]]
    quality = 0.9
    for item in cleaned[1:6]:
        out.append(f"{item};q={quality:.1f}")
        quality = max(0.1, quality - 0.1)
    return ",".join(out)


def _screen(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"none", "random"}:
        return ""
    return text.replace("_", "x")


def _dnt(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return "1"
    return ""
