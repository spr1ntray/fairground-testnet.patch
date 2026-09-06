from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re


ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
MARKET_ID_RE = re.compile(r"^[0-9]{1,20}$")
PLAIN_DECIMAL_RE = re.compile(r"^(?:\d{1,24}(?:\.\d{0,18})?|\.\d{1,18})$")


class ValidationError(ValueError):
    """Raised for untrusted user input that fails validation."""


def normalize_address(value: str) -> str:
    address = value.strip()
    if not ADDRESS_RE.fullmatch(address):
        raise ValidationError("Expected a 20-byte EVM public address (0x + 40 hex characters)")
    return address.lower()


def normalize_market_id(value: object) -> str:
    market_id = str(value).strip()
    if not MARKET_ID_RE.fullmatch(market_id):
        raise ValidationError("Market ID must be a positive uint64 decimal string")
    parsed = int(market_id)
    if parsed <= 0 or parsed > 2**64 - 1:
        raise ValidationError("Market ID is outside the uint64 range")
    return market_id


def decimal_value(
    value: object,
    name: str,
    *,
    allow_zero: bool = False,
    optional: bool = False,
) -> Decimal | None:
    raw = "" if value is None else str(value).strip()
    if optional and raw == "":
        return None
    if not PLAIN_DECIMAL_RE.fullmatch(raw):
        raise ValidationError(
            f"{name} must be a plain positive decimal with at most 24 integer and 18 fractional digits"
        )
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{name} must be a decimal number") from exc
    if not result.is_finite():
        raise ValidationError(f"{name} must be finite")
    if result < 0 or (result == 0 and not allow_zero):
        comparator = "zero or greater" if allow_zero else "greater than zero"
        raise ValidationError(f"{name} must be {comparator}")
    return result
