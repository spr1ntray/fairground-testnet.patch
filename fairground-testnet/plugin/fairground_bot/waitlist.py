"""Season 3 waitlist admission.

Fairground grants test USDC (and gas ETH) only after waitlist access.
A wallet with no test USDC and no trading activity is not admitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True, slots=True)
class WaitlistAdmission:
    admitted: bool
    usdc_units: int
    positions: int
    orders: int
    fills: int
    volume_usdc: str = "0"

    @property
    def reason(self) -> str:
        if self.admitted:
            return "в waitlist"
        return "не в waitlist"


def _positive_decimal(value: object) -> bool:
    try:
        number = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return number.is_finite() and number > 0


def admission_from_balances(
    *,
    usdc_units: int = 0,
    positions: int = 0,
    orders: int = 0,
    fills: int = 0,
    volume_usdc: object = "0",
) -> WaitlistAdmission:
    units = int(usdc_units or 0)
    pos = int(positions or 0)
    ord_n = int(orders or 0)
    fill_n = int(fills or 0)
    volume = str(volume_usdc or "0")
    admitted = (
        units > 0
        or pos > 0
        or ord_n > 0
        or fill_n > 0
        or _positive_decimal(volume)
    )
    return WaitlistAdmission(
        admitted=admitted,
        usdc_units=max(0, units),
        positions=max(0, pos),
        orders=max(0, ord_n),
        fills=max(0, fill_n),
        volume_usdc=volume,
    )


def _len_list(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, list):
            return len(raw)
    return 0


def admission_from_chain_and_api(
    *,
    usdc_units: int,
    positions_payload: dict[str, Any] | None = None,
    orders_payload: dict[str, Any] | None = None,
    portfolio_payload: dict[str, Any] | None = None,
) -> WaitlistAdmission:
    positions = 0
    if isinstance(positions_payload, dict):
        positions = _len_list(positions_payload, "positions", "openPositions")
    orders = 0
    if isinstance(orders_payload, dict):
        orders = _len_list(orders_payload, "orders", "openOrders")
    fills = 0
    volume = "0"
    if isinstance(portfolio_payload, dict):
        portfolio = portfolio_payload.get("portfolio")
        blob = portfolio if isinstance(portfolio, dict) else portfolio_payload
        fills = int(blob.get("totalTrades") or blob.get("fills") or 0)
        volume = str(blob.get("totalVolume") or blob.get("volume") or "0")
    return admission_from_balances(
        usdc_units=usdc_units,
        positions=positions,
        orders=orders,
        fills=fills,
        volume_usdc=volume,
    )
