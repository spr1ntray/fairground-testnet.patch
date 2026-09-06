"""Stable browser identities when AdsPower is absent.

`profile_bank.json` is a fixed set of Ads-like fingerprints. One wallet always
maps to the same row. The generated combinatorial pool is a fallback only.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

_BANK_PATH = Path(__file__).with_name("profile_bank.json")

_CHROME = (
    124, 125, 126, 127, 128, 129, 130, 131, 132,
    133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145,
)

_OS = (
    ("Windows", "Windows NT 10.0; Win64; x64"),
    ("Windows", "Windows NT 10.0; Win64; x64"),
    # Chrome reduced-UA freezes macOS at 10_15_7 regardless of the real OS.
    ("macOS", "Macintosh; Intel Mac OS X 10_15_7"),
    ("macOS", "Macintosh; Intel Mac OS X 10_15_7"),
    ("macOS", "Macintosh; Intel Mac OS X 10_15_7"),
    ("macOS", "Macintosh; Intel Mac OS X 10_15_7"),
    ("Linux", "X11; Linux x86_64"),
)

_LOCALES = (
    (("en-US", "en"), "America/New_York", ("1920x1080", "1366x768", "2560x1440"), ("8", "12", "16")),
    (("en-US", "en"), "America/Chicago", ("1920x1080", "1536x864", "1440x900"), ("4", "8", "16")),
    (("en-US", "en"), "America/Los_Angeles", ("1920x1080", "2560x1440", "1680x1050"), ("8", "12", "16")),
    (("en-GB", "en"), "Europe/London", ("1920x1080", "1366x768", "2560x1440"), ("8", "12")),
    (("de-DE", "de", "en"), "Europe/Berlin", ("1920x1080", "2560x1440", "1366x768"), ("8", "16")),
    (("fr-FR", "fr", "en"), "Europe/Paris", ("1920x1080", "1366x768", "2560x1440"), ("4", "8", "12")),
    (("es-ES", "es", "en"), "Europe/Madrid", ("1920x1080", "1366x768"), ("4", "8")),
    (("pt-BR", "pt", "en"), "America/Sao_Paulo", ("1366x768", "1920x1080", "1536x864"), ("4", "8", "12")),
    (("it-IT", "it", "en"), "Europe/Rome", ("1920x1080", "1366x768"), ("4", "8", "12")),
    (("nl-NL", "nl", "en"), "Europe/Amsterdam", ("1920x1080", "2560x1440"), ("8", "16")),
    (("pl-PL", "pl", "en"), "Europe/Warsaw", ("1920x1080", "1366x768", "1600x900"), ("4", "8")),
    (("sv-SE", "sv", "en"), "Europe/Stockholm", ("1920x1200", "1920x1080", "2560x1440"), ("8", "12")),
    (("tr-TR", "tr", "en"), "Europe/Istanbul", ("1920x1080", "1366x768", "1536x864"), ("4", "8", "12")),
    (("uk-UA", "uk", "en"), "Europe/Kyiv", ("1920x1080", "1366x768"), ("4", "8")),
    (("ru-RU", "ru", "en"), "Europe/Moscow", ("1920x1080", "1366x768", "1600x900"), ("4", "8", "16")),
    (("ja-JP", "ja", "en"), "Asia/Tokyo", ("1440x900", "1680x1050", "2560x1600"), ("8", "12", "16")),
    (("ko-KR", "ko", "en"), "Asia/Seoul", ("1920x1080", "2560x1440", "1440x900"), ("8", "16")),
    (("zh-TW", "zh", "en"), "Asia/Taipei", ("1920x1080", "1366x768"), ("4", "8", "12")),
    (("th-TH", "th", "en"), "Asia/Bangkok", ("1366x768", "1920x1080"), ("4", "8")),
    (("vi-VN", "vi", "en"), "Asia/Ho_Chi_Minh", ("1366x768", "1920x1080"), ("4", "8")),
    (("id-ID", "id", "en"), "Asia/Jakarta", ("1366x768", "1920x1080"), ("4", "8")),
    (("en-AU", "en"), "Australia/Sydney", ("1920x1080", "2560x1440", "1440x900"), ("8", "16")),
    (("en-CA", "en"), "America/Toronto", ("1920x1080", "1366x768", "2560x1440"), ("8", "12")),
)


def _ua(os_token: str, chrome: int) -> str:
    return (
        f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome}.0.0.0 Safari/537.36"
    )


@lru_cache(maxsize=1)
def load_bank() -> tuple[dict[str, Any], ...]:
    try:
        raw = json.loads(_BANK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    rows: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("ua"):
                rows.append(dict(item))
    return tuple(rows)


@lru_cache(maxsize=1)
def all_profiles() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for loc_index, (languages, timezone, screens, cores) in enumerate(_LOCALES):
        for os_index, (_platform, os_token) in enumerate(_OS):
            for chrome in _CHROME:
                mix = loc_index + os_index + chrome
                screen = screens[mix % len(screens)]
                cpu = cores[mix % len(cores)]
                rows.append(
                    {
                        "ua": _ua(os_token, chrome),
                        "language": list(languages),
                        "timezone": timezone,
                        "screen_resolution": screen.replace("x", "_"),
                        "hardware_concurrency": cpu,
                    }
                )
    return tuple(rows)


def pick_profile(seed: str) -> dict[str, Any]:
    rows = load_bank() or all_profiles()
    digest = hashlib.sha256((seed or "fairground").encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(rows)
    return dict(rows[index])


def pick_unique(seeds: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Stable unique rows: hash to a slot, walk forward on collision."""
    rows = load_bank() or all_profiles()
    used: set[int] = set()
    out: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        key = seed or "fairground"
        if key in out:
            continue
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        start = int.from_bytes(digest[:8], "big") % len(rows)
        index = start
        chosen = start
        for _ in range(len(rows)):
            if index not in used:
                chosen = index
                break
            index = (index + 1) % len(rows)
        used.add(chosen)
        out[key] = dict(rows[chosen])
    return out
