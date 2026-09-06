"""Secret-handling helpers: wipe, bound checks, never log raw material."""

from __future__ import annotations

import re
from typing import MutableSequence


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def wipe_str_list(values: MutableSequence[str]) -> None:
    for index in range(len(values)):
        values[index] = ""
    values.clear()


def wipe_mapping_secrets(record: dict, keys: tuple[str, ...] = ("private_key", "proxy", "adspower_api_key")) -> None:
    for key in keys:
        if key in record:
            record[key] = ""


def validate_secret_text(
    value: str,
    *,
    name: str,
    min_len: int = 1,
    max_len: int = 512,
    allow_empty: bool = False,
) -> str:
    """Reject oversized / control-character secrets before vault storage."""

    text = str(value or "")
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{name} is empty")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{name} must be a single line")
    if _CTRL.search(text):
        raise ValueError(f"{name} contains control characters")
    if len(text) < min_len:
        raise ValueError(f"{name} is too short")
    if len(text) > max_len:
        raise ValueError(f"{name} is too long (max {max_len})")
    return text


def validate_adspower_api_key(value: str, *, allow_empty: bool = True) -> str:
    return validate_secret_text(
        value,
        name="AdsPower API key",
        min_len=8,
        max_len=512,
        allow_empty=allow_empty,
    )


def validate_adspower_profile_id(value: str, *, allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError("AdsPower profile id is empty")
    if len(text) > 128:
        raise ValueError("AdsPower profile id is too long")
    if _CTRL.search(text) or any(ch.isspace() for ch in text):
        raise ValueError("AdsPower profile id has invalid characters")
    # AdsPower ids are typically alnum; allow common safe punctuation.
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", text):
        raise ValueError("AdsPower profile id has invalid characters")
    return text
