from __future__ import annotations

from decimal import Decimal
from typing import Any

from .client import FairgroundAPIError, FairgroundClient
from .config import Settings
from .safety import MarketLimits, SafetyPolicy, TradeIntent
from .validation import (
    ValidationError,
    decimal_value,
    normalize_market_id,
)


class TradePreviewService:
    """Read-only validation boundary shared by the trading workflows.

    The service has no signer or transaction broadcaster. It refreshes live
    market data and applies the local safety policy before a write workflow is
    allowed to prepare an intent.
    """

    def __init__(
        self,
        settings: Settings,
        client: FairgroundClient | None = None,
    ) -> None:
        self.client = client or FairgroundClient(
            settings.api_url,
            timeout_seconds=settings.timeout_seconds,
        )
        self.policy = SafetyPolicy(settings)

    def markets(self) -> dict[str, Any]:
        payload = self.client.get_markets()
        if not isinstance(payload.get("markets"), list):
            raise FairgroundAPIError("Fairground returned an invalid markets payload")
        return payload

    def preview(self, raw: dict[str, Any]) -> dict[str, Any]:
        intent = TradeIntent.from_json(raw)
        live_markets = self.markets().get("markets", [])
        selected = next(
            (
                item
                for item in live_markets
                if isinstance(item, dict) and str(item.get("marketId")) == intent.market_id
            ),
            None,
        )
        if selected is None:
            raise ValidationError("Selected market is not active")
        market = MarketLimits.from_api(selected)
        try:
            summary_payload = self.client.get_market_summary(intent.market_id)
            summary = summary_payload["marketSummary"]
            if not isinstance(summary, dict):
                raise KeyError("marketSummary")
            summary_market_id = summary.get("marketId")
            if (
                summary_market_id is not None
                and normalize_market_id(summary_market_id) != intent.market_id
            ):
                raise FairgroundAPIError(
                    "Fairground returned a mismatched market summary"
                )
            oracle_price = decimal_value(summary.get("price"), "oraclePrice")
            if oracle_price is None:
                raise ValidationError("oraclePrice is required")
            raw_open = summary.get("isOpen")
            market_is_closed = raw_open is False or str(raw_open).strip().lower() in {
                "0",
                "false",
                "closed",
            }
            if market_is_closed:
                raise ValidationError("Selected market is currently closed")
        except FairgroundAPIError:
            raise
        except ValidationError as exc:
            if str(exc) == "Selected market is currently closed":
                raise
            raise FairgroundAPIError(
                "Fairground returned an invalid market summary"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise FairgroundAPIError(
                "Fairground returned an invalid market summary"
            ) from exc

        # Reject unsafe user parameters before calling even a read-only fee
        # estimator. The result is recalculated below with the official fee.
        self.policy.preview(
            intent,
            market,
            oracle_reference_price=oracle_price,
        )
        official_fee: Decimal | None = None
        fee_warning: str | None = None
        try:
            fee_payload = self.client.get_estimated_fees(
                intent.market_id, intent.margin, intent.leverage
            )
            raw_fee = decimal_value(
                fee_payload.get("fees"),
                "fees",
                allow_zero=True,
            )
            if raw_fee is not None and raw_fee < intent.margin:
                official_fee = raw_fee
            else:
                fee_warning = (
                    "Fairground fee estimator returned no usable fee; "
                    "the live market scaler fallback is shown when available."
                )
        except (
            FairgroundAPIError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            fee_warning = (
                "Fairground fee estimator was unavailable; "
                "the live market scaler fallback is shown when available."
            )
        preview, warnings = self.policy.preview(
            intent,
            market,
            official_open_fee=official_fee,
            oracle_reference_price=oracle_price,
        )
        if fee_warning:
            warnings.append(fee_warning)
        if summary.get("isOpen") is None:
            warnings.append(
                "The market summary did not expose an explicit open/closed flag."
            )
        return {"preview": preview.to_dict(), "warnings": warnings}
