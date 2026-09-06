from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import Settings
from .validation import ValidationError, decimal_value, normalize_market_id


FEE_SCALER_DENOMINATOR = Decimal("100000")
MAX_MARKET_FEE_SCALER = Decimal("100000")
MAX_LIMIT_DISTANCE_BPS = Decimal("1000000")


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True, slots=True)
class MarketLimits:
    market_id: str
    market_name: str
    min_leverage: Decimal
    max_leverage: Decimal
    min_trade_size: Decimal
    max_trade_size: Decimal
    trade_fee_pct: Decimal | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "MarketLimits":
        try:
            raw_fee = raw.get("tradeFeePct")
            if raw_fee is None and isinstance(raw.get("feeConfig"), dict):
                raw_fee = raw["feeConfig"].get("tradeFeePct")
            trade_fee_pct = decimal_value(
                raw_fee,
                "tradeFeePct",
                allow_zero=True,
                optional=True,
            )
            if trade_fee_pct is not None and trade_fee_pct > MAX_MARKET_FEE_SCALER:
                raise ValidationError("Market fee scaler exceeds the local safety bound")
            return cls(
                market_id=normalize_market_id(raw.get("marketId")),
                market_name=str(raw["marketName"]).strip(),
                min_leverage=decimal_value(raw.get("minLeverage"), "minLeverage"),
                max_leverage=decimal_value(raw.get("maxLeverage"), "maxLeverage"),
                min_trade_size=decimal_value(raw.get("minTradeSize"), "minTradeSize"),
                max_trade_size=decimal_value(raw.get("maxTradeSize"), "maxTradeSize"),
                trade_fee_pct=trade_fee_pct,
            )
        except (KeyError, TypeError) as exc:
            raise ValidationError("Market configuration is incomplete") from exc


@dataclass(frozen=True, slots=True)
class TradeIntent:
    market_id: str
    market_name: str
    side: str
    margin: Decimal
    leverage: Decimal
    limit_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "TradeIntent":
        side = str(raw.get("side", "")).strip().lower()
        if side not in {"long", "short"}:
            raise ValidationError("side must be either long or short")
        market_name = str(raw.get("marketName", "")).strip()
        if not market_name or len(market_name) > 80:
            raise ValidationError("marketName is required")
        return cls(
            market_id=normalize_market_id(raw.get("marketId")),
            market_name=market_name,
            side=side,
            margin=decimal_value(raw.get("margin"), "margin"),
            leverage=decimal_value(raw.get("leverage"), "leverage"),
            limit_price=decimal_value(raw.get("limitPrice"), "limitPrice"),
            stop_loss=decimal_value(raw.get("stopLoss"), "stopLoss", optional=True),
            take_profit=decimal_value(raw.get("takeProfit"), "takeProfit", optional=True),
        )


@dataclass(frozen=True, slots=True)
class TradePreview:
    mode: str
    market_id: str
    market_name: str
    side: str
    margin_usdc: str
    leverage: str
    gross_notional_intent_usdc: str
    estimated_effective_margin_usdc: str
    estimated_effective_notional_usdc: str
    limit_price: str
    stop_loss: str | None
    take_profit: str | None
    oracle_reference_price: str
    limit_distance_bps: str
    protection_reference_price: str
    estimated_open_fee_usdc: str
    estimated_close_fee_usdc: str
    estimated_round_trip_fee_usdc: str
    estimated_round_trip_bps: str
    estimated_fee_per_1000_usdc: str
    market_fee_rate_bps: str
    fee_source: str
    cost_scope: str
    gas_estimate: str
    executable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SafetyPolicy:
    """Deterministic validation for a one-shot, non-executable trade preview."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(
        self,
        intent: TradeIntent,
        market: MarketLimits,
        *,
        official_open_fee: Decimal | None = None,
        oracle_reference_price: Decimal | None = None,
    ) -> tuple[TradePreview, list[str]]:
        if intent.market_id != market.market_id or intent.market_name != market.market_name:
            raise ValidationError("Selected market no longer matches the live market configuration")
        if intent.margin > self.settings.max_margin_usdc:
            raise ValidationError(
                f"margin exceeds the local safety cap of {self.settings.max_margin_usdc} USDC"
            )

        allowed_max_leverage = min(market.max_leverage, self.settings.max_leverage)
        if intent.leverage < market.min_leverage or intent.leverage > allowed_max_leverage:
            raise ValidationError(
                f"leverage must be between {market.min_leverage} and {allowed_max_leverage}"
            )

        notional = intent.margin * intent.leverage
        if notional < market.min_trade_size:
            raise ValidationError(
                f"notional is below the market minimum of {market.min_trade_size} USDC"
            )
        if notional > min(market.max_trade_size, self.settings.max_notional_usdc):
            raise ValidationError(
                f"notional exceeds the local safety cap of {self.settings.max_notional_usdc} USDC"
            )

        protection_reference = oracle_reference_price or intent.limit_price
        if intent.side == "long":
            if intent.stop_loss is not None and intent.stop_loss >= protection_reference:
                raise ValidationError("long stop loss must be below the current oracle price")
            if intent.take_profit is not None and intent.take_profit <= protection_reference:
                raise ValidationError("long take profit must be above the current oracle price")
        else:
            if intent.stop_loss is not None and intent.stop_loss <= protection_reference:
                raise ValidationError("short stop loss must be above the current oracle price")
            if intent.take_profit is not None and intent.take_profit >= protection_reference:
                raise ValidationError("short take profit must be below the current oracle price")

        calculated_open_fee: Decimal | None = None
        if (
            official_open_fee is not None
            and official_open_fee >= 0
            and official_open_fee < intent.margin
        ):
            calculated_open_fee = official_open_fee
            fee_source = "Fairground GetEstimatedFees"
        elif market.trade_fee_pct is not None:
            # OpenAPI documents tradeFeePct as a fixed-point scaler and the
            # opening fee as margin * rate / (1 + rate). Live estimator values
            # currently match raw scaler / 100000.
            leveraged_rate = (
                market.trade_fee_pct / FEE_SCALER_DENOMINATOR
            ) * intent.leverage
            calculated_open_fee = (
                leveraged_rate / (Decimal("1") + leveraged_rate)
            ) * intent.margin
            fee_source = "live market fee scaler fallback"
        else:
            fee_source = "unavailable"

        market_fee_rate: Decimal | None = None
        if market.trade_fee_pct is not None:
            market_fee_rate = market.trade_fee_pct / FEE_SCALER_DENOMINATOR

        calculated_close_fee: Decimal | None = None
        calculated_round_trip_fee: Decimal | None = None
        estimated_round_trip_bps: Decimal | None = None
        estimated_fee_per_1000: Decimal | None = None
        if calculated_open_fee is not None:
            open_fee = _money(calculated_open_fee)
            effective_margin = intent.margin - calculated_open_fee
            effective_notional = effective_margin * intent.leverage
            effective_margin_text = _money(effective_margin)
            effective_notional_text = _money(effective_notional)
            if market_fee_rate is not None:
                calculated_close_fee = effective_notional * market_fee_rate
                calculated_round_trip_fee = calculated_open_fee + calculated_close_fee
                if effective_notional > 0:
                    estimated_round_trip_bps = (
                        calculated_round_trip_fee / effective_notional
                    ) * Decimal("10000")
                    estimated_fee_per_1000 = (
                        calculated_round_trip_fee / effective_notional
                    ) * Decimal("1000")
        else:
            open_fee = "unavailable"
            effective_margin_text = "unavailable"
            effective_notional_text = "unavailable"

        if oracle_reference_price is not None:
            limit_distance_bps = (
                abs(intent.limit_price - oracle_reference_price)
                / oracle_reference_price
                * Decimal("10000")
            )
            if limit_distance_bps > MAX_LIMIT_DISTANCE_BPS:
                raise ValidationError("limitPrice is outside the bounded oracle-distance range")
            limit_distance_text = _money(limit_distance_bps)
            oracle_reference_text = format(oracle_reference_price.normalize(), "f")
        else:
            limit_distance_bps = None
            limit_distance_text = "unavailable"
            oracle_reference_text = "unavailable"

        preview = TradePreview(
            mode="preview-only",
            market_id=market.market_id,
            market_name=market.market_name,
            side=intent.side,
            margin_usdc=_money(intent.margin),
            leverage=format(intent.leverage.normalize(), "f"),
            gross_notional_intent_usdc=_money(notional),
            estimated_effective_margin_usdc=effective_margin_text,
            estimated_effective_notional_usdc=effective_notional_text,
            limit_price=format(intent.limit_price.normalize(), "f"),
            stop_loss=(format(intent.stop_loss.normalize(), "f") if intent.stop_loss else None),
            take_profit=(
                format(intent.take_profit.normalize(), "f") if intent.take_profit else None
            ),
            oracle_reference_price=oracle_reference_text,
            limit_distance_bps=limit_distance_text,
            protection_reference_price=(
                "not applicable (no SL/TP)"
                if intent.stop_loss is None and intent.take_profit is None
                else (
                    f"{format(protection_reference.normalize(), 'f')} (live oracle)"
                    if oracle_reference_price is not None
                    else f"{format(protection_reference.normalize(), 'f')} (limit-price fallback)"
                )
            ),
            estimated_open_fee_usdc=open_fee,
            estimated_close_fee_usdc=(
                _money(calculated_close_fee)
                if calculated_close_fee is not None
                else "unavailable"
            ),
            estimated_round_trip_fee_usdc=(
                _money(calculated_round_trip_fee)
                if calculated_round_trip_fee is not None
                else "unavailable"
            ),
            estimated_round_trip_bps=(
                _money(estimated_round_trip_bps)
                if estimated_round_trip_bps is not None
                else "unavailable"
            ),
            estimated_fee_per_1000_usdc=(
                _money(estimated_fee_per_1000)
                if estimated_fee_per_1000 is not None
                else "unavailable"
            ),
            market_fee_rate_bps=(
                _money(market_fee_rate * Decimal("10000"))
                if market_fee_rate is not None
                else "unavailable"
            ),
            fee_source=fee_source,
            cost_scope=(
                "scenario: unchanged notional; open + close trading fees before rebates"
                if calculated_round_trip_fee is not None
                else "opening fee only; round-trip scenario unavailable"
            ),
            gas_estimate="unknown until an official transaction is built",
            executable=False,
        )
        warnings = [
            "Preview only: no transaction was built, signed, or broadcast.",
            "Fee configuration differs by market; the official estimator is preferred. Round-trip cost assumes an unchanged notional and excludes rebates, gas, price movement, and failed or partial fills.",
            "Fairground uses an oracle price and counterparty matching, so a limit order may remain pending or partially fill.",
        ]
        if oracle_reference_price is None:
            warnings.append(
                "Live oracle price was unavailable. Limit-price distance and market-open checks could not be completed."
            )
        elif limit_distance_bps is not None and limit_distance_bps > Decimal("100"):
            warnings.append(
                f"Limit price is {_money(limit_distance_bps)} bps away from the current oracle; review the threshold before any manual action."
            )
        return preview, warnings
