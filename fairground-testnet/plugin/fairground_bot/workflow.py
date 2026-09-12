from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import time
from typing import Any, Callable, Protocol

from .client import FairgroundAPIError, FairgroundClient
from .config import Settings
from .onchain import (
    BroadcastUnknown,
    OnchainError,
    PreparedWrite,
    ReceiptReverted,
    SignedWrite,
    SimulationError,
    to_base_units,
)
from .preview import TradePreviewService
from .state import (
    ActionRecord,
    CycleRecord,
    CycleState,
    Lease,
    StateStore,
    StoreError,
)
from .validation import ValidationError, decimal_value, normalize_address, normalize_market_id


def api_lot_agrees_with_chain(api_lot: object, chain_lot: int) -> bool:
    """Connect JSON often omits lotSize (0). New diamond dropped the field too."""

    try:
        value = Decimal(str(api_lot if api_lot is not None else "0"))
    except (InvalidOperation, ValueError):
        return False
    if value != value.to_integral_value() or value < 0:
        return False
    raw = int(value)
    chain = int(chain_lot)
    if chain < 0:
        return False
    return raw in (0, chain)


ACTIVE_ORDER_STATUSES = {"PENDING", "PARTIALLY_FILLED", "PARTIALLY_MERGED"}
PARTIAL_ORDER_STATUSES = {"PARTIALLY_FILLED", "PARTIALLY_MERGED"}
TERMINAL_ORDER_STATUSES = {
    "FULLY_FILLED",
    "CLOSED",
    "CANCELLED",
    "MERGED",
    "PARTIALLY_CANCELLED",
}

# A terminal entry order and its resulting position are indexed by separate API
# projections.  Do not interpret an initially empty position projection as a
# proven zero-fill/flat account until it has remained empty for this interval.
POST_ENTRY_INDEXING_GRACE_SECONDS = 30
REMOTE_INDEX_GRACE_SECONDS = 45
POSITION_VERIFY_TIMEOUT_SECONDS = 90
PREFLIGHT_VALIDATION_PREFIX = "Preflight validation rejected: "
# Testnet volume farm: keep gas headroom tight so small ETH balances still trade.
ENTRY_GAS_RESERVE_WRITES = 2


def format_eth_wei(value: int) -> str:
    amount = Decimal(max(0, int(value))) / Decimal(10**18)
    rendered = format(
        amount.quantize(Decimal("0.000000000000000001")), "f"
    )
    return rendered.rstrip("0").rstrip(".") or "0"


def entry_gas_reserve_wei(prepared: PreparedWrite) -> int:
    return prepared.max_cost_wei * ENTRY_GAS_RESERVE_WRITES


class WorkflowError(RuntimeError):
    pass


class WorkflowNeedsReconciliation(WorkflowError):
    pass


class ChainGateway(Protocol):
    account: str

    def verify_deployment(self) -> Any: ...

    def get_market_config(self, market_id: int) -> Any: ...

    def build_approval(self, amount_units: int) -> PreparedWrite: ...

    def build_open(self, **kwargs: Any) -> PreparedWrite: ...

    def build_reduce(self, **kwargs: Any) -> PreparedWrite: ...

    def build_cancel(self, **kwargs: Any) -> PreparedWrite: ...

    def sign(self, prepared: PreparedWrite) -> Any: ...

    def broadcast(self, signed: Any) -> str: ...

    def wait_for_receipt(self, tx_hash: str, **kwargs: Any) -> Any: ...

    def get_receipt(self, tx_hash: str) -> Any | None: ...

    def current_block(self) -> int: ...


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _decimal(raw: object, name: str, *, allow_zero: bool = False) -> Decimal:
    value = decimal_value(raw, name, allow_zero=allow_zero)
    if value is None:  # pragma: no cover - required inputs
        raise ValidationError(f"{name} is required")
    return value


@dataclass(frozen=True, slots=True)
class TradeCycleIntent:
    market_id: str
    market_name: str
    side: str
    margin: Decimal
    leverage: Decimal
    limit_price: Decimal
    hold_seconds: int = 60
    entry_ttl_seconds: int = 120
    partial_ttl_seconds: int = 30
    exit_ttl_seconds: int = 60
    poll_seconds: int = 3
    max_exit_attempts: int = 2
    exit_slippage_bps: int = 200  # market-style ±2% protective band
    allow_approval: bool = False

    @classmethod
    def from_values(
        cls,
        *,
        market_id: object,
        market_name: object,
        side: object,
        margin: object,
        leverage: object,
        limit_price: object,
        hold_seconds: object = 60,
        entry_ttl_seconds: object = 120,
        partial_ttl_seconds: object = 30,
        exit_ttl_seconds: object = 60,
        poll_seconds: object = 3,
        max_exit_attempts: object = 2,
        exit_slippage_bps: object = 200,
        allow_approval: bool = False,
    ) -> "TradeCycleIntent":
        market = normalize_market_id(market_id)
        name = str(market_name).strip()
        if not name or len(name) > 80:
            raise ValidationError("marketName must be between 1 and 80 characters")
        normalized_side = str(side).strip().lower()
        if normalized_side not in {"long", "short"}:
            raise ValidationError("side must be long or short")
        return cls(
            market_id=market,
            market_name=name,
            side=normalized_side,
            margin=_decimal(margin, "margin"),
            leverage=_decimal(leverage, "leverage"),
            limit_price=_decimal(limit_price, "limitPrice", allow_zero=True),
            hold_seconds=_bounded_int(hold_seconds, "holdSeconds", 0, 86_400),
            entry_ttl_seconds=_bounded_int(
                entry_ttl_seconds, "entryTtlSeconds", 15, 900
            ),
            partial_ttl_seconds=_bounded_int(
                partial_ttl_seconds, "partialTtlSeconds", 10, 300
            ),
            exit_ttl_seconds=_bounded_int(
                exit_ttl_seconds, "exitTtlSeconds", 15, 900
            ),
            poll_seconds=_bounded_int(poll_seconds, "pollSeconds", 1, 30),
            max_exit_attempts=_bounded_int(
                max_exit_attempts, "maxExitAttempts", 1, 5
            ),
            exit_slippage_bps=_bounded_int(
                exit_slippage_bps, "exitSlippageBps", 1, 1000
            ),
            allow_approval=bool(allow_approval),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "marketId": self.market_id,
            "marketName": self.market_name,
            "side": self.side,
            "margin": str(self.margin),
            "leverage": str(self.leverage),
            "limitPrice": str(self.limit_price),
            "holdSeconds": self.hold_seconds,
            "entryTtlSeconds": self.entry_ttl_seconds,
            "partialTtlSeconds": self.partial_ttl_seconds,
            "exitTtlSeconds": self.exit_ttl_seconds,
            "pollSeconds": self.poll_seconds,
            "maxExitAttempts": self.max_exit_attempts,
            "exitSlippageBps": self.exit_slippage_bps,
            "allowApproval": self.allow_approval,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TradeCycleIntent":
        return cls.from_values(
            market_id=raw.get("marketId"),
            market_name=raw.get("marketName"),
            side=raw.get("side"),
            margin=raw.get("margin"),
            leverage=raw.get("leverage"),
            limit_price=raw.get("limitPrice"),
            hold_seconds=raw.get("holdSeconds", 60),
            entry_ttl_seconds=raw.get("entryTtlSeconds", 120),
            partial_ttl_seconds=raw.get("partialTtlSeconds", 30),
            exit_ttl_seconds=raw.get("exitTtlSeconds", 60),
            poll_seconds=raw.get("pollSeconds", 3),
            max_exit_attempts=raw.get("maxExitAttempts", 2),
            exit_slippage_bps=raw.get("exitSlippageBps", 200),
            allow_approval=bool(raw.get("allowApproval", False)),
        )


def _enum_tail(value: object, prefix: str) -> str:
    text = str(value or "").strip().strip("'").upper()
    return text.removeprefix(prefix)


def _api_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError) as exc:
        raise FairgroundAPIError(f"Fairground returned an invalid {field}") from exc
    parts = result.as_tuple()
    if (
        not result.is_finite()
        or result < 0
        or result > Decimal("1e30")
        or len(parts.digits) > 80
        or abs(parts.exponent) > 80
    ):
        raise FairgroundAPIError(f"Fairground returned an invalid {field}")
    return result


@dataclass(frozen=True, slots=True)
class OrderView:
    order_id: int
    market_id: str
    owner: str | None
    side: str
    status: str
    reduce_only: bool
    threshold_price: Decimal
    size: Decimal
    filled_size: Decimal
    initial_margin: Decimal
    initial_notional: Decimal

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_ORDER_STATUSES

    @property
    def partial(self) -> bool:
        return self.status in PARTIAL_ORDER_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "OrderView":
        try:
            order_id = int(raw.get("orderId"))
        except (TypeError, ValueError) as exc:
            raise FairgroundAPIError("Fairground returned an invalid order ID") from exc
        owner_raw = raw.get("owner")
        owner = normalize_address(owner_raw) if isinstance(owner_raw, str) and owner_raw else None
        side = _enum_tail(raw.get("side"), "SIDE_")
        status = _enum_tail(raw.get("status"), "ORDER_STATUS_")
        if side not in {"LONG", "SHORT"}:
            raise FairgroundAPIError("Fairground returned an unknown order side")
        if status not in ACTIVE_ORDER_STATUSES | TERMINAL_ORDER_STATUSES:
            raise FairgroundAPIError("Fairground returned an unknown order status")
        return cls(
            order_id=order_id,
            market_id=normalize_market_id(raw.get("marketId")),
            owner=owner,
            side=side,
            status=status,
            reduce_only=bool(raw.get("reduceOnly", False)),
            threshold_price=_api_decimal(raw.get("thresholdPrice"), "threshold price"),
            size=_api_decimal(raw.get("size"), "order size"),
            filled_size=_api_decimal(raw.get("filledSize"), "filled size"),
            initial_margin=_api_decimal(raw.get("initialMargin"), "initial margin"),
            initial_notional=_api_decimal(raw.get("initialNotional"), "initial notional"),
        )


@dataclass(frozen=True, slots=True)
class PositionView:
    position_id: int
    market_id: str
    side: str
    nominal_size: Decimal

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "PositionView":
        try:
            position_id = int(raw.get("positionId"))
        except (TypeError, ValueError) as exc:
            raise FairgroundAPIError("Fairground returned an invalid position ID") from exc
        side = _enum_tail(raw.get("side"), "SIDE_")
        if side not in {"LONG", "SHORT"}:
            raise FairgroundAPIError("Fairground returned an unknown position side")
        return cls(
            position_id=position_id,
            market_id=normalize_market_id(raw.get("marketId")),
            side=side,
            nominal_size=_api_decimal(raw.get("nominalSize"), "position size"),
        )


def _items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = payload.get(key, [])
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise FairgroundAPIError(f"Fairground returned an invalid {key} payload")
    return raw


@dataclass(frozen=True, slots=True)
class RemoteSnapshot:
    orders: tuple[OrderView, ...]
    positions: tuple[PositionView, ...]

    @property
    def active_orders(self) -> tuple[OrderView, ...]:
        return tuple(order for order in self.orders if order.active)


def terminalize_invalid_preflight_without_chain(
    *,
    settings: Settings,
    client: FairgroundClient,
    store: StateStore,
    account: str,
) -> CycleRecord | None:
    """Repair a deterministic legacy PREFLIGHT without loading a signer.

    A pristine PREFLIGHT cannot have prepared, signed, or broadcast a write.
    This narrow helper repeats only the live Guard preview while holding the
    account lease.  It terminalizes deterministic ValidationError failures and
    leaves valid or transiently-unverifiable cycles untouched so every path
    that could write still requires the full deployment verification.
    """

    owner = normalize_address(account)
    if settings.account is not None and normalize_address(settings.account) != owner:
        raise WorkflowError("Settings account does not match the PREFLIGHT account")
    lease = store.acquire_lease(owner)
    try:
        record = store.get_active_cycle(owner)
        if record is None or record.state is not CycleState.PREFLIGHT:
            return record
        if record.chain_id != settings.chain_id:
            raise WorkflowError("PREFLIGHT chain does not match the configured chain")
        if record.runtime or store.actions_for_cycle(record.cycle_id):
            raise WorkflowNeedsReconciliation(
                "PREFLIGHT contains unexpected runtime/action data; automatic repair refused"
            )
        try:
            intent = TradeCycleIntent.from_dict(record.intent)
            TradePreviewService(settings, client=client).preview(
                {
                    "marketId": intent.market_id,
                    "marketName": intent.market_name,
                    "side": intent.side,
                    "margin": str(intent.margin),
                    "leverage": str(intent.leverage),
                    "limitPrice": str(intent.limit_price),
                    "stopLoss": None,
                    "takeProfit": None,
                }
            )
        except ValidationError as exc:
            store.assert_lease(lease)
            return store.transition(
                record,
                CycleState.ABORTED,
                error=f"{PREFLIGHT_VALIDATION_PREFIX}{str(exc)[:180]}",
            )
        return record
    finally:
        store.release_lease(lease)


class CycleRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        client: FairgroundClient,
        store: StateStore,
        chain: ChainGateway,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        force_close: bool = False,
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.chain = chain
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.account = normalize_address(chain.account)
        self.lease: Lease | None = None
        # Explicit operator force-close (menu) — more reliable than only DB kill flag.
        self.force_close = bool(force_close)

    def _remote(self) -> RemoteSnapshot:
        orders = tuple(
            OrderView.from_api(item)
            for item in _items(self.client.get_orders(self.account), "orders")
        )
        if any(order.owner is not None and order.owner != self.account for order in orders):
            raise FairgroundAPIError(
                "Fairground returned an order outside the bound execution account"
            )
        positions = tuple(
            PositionView.from_api(item)
            for item in _items(
                self.client.get_open_positions(self.account), "positions"
            )
        )
        return RemoteSnapshot(orders=orders, positions=positions)

    def _heartbeat(self) -> None:
        if self.lease is None:
            raise StoreError("Execution lease is not held")
        self.lease = self.store.heartbeat(self.lease)

    def _kill(self) -> dict[str, str] | None:
        return self.store.get_kill(self.account)

    def _entry_is_forbidden(self) -> bool:
        return self._kill() is not None

    def _signing_is_frozen(self) -> bool:
        kill = self._kill()
        return kill is not None and kill.get("kill_mode") == "FREEZE_SIGNER"

    def _is_kill_flatten(self) -> bool:
        if self.force_close:
            return True
        kill = self._kill()
        return kill is not None and str(kill.get("kill_mode") or "") == "KILL_FLATTEN"

    def _force_exit_attempt_ceiling(self, intent: TradeCycleIntent) -> int:
        """Normal cycles use intent.max_exit_attempts; force-close gets extra budget."""

        base = int(intent.max_exit_attempts)
        if self._is_kill_flatten():
            return max(base + 8, 10)
        return base

    def _quarantine(self, record: CycleRecord, message: str) -> CycleRecord:
        clean = message.strip()[:240] or "Reconciliation invariant failed"
        if record.state == CycleState.QUARANTINED:
            return record
        try:
            return self.store.transition(
                record, CycleState.QUARANTINED, error=clean
            )
        except StoreError:
            raise WorkflowError(clean)

    def _market_config(self, intent: TradeCycleIntent) -> dict[str, Any]:
        payload = self.client.get_market_config(intent.market_id)
        raw = payload.get("marketConfig")
        if not isinstance(raw, dict):
            raise FairgroundAPIError("Fairground returned an invalid market config")
        if normalize_market_id(raw.get("marketId")) != intent.market_id:
            raise FairgroundAPIError("Fairground returned a mismatched market config")
        return raw

    def _cross_check_onchain_market(
        self,
        record: CycleRecord,
        intent: TradeCycleIntent,
    ) -> Any:
        chain_market = self.chain.get_market_config(int(intent.market_id))
        if chain_market.name != intent.market_name:
            raise WorkflowError("On-chain market name differs from the API/intent")
        if chain_market.tick_decimals != int(record.runtime["tick_decimals"]):
            raise WorkflowError("On-chain tick decimals differ from the API")
        if chain_market.size_decimals != int(record.runtime["size_decimals"]):
            raise WorkflowError("On-chain size decimals differ from the API")
        if chain_market.min_leverage_raw != int(record.runtime["min_leverage_raw"]):
            raise WorkflowError("On-chain minimum leverage differs from the API")
        if chain_market.max_leverage_raw != int(record.runtime["max_leverage_raw"]):
            raise WorkflowError("On-chain maximum leverage differs from the API")
        api_lot = record.runtime.get("lot_size", "0")
        if not api_lot_agrees_with_chain(api_lot, chain_market.lot_size_raw):
            raise WorkflowError(
                "On-chain lot size differs from the API "
                f"(api={api_lot} chain={chain_market.lot_size_raw})"
            )
        if chain_market.min_trade_size_units != int(record.runtime["min_trade_size_units"]):
            raise WorkflowError("On-chain minimum trade size differs from the API")
        if chain_market.max_trade_size_units != int(record.runtime["max_trade_size_units"]):
            raise WorkflowError("On-chain maximum trade size differs from the API")
        return chain_market

    def _exit_threshold(
        self, intent: TradeCycleIntent, tick_decimals: int
    ) -> tuple[Decimal, int, str]:
        payload = self.client.get_price(intent.market_id)
        price_raw = payload.get("price")
        if not isinstance(price_raw, dict):
            raise FairgroundAPIError("Fairground returned an invalid exit oracle price")
        if normalize_market_id(price_raw.get("marketId")) != intent.market_id:
            raise FairgroundAPIError("Fairground returned a mismatched exit oracle price")
        oracle_price = _api_decimal(price_raw.get("price"), "exit oracle price")
        if oracle_price <= 0:
            raise FairgroundAPIError("Fairground returned a zero exit oracle price")
        timestamp_raw = price_raw.get("timestamp")
        if not isinstance(timestamp_raw, str):
            raise FairgroundAPIError("Fairground exit oracle timestamp is missing")
        try:
            timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FairgroundAPIError("Fairground exit oracle timestamp is malformed") from exc
        age = abs((datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())
        if age > 120:
            raise FairgroundAPIError("Fairground exit oracle price is stale")
        # Force-close uses aggressive slippage so residual dust can fill.
        # Always market-style protective band (UI "Market + N%").
        # Floor 200 bps (2%); force-close can go wider.
        bps = int(getattr(intent, "exit_slippage_bps", 200) or 200)
        bps = max(bps, 200)
        if self._is_kill_flatten():
            bps = max(bps, 300)
        slippage = Decimal(bps) / Decimal(10_000)
        side = str(getattr(intent, "side", "long") or "long").strip().lower()
        if side == "long":
            # Close long → sell market: threshold below oracle.
            threshold = oracle_price * (Decimal(1) - slippage)
            rounding = ROUND_FLOOR
        else:
            # Close short → buy market: threshold above oracle.
            threshold = oracle_price * (Decimal(1) + slippage)
            rounding = ROUND_CEILING
        scaled = (threshold * (Decimal(10) ** tick_decimals)).to_integral_value(
            rounding=rounding
        )
        units = int(scaled)
        if units <= 0 or units >= 2**32:
            raise WorkflowError("Exit oracle threshold is outside uint32")
        return oracle_price, units, timestamp_raw

    @staticmethod
    def _baseline(orders: tuple[OrderView, ...], market_id: str, reduce: bool) -> int:
        return max(
            (
                order.order_id
                for order in orders
                if order.market_id == market_id and order.reduce_only is reduce
            ),
            default=0,
        )

    def _preflight(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        if self._entry_is_forbidden():
            return self.store.transition(
                record,
                CycleState.ABORTED,
                error="Kill switch is set; entry was not attempted",
            )
        try:
            preview = TradePreviewService(
                self.settings, client=self.client
            ).preview(
                {
                    "marketId": intent.market_id,
                    "marketName": intent.market_name,
                    "side": intent.side,
                    "margin": str(intent.margin),
                    "leverage": str(intent.leverage),
                    "limitPrice": str(intent.limit_price),
                    "stopLoss": None,
                    "takeProfit": None,
                }
            )
        except ValidationError as exc:
            # PREFLIGHT has not prepared, signed, or broadcast any action yet.
            # Deterministic intent/market-policy failures are terminal so a bad
            # configuration cannot strand the account in an endless resume loop.
            return self.store.transition(
                record,
                CycleState.ABORTED,
                error=f"{PREFLIGHT_VALIDATION_PREFIX}{str(exc)[:180]}",
            )
        remote = self._remote()
        if remote.active_orders or remote.positions:
            wait_from = self.wall_clock()
            while remote.active_orders or remote.positions:
                self._heartbeat()
                if self.wall_clock() - wait_from >= REMOTE_INDEX_GRACE_SECONDS:
                    return self._quarantine(
                        record,
                        "Account must be globally flat with no active orders before a new cycle",
                    )
                self.sleep(intent.poll_seconds)
                remote = self._remote()
        market = self._market_config(intent)
        tick_decimals = _bounded_int(
            market.get("tickDecimals"), "tickDecimals", 0, 12
        )
        size_decimals = _bounded_int(
            market.get("sizeDecimals"), "sizeDecimals", 0, 18
        )
        margin_units = to_base_units(intent.margin, 6, bits=48)
        notional_units = to_base_units(
            intent.margin * intent.leverage, 6, bits=48
        )
        limit_units = to_base_units(intent.limit_price, tick_decimals, bits=32)
        if margin_units <= 0 or notional_units <= 0:
            return self._quarantine(record, "Quantized margin/notional is zero")
        if (
            Decimal(limit_units) / (Decimal(10) ** tick_decimals)
            != intent.limit_price
        ):
            return self._quarantine(
                record,
                "Entry limit price must align exactly with the market tick decimals",
            )
        return self.store.transition(
            record,
            CycleState.FUNDS_CHECK,
            runtime_updates={
                "preview": preview["preview"],
                "tick_decimals": tick_decimals,
                "size_decimals": size_decimals,
                # The contract stores leverage with two fixed-point decimals;
                # the Connect API exposes the human-readable multiplier.
                "min_leverage_raw": to_base_units(
                    _api_decimal(market.get("minLeverage"), "minimum leverage"),
                    2,
                    bits=16,
                ),
                "max_leverage_raw": to_base_units(
                    _api_decimal(market.get("maxLeverage"), "maximum leverage"),
                    2,
                    bits=16,
                ),
                "lot_size": str(market.get("lotSize", "0")),
                "min_trade_size_units": to_base_units(
                    _api_decimal(market.get("minTradeSize"), "minimum trade size"),
                    6,
                    bits=48,
                ),
                "max_trade_size_units": to_base_units(
                    _api_decimal(market.get("maxTradeSize"), "maximum trade size"),
                    6,
                    bits=48,
                ),
                "margin_units": margin_units,
                "notional_units": notional_units,
                "limit_price_units": limit_units,
                "baseline_entry_id": self._baseline(
                    remote.orders, intent.market_id, False
                ),
                "baseline_exit_id": self._baseline(
                    remote.orders, intent.market_id, True
                ),
                "exit_attempts": 0,
            },
        )

    @staticmethod
    def _write_cost(prepared: PreparedWrite) -> int:
        return prepared.max_cost_wei

    @staticmethod
    def _reducing_resume_state(
        *,
        kind: str,
        from_state: CycleState,
    ) -> CycleState:
        """Return the state that can safely rebuild the stopped reducing write."""

        if kind == "exit" and from_state is CycleState.EXIT_ARMED:
            return CycleState.EXIT_ARMED
        if kind == "cancel_entry" and from_state in {
            CycleState.ENTRY_PENDING,
            CycleState.ENTRY_PARTIAL,
        }:
            return from_state
        if kind == "cancel_exit" and from_state in {
            CycleState.EXIT_PENDING,
            CycleState.EXIT_PARTIAL,
        }:
            return from_state
        raise WorkflowError(
            f"Risk-reducing action {kind!r} cannot resume from {from_state.value}"
        )

    def _pause_for_reducing_gas(
        self,
        record: CycleRecord,
        prepared: PreparedWrite,
        *,
        resume_state: CycleState,
    ) -> CycleRecord | None:
        """Require only the exact next cancel/reduce ceiling, never entry reserve."""

        snapshot = self.chain.verify_deployment()
        available = int(snapshot.eth_balance_wei)
        required = prepared.max_cost_wei
        if available >= required:
            return None
        shortfall = required - available
        return self.store.transition(
            record,
            CycleState.PAUSED,
            runtime_updates={
                "resume_state": resume_state.value,
                "close_gas_balance_wei": available,
                "close_gas_required_wei": required,
                "close_gas_shortfall_wei": shortfall,
                "close_gas_action": prepared.kind,
                "close_gas_limit": int(prepared.transaction["gas"]),
                "close_max_fee_per_gas_wei": int(
                    prepared.transaction["maxFeePerGas"]
                ),
            },
            error=(
                "Недостаточно Arbitrum Sepolia ETH для следующей аварийной "
                f"операции {prepared.kind.upper()}: баланс "
                f"{format_eth_wei(available)} ETH · нужно ≥ "
                f"{format_eth_wei(required)} ETH · не хватает "
                f"{format_eth_wei(shortfall)} ETH. Новая сделка запрещена; "
                "после пополнения повтори принудительное закрытие."
            ),
        )

    def _abort_unbroadcast(
        self,
        record: CycleRecord,
        action: ActionRecord,
        *,
        reducing: bool,
    ) -> CycleRecord:
        kill = self._kill()
        if kill is None or (reducing and kill.get("kill_mode") == "KILL_FLATTEN"):
            return record
        self.store.mark_action(action.action_id, "NOT_BROADCAST")
        # Before an entry/approval broadcast the account is still flat, so the
        # safest result is a terminal local abort.  Risk-reducing actions keep
        # a resumable PAUSED state because a position/order may remain live.
        target = CycleState.PAUSED if reducing else CycleState.ABORTED
        if reducing:
            raw_from_state = record.runtime.get("write_from_state")
            try:
                from_state = CycleState(str(raw_from_state))
            except ValueError as exc:
                raise WorkflowError(
                    "Risk-reducing action has no valid pre-write state"
                ) from exc
            resume_state = self._reducing_resume_state(
                kind=action.kind,
                from_state=from_state,
            ).value
        else:
            resume_state = record.state.value
        return self.store.transition(
            record,
            target,
            runtime_updates={"resume_state": resume_state},
            error="Kill switch prevented transaction broadcast",
        )

    def _submit(
        self,
        record: CycleRecord,
        *,
        prepared: PreparedWrite,
        attempt: int,
        pending_state: CycleState,
        success_state: CycleState,
        reducing: bool,
        runtime_updates: dict[str, Any] | None = None,
    ) -> CycleRecord:
        if self.lease is None:
            raise StoreError("Execution lease is not held")
        self.store.assert_lease(self.lease)
        if self._signing_is_frozen() or (not reducing and self._entry_is_forbidden()):
            target = CycleState.PAUSED if reducing else CycleState.ABORTED
            pause_updates = None
            if reducing:
                resume_state = self._reducing_resume_state(
                    kind=prepared.kind,
                    from_state=record.state,
                )
                pause_updates = {"resume_state": resume_state.value}
            return self.store.transition(
                record,
                target,
                runtime_updates=pause_updates,
                error="Kill switch prevented signing",
            )
        record, action = self.store.arm_action(
            record,
            kind=prepared.kind,
            attempt=attempt,
            nonce=prepared.nonce,
            calldata_hash=prepared.calldata_hash,
            pending_state=pending_state,
            runtime_updates=runtime_updates,
        )
        self.store.assert_lease(self.lease)
        signed = self.chain.sign(prepared)
        # Deployment verification inside sign() performs several RPC reads and
        # can outlive a degraded-node lease.  A signature alone changes no
        # chain state; re-check fencing before persisting or broadcasting it.
        self.store.assert_lease(self.lease)
        action = self.store.record_signed_transaction(
            action, signed.tx_hash, signed.raw_transaction
        )
        stopped = self._abort_unbroadcast(record, action, reducing=reducing)
        if stopped.state != record.state:
            return stopped
        self._heartbeat()
        try:
            self.chain.broadcast(signed)
            self.store.mark_action(action.action_id, "BROADCAST")
            receipt = self.chain.wait_for_receipt(
                signed.tx_hash, on_poll=self._heartbeat
            )
        except ReceiptReverted as exc:
            self.store.mark_action(action.action_id, "REVERTED")
            if prepared.kind == "cancel_entry":
                return self.store.transition(record, CycleState.POSITION_VERIFY)
            if prepared.kind == "cancel_exit":
                return self.store.transition(
                    record, CycleState.RESIDUAL_RECONCILE
                )
            return self._quarantine(record, str(exc))
        except (BroadcastUnknown, OnchainError) as exc:
            self.store.mark_action(action.action_id, "UNKNOWN")
            return self.store.transition(
                record,
                CycleState.TX_UNKNOWN,
                runtime_updates={
                    "action_id": action.action_id,
                    **({"exit_attempts": attempt} if prepared.kind == "exit" else {}),
                },
                error=str(exc)[:240],
            )
        self.store.mark_action(
            action.action_id, "CONFIRMED", receipt_block=receipt.block_number
        )
        updates: dict[str, Any] = {
            "last_tx_hash": signed.tx_hash,
            "last_receipt_block": receipt.block_number,
        }
        if prepared.kind == "entry":
            updates["entry_deadline"] = self.wall_clock() + TradeCycleIntent.from_dict(record.intent).entry_ttl_seconds
            updates["entry_last_progress_at"] = self.wall_clock()
        if prepared.kind == "exit":
            updates["exit_deadline"] = self.wall_clock() + TradeCycleIntent.from_dict(record.intent).exit_ttl_seconds
            updates["exit_last_progress_at"] = self.wall_clock()
            updates["exit_attempts"] = attempt
        return self.store.transition(
            record, success_state, runtime_updates=updates
        )

    def _funds_check(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        snapshot = self.chain.verify_deployment()
        try:
            self._cross_check_onchain_market(record, intent)
        except (WorkflowError, OnchainError) as exc:
            return self._quarantine(record, str(exc))
        margin_units = int(record.runtime["margin_units"])
        if snapshot.collateral_balance_units < margin_units:
            have_usdc = Decimal(snapshot.collateral_balance_units) / Decimal(10**6)
            need_usdc = Decimal(margin_units) / Decimal(10**6)
            return self.store.transition(
                record,
                CycleState.BLOCKED_FUNDS,
                error=(
                    "Недостаточно test USDC: "
                    f"баланс {format(have_usdc.normalize(), 'f')} · "
                    f"нужно {format(need_usdc.normalize(), 'f')}"
                ),
            )
        if snapshot.eth_balance_wei <= 0:
            return self.store.transition(
                record,
                CycleState.BLOCKED_FUNDS,
                error=(
                    "Баланс Arbitrum Sepolia ETH равен 0 · пополни gas и снова "
                    "выбери «Режим работы»"
                ),
            )
        if snapshot.allowance_units < margin_units:
            if not intent.allow_approval:
                return self.store.transition(
                    record,
                    CycleState.BLOCKED_FUNDS,
                    error="Insufficient allowance; rerun with explicit --allow-approval",
                )
            prepared = self.chain.build_approval(margin_units)
            approval_reserve = self._write_cost(prepared) * ENTRY_GAS_RESERVE_WRITES
            if snapshot.eth_balance_wei < approval_reserve:
                shortfall = approval_reserve - snapshot.eth_balance_wei
                return self.store.transition(
                    record,
                    CycleState.BLOCKED_FUNDS,
                    runtime_updates={
                        "gas_balance_wei": snapshot.eth_balance_wei,
                        "gas_required_wei": approval_reserve,
                        "gas_shortfall_wei": shortfall,
                        "approval_gas_limit": int(prepared.transaction["gas"]),
                        "max_fee_per_gas_wei": int(
                            prepared.transaction["maxFeePerGas"]
                        ),
                        "gas_reserve_writes": ENTRY_GAS_RESERVE_WRITES,
                    },
                    error=(
                        "Недостаточно Arbitrum Sepolia ETH даже для безопасного "
                        f"APPROVE: баланс {format_eth_wei(snapshot.eth_balance_wei)} ETH · "
                        f"нужно ≥ {format_eth_wei(approval_reserve)} ETH · "
                        f"не хватает {format_eth_wei(shortfall)} ETH."
                    ),
                )
            return self._submit(
                record,
                prepared=prepared,
                attempt=1,
                pending_state=CycleState.APPROVAL_PENDING,
                success_state=CycleState.FUNDS_CHECK,
                reducing=False,
            )
        return self.store.transition(
            record,
            CycleState.ENTRY_ARMED,
            runtime_updates={
                "preflight_block": snapshot.block_number,
                "eth_balance_wei": snapshot.eth_balance_wei,
                "collateral_balance_units": snapshot.collateral_balance_units,
                "allowance_units": snapshot.allowance_units,
            },
        )

    def _entry_armed(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        if self._entry_is_forbidden():
            return self.store.transition(
                record, CycleState.ABORTED, error="Kill switch blocked entry"
            )
        remote = self._remote()
        if remote.active_orders or remote.positions:
            return self._quarantine(
                record, "Remote account changed after preflight; entry refused"
            )
        baseline = self._baseline(remote.orders, intent.market_id, False)
        if baseline != int(record.runtime["baseline_entry_id"]):
            return self._quarantine(record, "Order history changed after preflight")
        prepared = self.chain.build_open(
            market_id=int(intent.market_id),
            margin_units=int(record.runtime["margin_units"]),
            notional_units=int(record.runtime["notional_units"]),
            is_long=intent.side == "long",
            limit_price_units=int(record.runtime["limit_price_units"]),
        )
        available_eth = int(record.runtime.get("eth_balance_wei", 0))
        required_eth = entry_gas_reserve_wei(prepared)
        if available_eth < required_eth:
            shortfall = required_eth - available_eth
            return self.store.transition(
                record,
                CycleState.BLOCKED_FUNDS,
                runtime_updates={
                    "gas_balance_wei": available_eth,
                    "gas_required_wei": required_eth,
                    "gas_shortfall_wei": shortfall,
                    "entry_gas_limit": int(prepared.transaction["gas"]),
                    "max_fee_per_gas_wei": int(
                        prepared.transaction["maxFeePerGas"]
                    ),
                    "gas_reserve_writes": ENTRY_GAS_RESERVE_WRITES,
                },
                error=(
                    "Недостаточно Arbitrum Sepolia ETH для безопасного цикла: "
                    f"баланс {format_eth_wei(available_eth)} ETH · "
                    f"нужно ≥ {format_eth_wei(required_eth)} ETH · "
                    f"не хватает {format_eth_wei(shortfall)} ETH. "
                    "Резерв = 2 × максимальная оценка OPEN (запас на CANCEL/CLOSE). "
                    "Пополни ETH и снова выбери «Режим работы» — цикл продолжится автоматически."
                ),
            )
        return self._submit(
            record,
            prepared=prepared,
            attempt=1,
            pending_state=CycleState.ENTRY_TX_PENDING,
            success_state=CycleState.ENTRY_DISCOVERY,
            reducing=False,
        )

    def _entry_fingerprint(
        self, order: OrderView, record: CycleRecord, intent: TradeCycleIntent
    ) -> bool:
        if (
            order.reduce_only
            or order.owner != self.account
            or order.market_id != intent.market_id
            or order.side != intent.side.upper()
            or order.order_id <= int(record.runtime["baseline_entry_id"])
        ):
            return False
        tick = int(record.runtime["tick_decimals"])
        try:
            threshold_units = to_base_units(order.threshold_price, tick, bits=32)
            margin_units = to_base_units(order.initial_margin, 6, bits=48)
            notional_units = to_base_units(order.initial_notional, 6, bits=48)
        except Exception:
            return False
        if threshold_units != int(record.runtime["limit_price_units"]):
            return False
        # Exact match preferred. Fee is taken from margin before notional is
        # finalized, so API initialMargin/Notional often differ slightly from
        # the units we deposited — accept a small haircut instead of quarantining
        # a confirmed fill.
        want_margin = int(record.runtime["margin_units"])
        want_notional = int(record.runtime["notional_units"])
        if margin_units == want_margin and notional_units == want_notional:
            return True
        # Up to 2% fee/rounding drift on either field (testnet volume path).
        def _near(actual: int, expected: int) -> bool:
            if expected <= 0:
                return actual == expected
            drift = Decimal(abs(actual - expected)) / Decimal(expected)
            return drift <= Decimal("0.02")

        return _near(margin_units, want_margin) and _near(
            notional_units, want_notional
        )

    def _discover_entry(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        # Indexing lag on Fairground can exceed 45s after a confirmed entry,
        # especially when the order fully fills and drops out of GetOrders.
        deadline = self.wall_clock() + 90
        while self.wall_clock() < deadline:
            self._heartbeat()
            remote = self._remote()
            candidates = [
                order
                for order in remote.orders
                if self._entry_fingerprint(order, record, intent)
            ]
            if len(candidates) > 1:
                # Prefer the highest order id (newest) when fingerprints collide.
                candidates = sorted(candidates, key=lambda item: item.order_id)
                if all(
                    order.market_id == candidates[-1].market_id
                    and order.side == candidates[-1].side
                    for order in candidates
                ):
                    candidates = [candidates[-1]]
                else:
                    return self._quarantine(record, "Entry discovery was ambiguous")
            if len(candidates) == 1:
                order = candidates[0]
                if order.terminal:
                    target = CycleState.POSITION_VERIFY
                elif order.partial:
                    target = CycleState.ENTRY_PARTIAL
                else:
                    target = CycleState.ENTRY_PENDING
                return self.store.transition(
                    record,
                    target,
                    runtime_updates={
                        "entry_order_id": order.order_id,
                        "entry_filled": str(order.filled_size),
                        "entry_last_progress_at": self.wall_clock(),
                    },
                )
            # Fast path: order already gone from book after full fill, but the
            # position projection is already live. Continue the cycle.
            try:
                position = self._matching_position(remote, intent)
            except WorkflowError as exc:
                return self._quarantine(record, str(exc))
            if position is not None:
                return self.store.transition(
                    record,
                    CycleState.HOLDING,
                    runtime_updates={
                        "position_id": position.position_id,
                        "position_size": str(position.nominal_size),
                        "hold_deadline": self.wall_clock() + intent.hold_seconds,
                        "entry_filled": str(position.nominal_size),
                        "entry_last_progress_at": self.wall_clock(),
                    },
                )
            self.sleep(intent.poll_seconds)
        # Last chance: still no order row, but maybe position indexed late.
        try:
            remote = self._remote()
            position = self._matching_position(remote, intent)
        except (WorkflowError, FairgroundAPIError) as exc:
            return self._quarantine(record, str(exc))
        if position is not None:
            return self.store.transition(
                record,
                CycleState.HOLDING,
                runtime_updates={
                    "position_id": position.position_id,
                    "position_size": str(position.nominal_size),
                    "hold_deadline": self.wall_clock() + intent.hold_seconds,
                    "entry_filled": str(position.nominal_size),
                    "entry_last_progress_at": self.wall_clock(),
                },
            )
        # Confirmed entry with zero residual exposure → treat as empty fill and
        # wind down instead of permanent quarantine.
        return self.store.transition(
            record,
            CycleState.POSITION_VERIFY,
            runtime_updates={
                "entry_last_progress_at": self.wall_clock(),
                "post_entry_flat_observed_at": self.wall_clock(),
            },
            error="Entry indexed without a durable order row; verifying position/flat",
        )

    def _find_order(
        self,
        remote: RemoteSnapshot,
        market_id: str,
        order_id: int,
        *,
        reduce_only: bool,
    ) -> OrderView | None:
        matches = [
            order
            for order in remote.orders
            if order.owner == self.account
            and order.market_id == market_id
            and order.order_id == order_id
            and order.reduce_only is reduce_only
        ]
        if len(matches) > 1:
            raise WorkflowError("Duplicate composite order identity")
        return matches[0] if matches else None

    def _unexpected_active(
        self,
        remote: RemoteSnapshot,
        *,
        expected_market_id: str,
        expected_id: int | None,
        reduce_only: bool,
    ) -> bool:
        return any(
            order.active
            and not (
                expected_id is not None
                and order.owner == self.account
                and order.market_id == expected_market_id
                and order.order_id == expected_id
                and order.reduce_only is reduce_only
            )
            for order in remote.orders
        )

    def _next_action_attempt(
        self,
        record: CycleRecord,
        *,
        kind: str,
        floor: object,
    ) -> int:
        """Allocate the next same-kind journal identity without reusing a slot."""

        try:
            minimum = int(floor)
        except (TypeError, ValueError) as exc:
            raise WorkflowNeedsReconciliation(
                f"Persisted {kind} attempt floor is invalid"
            ) from exc
        if minimum <= 0:
            raise WorkflowNeedsReconciliation(
                f"Persisted {kind} attempt floor is invalid"
            )

        highest = 0
        terminal = {"CONFIRMED", "REVERTED", "NOT_BROADCAST"}
        for action in self.store.actions_for_cycle(record.cycle_id):
            if action.kind != kind:
                continue
            if action.attempt <= 0:
                raise WorkflowNeedsReconciliation(
                    f"Persisted {kind} action has an invalid attempt number"
                )
            if action.status not in terminal:
                raise WorkflowNeedsReconciliation(
                    f"Earlier {kind} action is {action.status}; retry refused"
                )
            if action.status == "NOT_BROADCAST":
                # Idempotently scrub legacy hash/raw slots before an identical
                # unbroadcast transaction may be rebuilt and signed again.
                self.store.mark_action(action.action_id, "NOT_BROADCAST")
            highest = max(highest, action.attempt)
        if self.store.account_has_unresolved_actions(record.account):
            raise WorkflowNeedsReconciliation(
                "Another account transaction is unresolved; retry refused"
            )
        return max(minimum, highest + 1)

    def _cancel_entry(
        self, record: CycleRecord, intent: TradeCycleIntent, order: OrderView
    ) -> CycleRecord:
        if self._signing_is_frozen():
            raise WorkflowNeedsReconciliation(
                "FREEZE_SIGNER is active; entry remains under read-only monitoring"
            )
        try:
            prepared = self.chain.build_cancel(
                market_id=int(intent.market_id),
                order_id=order.order_id,
                reduce_only=False,
            )
        except SimulationError:
            # The order may have filled between the read and simulation.  Never
            # retry blindly; re-read position/order in POSITION_VERIFY.
            return self.store.transition(record, CycleState.POSITION_VERIFY)
        paused = self._pause_for_reducing_gas(
            record,
            prepared,
            resume_state=record.state,
        )
        if paused is not None:
            return paused
        attempt = self._next_action_attempt(
            record,
            kind="cancel_entry",
            floor=1,
        )
        return self._submit(
            record,
            prepared=prepared,
            attempt=attempt,
            pending_state=CycleState.ENTRY_CANCEL_PENDING,
            success_state=CycleState.POSITION_VERIFY,
            reducing=True,
        )

    def _monitor_entry(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        order_id = int(record.runtime["entry_order_id"])
        last_filled = Decimal(str(record.runtime.get("entry_filled", "0")))
        last_progress = float(
            record.runtime.get("entry_last_progress_at", self.wall_clock())
        )
        while True:
            self._heartbeat()
            remote = self._remote()
            if self._unexpected_active(
                remote,
                expected_market_id=intent.market_id,
                expected_id=order_id,
                reduce_only=False,
            ):
                return self._quarantine(record, "Unexpected active order appeared")
            order = self._find_order(
                remote, intent.market_id, order_id, reduce_only=False
            )
            if order is None:
                raise WorkflowNeedsReconciliation("Entry order temporarily missing from API")
            if order.filled_size < last_filled:
                raise WorkflowNeedsReconciliation("Entry filled size regressed in API snapshot")
            if order.filled_size > last_filled:
                last_filled = order.filled_size
                last_progress = self.wall_clock()
            if order.terminal:
                return self.store.transition(
                    record,
                    CycleState.POSITION_VERIFY,
                    runtime_updates={"entry_filled": str(last_filled)},
                )
            if order.partial and record.state == CycleState.ENTRY_PENDING:
                record = self.store.transition(
                    record,
                    CycleState.ENTRY_PARTIAL,
                    runtime_updates={
                        "entry_filled": str(last_filled),
                        "entry_last_progress_at": last_progress,
                    },
                )
            kill = self._kill()
            expired = self.wall_clock() >= float(record.runtime["entry_deadline"])
            no_progress = order.partial and (
                self.wall_clock() - last_progress >= intent.partial_ttl_seconds
            )
            if expired or no_progress or (
                kill is not None and kill.get("kill_mode") == "KILL_FLATTEN"
            ):
                return self._cancel_entry(record, intent, order)
            if kill is not None and kill.get("kill_mode") == "FREEZE_SIGNER":
                raise WorkflowNeedsReconciliation(
                    "FREEZE_SIGNER is active; no cancel transaction will be signed"
                )
            self.sleep(intent.poll_seconds)

    def _matching_position(
        self, remote: RemoteSnapshot, intent: TradeCycleIntent
    ) -> PositionView | None:
        if len(remote.positions) > 1:
            raise WorkflowError("More than one open position exists")
        if not remote.positions:
            return None
        position = remote.positions[0]
        if position.market_id != intent.market_id or position.side != intent.side.upper():
            # Kill-flatten / orphan rehydrate: close the only live position even
            # when the durable intent market/side drifted after a crash.
            if self._is_kill_flatten() and len(remote.positions) == 1:
                return position
            raise WorkflowError("Open position does not match the cycle intent")
        return position

    def _position_verify(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        deadline = self.wall_clock() + POSITION_VERIFY_TIMEOUT_SECONDS
        order_id = int(record.runtime.get("entry_order_id", 0)) or None
        flat_candidate_since: float | None = None
        while self.wall_clock() < deadline:
            self._heartbeat()
            remote = self._remote()
            if self._unexpected_active(
                remote,
                expected_market_id=intent.market_id,
                expected_id=order_id,
                reduce_only=False,
            ):
                return self._quarantine(record, "Unexpected active order during position verification")
            expected = (
                self._find_order(
                    remote, intent.market_id, order_id, reduce_only=False
                )
                if order_id is not None
                else None
            )
            if expected is not None and expected.active:
                flat_candidate_since = None
                self.sleep(intent.poll_seconds)
                continue
            try:
                position = self._matching_position(remote, intent)
            except WorkflowError as exc:
                return self._quarantine(record, str(exc))
            if position is None:
                now = self.wall_clock()
                if flat_candidate_since is None:
                    flat_candidate_since = now
                if now - flat_candidate_since < POST_ENTRY_INDEXING_GRACE_SECONDS:
                    self.sleep(intent.poll_seconds)
                    continue
                return self.store.transition(
                    record,
                    CycleState.FLAT_CONFIRM,
                    runtime_updates={
                        "post_entry_flat_observed_at": flat_candidate_since,
                        "post_entry_indexing_grace_seconds": POST_ENTRY_INDEXING_GRACE_SECONDS,
                    },
                )
            return self.store.transition(
                record,
                CycleState.HOLDING,
                runtime_updates={
                    "position_id": position.position_id,
                    "position_size": str(position.nominal_size),
                    "hold_deadline": self.wall_clock() + intent.hold_seconds,
                },
            )
        raise WorkflowNeedsReconciliation("Entry cancellation/fill has not indexed to terminal state")

    def _stale_hold_orders(
        self, remote: RemoteSnapshot, record: CycleRecord, intent: TradeCycleIntent
    ) -> tuple[OrderView, ...]:
        """Orders that are not just the filled entry still sitting in GetOrders."""

        try:
            entry_id = int(record.runtime.get("entry_order_id") or 0)
        except (TypeError, ValueError):
            entry_id = 0
        want_side = intent.side.upper()
        stray: list[OrderView] = []
        for order in remote.active_orders:
            if order.terminal:
                continue
            if entry_id and order.order_id == entry_id:
                continue
            if (
                not entry_id
                and not order.reduce_only
                and order.market_id == intent.market_id
                and order.side == want_side
            ):
                continue
            stray.append(order)
        return tuple(stray)

    def _holding(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        stray_since: float | None = None
        while True:
            self._heartbeat()
            remote = self._remote()
            stray = self._stale_hold_orders(remote, record, intent)
            if stray:
                if stray_since is None:
                    stray_since = self.wall_clock()
                if self.wall_clock() - stray_since < REMOTE_INDEX_GRACE_SECONDS:
                    self.sleep(intent.poll_seconds)
                    continue
                return self._quarantine(record, "Unexpected active order while holding")
            stray_since = None
            try:
                position = self._matching_position(remote, intent)
            except WorkflowError as exc:
                return self._quarantine(record, str(exc))
            if position is None:
                return self.store.transition(record, CycleState.FLAT_CONFIRM)
            kill = self._kill()
            if kill is not None and kill.get("kill_mode") == "FREEZE_SIGNER":
                return self.store.transition(
                    record,
                    CycleState.PAUSED,
                    runtime_updates={"resume_state": CycleState.HOLDING.value},
                    error="FREEZE_SIGNER is active; position is not being modified",
                )
            if kill is not None or self.wall_clock() >= float(record.runtime["hold_deadline"]):
                return self.store.transition(
                    record,
                    CycleState.EXIT_ARMED,
                    runtime_updates={
                        "position_id": position.position_id,
                        "position_size": str(position.nominal_size),
                    },
                )
            self.sleep(intent.poll_seconds)

    def _durable_exit_attempts(self, record: CycleRecord) -> int:
        """Return consumed reduce attempts from the journal, not runtime alone.

        ``runtime.exit_attempts`` is only a cache and legacy cycles can lag the
        action journal after a mined revert/crash.  The unique action rows are
        the durable allocation record, so a retry must advance past their
        highest attempt instead of trying to insert the same identity again.
        """

        try:
            runtime_attempts = int(record.runtime.get("exit_attempts", 0))
        except (TypeError, ValueError) as exc:
            raise WorkflowNeedsReconciliation(
                "Persisted exit attempt counter is invalid"
            ) from exc
        if runtime_attempts < 0:
            raise WorkflowNeedsReconciliation(
                "Persisted exit attempt counter is invalid"
            )
        if self.store.account_has_unresolved_actions(record.account):
            raise WorkflowNeedsReconciliation(
                "An earlier transaction is unresolved; refusing a new reduce"
            )

        durable_attempts = runtime_attempts
        for action in self.store.actions_for_cycle(record.cycle_id):
            if action.kind != "exit":
                continue
            if action.attempt <= 0:
                raise WorkflowNeedsReconciliation(
                    "Persisted reduce action has an invalid attempt number"
                )
            if action.status not in {"CONFIRMED", "REVERTED", "NOT_BROADCAST"}:
                raise WorkflowNeedsReconciliation(
                    "Earlier reduce action is not terminal; retry refused"
                )
            if action.status == "NOT_BROADCAST":
                self.store.mark_action(action.action_id, "NOT_BROADCAST")
            durable_attempts = max(durable_attempts, action.attempt)
        return durable_attempts

    def _exit_armed(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        if self._signing_is_frozen():
            return self.store.transition(
                record,
                CycleState.PAUSED,
                runtime_updates={"resume_state": CycleState.EXIT_ARMED.value},
                error="FREEZE_SIGNER prevented close signing",
            )
        remote = self._remote()
        if remote.active_orders:
            return self._quarantine(record, "An active order exists before reduce-only close")
        try:
            position = self._matching_position(remote, intent)
        except WorkflowError as exc:
            return self._quarantine(record, str(exc))
        if position is None:
            return self.store.transition(record, CycleState.FLAT_CONFIRM)
        attempts = self._durable_exit_attempts(record)
        ceiling = self._force_exit_attempt_ceiling(intent)
        if attempts >= ceiling:
            if self._is_kill_flatten():
                # Last-resort free: only if API no longer shows a position.
                remote_now = self._remote()
                if not remote_now.positions and not remote_now.active_orders:
                    return self.store.transition(
                        record,
                        CycleState.COMPLETE,
                        error="Force-close: flat after reduce budget",
                    )
            return self.store.transition(
                record,
                CycleState.DUST_BLOCKED,
                error="Bounded reduce-only exit attempts are exhausted",
            )
        # Prefer live position market under force-close (intent may be stale).
        reduce_market_id = (
            position.market_id if self._is_kill_flatten() else intent.market_id
        )
        try:
            if reduce_market_id == intent.market_id and not self._is_kill_flatten():
                chain_market = self._cross_check_onchain_market(record, intent)
            else:
                chain_market = self.chain.get_market_config(int(reduce_market_id))
        except (WorkflowError, OnchainError) as exc:
            return self._quarantine(record, str(exc))
        size_decimals = int(
            record.runtime.get("size_decimals")
            or getattr(chain_market, "size_decimals", 0)
            or 0
        )
        if size_decimals < 0 or size_decimals > 18:
            size_decimals = int(getattr(chain_market, "size_decimals", 0) or 0)
        size_units = to_base_units(position.nominal_size, size_decimals, bits=56)
        lot = int(getattr(chain_market, "lot_size_raw", 0) or 0)
        if size_units <= 0 or (lot > 0 and size_units < lot):
            if self._is_kill_flatten():
                return self.store.transition(
                    record,
                    CycleState.COMPLETE,
                    error="Force-close: residual below lot size accepted as complete",
                )
            return self.store.transition(
                record,
                CycleState.DUST_BLOCKED,
                error="Live residual position is below the on-chain lot size",
            )
        next_attempt = attempts + 1
        baseline = self._baseline(remote.orders, reduce_market_id, True)
        try:
            # HAR: UI Market close = MANUAL orderType=0 + threshold ±2% of mark.
            slip_bps = max(200, int(intent.exit_slippage_bps))
            if self._is_kill_flatten():
                slip_bps = max(slip_bps, 300)

            class _ExitIntent:
                market_id = reduce_market_id
                side = (
                    position.side.lower()
                    if self._is_kill_flatten()
                    else intent.side
                )
                exit_slippage_bps = slip_bps

            tick = int(
                record.runtime.get("tick_decimals")
                or getattr(chain_market, "tick_decimals", 2)
                or 2
            )
            oracle_price, exit_threshold_units, oracle_timestamp = (
                self._exit_threshold(_ExitIntent(), tick)  # type: ignore[arg-type]
            )
            prepared = self.chain.build_reduce(
                market_id=int(reduce_market_id),
                position_id=position.position_id,
                reduce_size_units=size_units,
                limit_price_units=exit_threshold_units,
                order_type=0,  # HAR: MANUAL market-band (not enum MARKET=1)
            )
        except (SimulationError, FairgroundAPIError, WorkflowError, OnchainError) as exc:
            if self._is_kill_flatten():
                # Keep trying with next attempt counter via residual, not hard stop.
                return self.store.transition(
                    record,
                    CycleState.RESIDUAL_RECONCILE,
                    runtime_updates={
                        "exit_attempts": next_attempt,
                        "position_id": position.position_id,
                        "position_size": str(position.nominal_size),
                        "size_decimals": size_decimals,
                    },
                    error=f"Force-close reduce failed: {exc}"[:240],
                )
            return self._quarantine(record, str(exc))
        paused = self._pause_for_reducing_gas(
            record,
            prepared,
            resume_state=CycleState.EXIT_ARMED,
        )
        if paused is not None:
            return paused
        return self._submit(
            record,
            prepared=prepared,
            attempt=next_attempt,
            pending_state=CycleState.EXIT_TX_PENDING,
            success_state=CycleState.EXIT_DISCOVERY,
            reducing=True,
            runtime_updates={
                # Persist the allocation atomically with the PREPARED action;
                # a revert/crash before receipt must not make this slot reusable.
                "exit_attempts": next_attempt,
                "baseline_exit_id": baseline,
                "position_id": position.position_id,
                "position_size": str(position.nominal_size),
                "reduce_size_units": size_units,
                "exit_oracle_price": str(oracle_price),
                "exit_oracle_timestamp": oracle_timestamp,
                "exit_threshold_units": exit_threshold_units,
            },
        )

    def _exit_fingerprint(
        self, order: OrderView, record: CycleRecord, intent: TradeCycleIntent
    ) -> bool:
        if (
            not order.reduce_only
            or order.owner != self.account
            or order.market_id != intent.market_id
            or order.side != intent.side.upper()
            or order.order_id <= int(record.runtime["baseline_exit_id"])
        ):
            return False
        return to_base_units(
            order.size,
            int(record.runtime["size_decimals"]),
            bits=56,
        ) == int(record.runtime["reduce_size_units"])

    def _discover_exit(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        deadline = self.wall_clock() + 45
        while self.wall_clock() < deadline:
            self._heartbeat()
            remote = self._remote()
            if not remote.positions:
                return self.store.transition(record, CycleState.RESIDUAL_RECONCILE)
            candidates = [
                order
                for order in remote.orders
                if self._exit_fingerprint(order, record, intent)
            ]
            if len(candidates) > 1:
                return self._quarantine(record, "Reduce-order discovery was ambiguous")
            if len(candidates) == 1:
                order = candidates[0]
                if order.terminal:
                    target = CycleState.RESIDUAL_RECONCILE
                elif order.partial:
                    target = CycleState.EXIT_PARTIAL
                else:
                    target = CycleState.EXIT_PENDING
                return self.store.transition(
                    record,
                    target,
                    runtime_updates={
                        "exit_order_id": order.order_id,
                        "exit_filled": str(order.filled_size),
                        "exit_last_progress_at": self.wall_clock(),
                    },
                )
            self.sleep(intent.poll_seconds)
        return self._quarantine(
            record, "Confirmed reduce transaction was not uniquely indexed"
        )

    def _cancel_exit(
        self, record: CycleRecord, intent: TradeCycleIntent, order: OrderView
    ) -> CycleRecord:
        if self._signing_is_frozen():
            raise WorkflowNeedsReconciliation(
                "FREEZE_SIGNER is active; reduce order remains under monitoring"
            )
        try:
            prepared = self.chain.build_cancel(
                market_id=int(intent.market_id),
                order_id=order.order_id,
                reduce_only=True,
            )
        except SimulationError:
            return self.store.transition(record, CycleState.RESIDUAL_RECONCILE)
        paused = self._pause_for_reducing_gas(
            record,
            prepared,
            resume_state=record.state,
        )
        if paused is not None:
            return paused
        exit_floor = max(1, self._durable_exit_attempts(record))
        attempt = self._next_action_attempt(
            record,
            kind="cancel_exit",
            floor=exit_floor,
        )
        return self._submit(
            record,
            prepared=prepared,
            attempt=attempt,
            pending_state=CycleState.EXIT_CANCEL_PENDING,
            success_state=CycleState.RESIDUAL_RECONCILE,
            reducing=True,
        )

    def _monitor_exit(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        order_id = int(record.runtime["exit_order_id"])
        last_filled = Decimal(str(record.runtime.get("exit_filled", "0")))
        while True:
            self._heartbeat()
            remote = self._remote()
            if self._unexpected_active(
                remote,
                expected_market_id=intent.market_id,
                expected_id=order_id,
                reduce_only=True,
            ):
                return self._quarantine(record, "Unexpected active order during close")
            try:
                position = self._matching_position(remote, intent)
            except WorkflowError as exc:
                return self._quarantine(record, str(exc))
            if position is None:
                return self.store.transition(record, CycleState.RESIDUAL_RECONCILE)
            order = self._find_order(
                remote, intent.market_id, order_id, reduce_only=True
            )
            if order is None:
                raise WorkflowNeedsReconciliation("Reduce order temporarily missing from API")
            if order.filled_size < last_filled:
                raise WorkflowNeedsReconciliation("Reduce filled size regressed")
            if order.filled_size > last_filled:
                last_filled = order.filled_size
            if order.terminal:
                return self.store.transition(
                    record,
                    CycleState.RESIDUAL_RECONCILE,
                    runtime_updates={"exit_filled": str(last_filled)},
                )
            if order.partial and record.state == CycleState.EXIT_PENDING:
                record = self.store.transition(
                    record,
                    CycleState.EXIT_PARTIAL,
                    runtime_updates={"exit_filled": str(last_filled)},
                )
            if self.wall_clock() >= float(record.runtime["exit_deadline"]):
                return self._cancel_exit(record, intent, order)
            self.sleep(intent.poll_seconds)

    def _residual(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        deadline = self.wall_clock() + 60
        while self.wall_clock() < deadline:
            self._heartbeat()
            remote = self._remote()
            if remote.active_orders:
                self.sleep(intent.poll_seconds)
                continue
            try:
                position = self._matching_position(remote, intent)
            except WorkflowError as exc:
                return self._quarantine(record, str(exc))
            if position is None:
                return self.store.transition(record, CycleState.FLAT_CONFIRM)
            try:
                durable = self._durable_exit_attempts(record)
            except WorkflowNeedsReconciliation:
                durable = int(record.runtime.get("exit_attempts", 0) or 0)
            if durable >= self._force_exit_attempt_ceiling(intent):
                if self._is_kill_flatten() and not remote.positions:
                    return self.store.transition(
                        record,
                        CycleState.COMPLETE,
                        error="Force-close: flat after extra reduce budget",
                    )
                return self.store.transition(
                    record,
                    CycleState.DUST_BLOCKED,
                    error=(
                        "Residual remains after bounded reduce-only attempts"
                        + (
                            " · force-close: check size decimals / try UI market close 2%"
                            if self._is_kill_flatten()
                            else ""
                        )
                    ),
                )
            return self.store.transition(
                record,
                CycleState.EXIT_ARMED,
                runtime_updates={
                    "position_id": position.position_id,
                    "position_size": str(position.nominal_size),
                },
            )
        raise WorkflowNeedsReconciliation("Reduce cancellation has not indexed to terminal state")

    def _flat_confirm(self, record: CycleRecord, intent: TradeCycleIntent) -> CycleRecord:
        first_block: int | None = None
        confirmations = 0
        deadline = self.wall_clock() + 60
        while self.wall_clock() < deadline:
            self._heartbeat()
            remote = self._remote()
            if remote.active_orders or remote.positions:
                return self.store.transition(record, CycleState.RESIDUAL_RECONCILE)
            block = int(self.chain.current_block())
            if first_block is None:
                first_block = block
                confirmations = 1
            elif block >= first_block + 2:
                confirmations += 1
                if confirmations >= 2:
                    return self.store.transition(
                        record,
                        CycleState.COMPLETE,
                        runtime_updates={
                            "flat_confirmed_block": block,
                            "flat_confirmations": confirmations,
                        },
                    )
            self.sleep(intent.poll_seconds)
        raise WorkflowNeedsReconciliation("Flat state could not be confirmed across blocks")

    def _reconcile_reverted_action(
        self,
        record: CycleRecord,
        action: ActionRecord,
        error: ReceiptReverted,
    ) -> CycleRecord:
        self.store.mark_action(action.action_id, "REVERTED")
        # Cancellation can legitimately lose a race with the final fill.  The
        # receipt alone does not tell us whether exposure remains, so reconcile
        # the live order/position projection instead of quarantining it.
        if action.kind == "cancel_entry":
            return self.store.transition(record, CycleState.POSITION_VERIFY)
        if action.kind == "cancel_exit":
            return self.store.transition(record, CycleState.RESIDUAL_RECONCILE)
        return self._quarantine(record, str(error))

    def _reconcile_unknown(self, record: CycleRecord) -> CycleRecord:
        action_id = record.runtime.get("action_id")
        if not isinstance(action_id, str):
            return self._quarantine(record, "TX_UNKNOWN has no persisted action identity")
        action = self.store.get_action(action_id)
        if action is None:
            return self._quarantine(record, "Unknown transaction action is missing")
        expected_kind = record.runtime.get("expected_action_kind")
        if expected_kind != action.kind or action.cycle_id != record.cycle_id:
            return self._quarantine(record, "Unknown transaction action identity is stale")
        if action.status == "NOT_BROADCAST":
            if action.kind in {"approval", "entry"}:
                return self.store.transition(
                    record,
                    CycleState.ABORTED,
                    error="Prepared entry-side transaction was never broadcast",
                )
            resume_state = (
                CycleState.EXIT_ARMED
                if action.kind == "exit"
                else CycleState.POSITION_VERIFY
                if action.kind == "cancel_entry"
                else CycleState.RESIDUAL_RECONCILE
            )
            return self.store.transition(
                record,
                CycleState.PAUSED,
                runtime_updates={"resume_state": resume_state.value},
                error="Risk-reducing transaction was not broadcast; explicit resume required",
            )
        if action.tx_hash is None:
            return self._quarantine(record, "Unknown transaction has no persisted hash")
        try:
            receipt = self.chain.get_receipt(action.tx_hash)
        except ReceiptReverted as exc:
            return self._reconcile_reverted_action(record, action, exc)
        if receipt is None:
            if action.raw_transaction is None:
                raise WorkflowNeedsReconciliation(
                    f"Transaction {action.tx_hash} is unresolved and exact raw bytes are unavailable"
                )
            if self._signing_is_frozen() or (
                action.kind in {"approval", "entry"} and self._entry_is_forbidden()
            ):
                raise WorkflowNeedsReconciliation(
                    "Kill switch prevents exact transaction rebroadcast; hash remains unresolved"
                )
            signed = SignedWrite(
                tx_hash=action.tx_hash,
                raw_transaction=action.raw_transaction,
            )
            try:
                self.chain.broadcast(signed)
                self.store.mark_action(action.action_id, "BROADCAST")
            except BroadcastUnknown:
                self.store.mark_action(action.action_id, "UNKNOWN")
        # get_receipt only proves inclusion at the node's current head.  Apply
        # the same confirmation/reorg policy as the normal submission path even
        # when a preliminary receipt was already found above.
        try:
            receipt = self.chain.wait_for_receipt(
                action.tx_hash, on_poll=self._heartbeat
            )
        except ReceiptReverted as exc:
            return self._reconcile_reverted_action(record, action, exc)
        except (BroadcastUnknown, OnchainError) as exc:
            raise WorkflowNeedsReconciliation(
                f"Transaction {action.tx_hash} is still unresolved; exact hash was not replaced"
            ) from exc
        self.store.mark_action(
            action.action_id, "CONFIRMED", receipt_block=receipt.block_number
        )
        next_state = {
            "approval": CycleState.FUNDS_CHECK,
            "entry": CycleState.ENTRY_DISCOVERY,
            "cancel_entry": CycleState.POSITION_VERIFY,
            "exit": CycleState.EXIT_DISCOVERY,
            "cancel_exit": CycleState.RESIDUAL_RECONCILE,
        }.get(action.kind)
        if next_state is None:
            return self._quarantine(record, "Unknown transaction action kind")
        intent = TradeCycleIntent.from_dict(record.intent)
        updates: dict[str, Any] = {
            "last_tx_hash": action.tx_hash,
            "last_receipt_block": receipt.block_number,
        }
        if action.kind == "entry":
            updates.update(
                {
                    "entry_deadline": self.wall_clock() + intent.entry_ttl_seconds,
                    "entry_last_progress_at": self.wall_clock(),
                }
            )
        elif action.kind == "exit":
            updates.update(
                {
                    "exit_deadline": self.wall_clock() + intent.exit_ttl_seconds,
                    "exit_last_progress_at": self.wall_clock(),
                    "exit_attempts": action.attempt,
                }
            )
        return self.store.transition(record, next_state, runtime_updates=updates)

    def _drive(self, record: CycleRecord) -> CycleRecord:
        intent = TradeCycleIntent.from_dict(record.intent)
        while record.state not in {
            CycleState.COMPLETE,
            CycleState.ABORTED,
            CycleState.QUARANTINED,
            CycleState.BLOCKED_FUNDS,
            CycleState.DUST_BLOCKED,
            CycleState.PAUSED,
        }:
            if record.state == CycleState.PREFLIGHT:
                record = self._preflight(record, intent)
            elif record.state == CycleState.FUNDS_CHECK:
                record = self._funds_check(record, intent)
            elif record.state == CycleState.ENTRY_ARMED:
                record = self._entry_armed(record, intent)
            elif record.state == CycleState.ENTRY_DISCOVERY:
                record = self._discover_entry(record, intent)
            elif record.state in {CycleState.ENTRY_PENDING, CycleState.ENTRY_PARTIAL}:
                record = self._monitor_entry(record, intent)
            elif record.state == CycleState.POSITION_VERIFY:
                record = self._position_verify(record, intent)
            elif record.state == CycleState.HOLDING:
                record = self._holding(record, intent)
            elif record.state == CycleState.EXIT_ARMED:
                record = self._exit_armed(record, intent)
            elif record.state == CycleState.EXIT_DISCOVERY:
                record = self._discover_exit(record, intent)
            elif record.state in {CycleState.EXIT_PENDING, CycleState.EXIT_PARTIAL}:
                record = self._monitor_exit(record, intent)
            elif record.state == CycleState.RESIDUAL_RECONCILE:
                record = self._residual(record, intent)
            elif record.state == CycleState.FLAT_CONFIRM:
                record = self._flat_confirm(record, intent)
            elif record.state == CycleState.TX_UNKNOWN:
                record = self._reconcile_unknown(record)
            elif record.state in {
                CycleState.APPROVAL_PENDING,
                CycleState.ENTRY_TX_PENDING,
                CycleState.ENTRY_CANCEL_PENDING,
                CycleState.EXIT_TX_PENDING,
                CycleState.EXIT_CANCEL_PENDING,
            }:
                # A crash can leave PREPARED/SIGNED/BROADCAST state here.  Only
                # a persisted tx hash permits recovery; otherwise quarantine.
                action_id = record.runtime.get("action_id")
                action = (
                    self.store.get_action(action_id)
                    if isinstance(action_id, str)
                    else None
                )
                expected_by_state = {
                    CycleState.APPROVAL_PENDING: "approval",
                    CycleState.ENTRY_TX_PENDING: "entry",
                    CycleState.ENTRY_CANCEL_PENDING: "cancel_entry",
                    CycleState.EXIT_TX_PENDING: "exit",
                    CycleState.EXIT_CANCEL_PENDING: "cancel_exit",
                }
                expected_kind = expected_by_state[record.state]
                if (
                    action is None
                    or action.kind != expected_kind
                    or record.runtime.get("expected_action_kind") != expected_kind
                ):
                    record = self._quarantine(
                        record, "Pending state points to a stale or wrong action"
                    )
                elif action.status == "PREPARED" and action.tx_hash is None:
                    # Atomic arming proves this action was never signed or sent.
                    self.store.mark_action(action.action_id, "NOT_BROADCAST")
                    if action.kind in {"approval", "entry"}:
                        record = self.store.transition(
                            record,
                            CycleState.ABORTED,
                            error="Interrupted entry-side action was never broadcast",
                        )
                    else:
                        resume_state = (
                            CycleState.EXIT_ARMED
                            if action.kind == "exit"
                            else CycleState.POSITION_VERIFY
                            if action.kind == "cancel_entry"
                            else CycleState.RESIDUAL_RECONCILE
                        )
                        record = self.store.transition(
                            record,
                            CycleState.PAUSED,
                            runtime_updates={"resume_state": resume_state.value},
                            error="Interrupted risk-reducing action was never broadcast",
                        )
                elif action.tx_hash and (
                    action.raw_transaction
                    or action.status in {"CONFIRMED", "REVERTED"}
                ):
                    record = self.store.transition(record, CycleState.TX_UNKNOWN)
                else:
                    record = self._quarantine(
                        record, "Interrupted write lacks a complete signed transaction"
                    )
            else:
                record = self._quarantine(
                    record, f"Unhandled workflow state {record.state}"
                )
        return record

    def _rehydrate_quarantine_from_remote(
        self, record: CycleRecord
    ) -> CycleRecord | None:
        """If API still shows exposure, re-enter close path.

        Used when QUARANTINED has no CONFIRMED entry journal (crash / partial
        index) but the wallet is not flat — otherwise farm is permanently stuck.
        Under KILL_FLATTEN accepts any single live position (intent may be stale).
        """

        if record.state is not CycleState.QUARANTINED:
            return None
        try:
            intent = TradeCycleIntent.from_dict(record.intent)
            remote = self._remote()
        except (FairgroundAPIError, ValidationError, WorkflowError, KeyError, TypeError):
            return None
        strict_positions = [
            item
            for item in remote.positions
            if item.market_id == intent.market_id
            and item.side == intent.side.upper()
        ]
        positions = strict_positions
        if not positions and (self._is_kill_flatten() or len(remote.positions) == 1):
            positions = list(remote.positions)
        active_orders = [
            item
            for item in remote.active_orders
            if (
                item.market_id == intent.market_id
                or self._is_kill_flatten()
                or len(remote.active_orders) == 1
            )
            and not item.reduce_only
        ]
        if positions:
            position = positions[0]
            # Jump to EXIT_ARMED when force-closing; HOLDING for normal resume.
            target = (
                CycleState.EXIT_ARMED
                if self._is_kill_flatten()
                else CycleState.HOLDING
            )
            updates: dict[str, Any] = {
                "position_id": position.position_id,
                "position_size": str(position.nominal_size),
            }
            if target is CycleState.HOLDING:
                updates["hold_deadline"] = self.wall_clock()
            return self.store.transition(
                record,
                target,
                runtime_updates=updates,
                error="Rehydrated QUARANTINED from live position → close",
            )
        if active_orders:
            order = active_orders[0]
            return self.store.transition(
                record,
                CycleState.POSITION_VERIFY,
                runtime_updates={"entry_order_id": order.order_id},
                error="Rehydrated QUARANTINED from live entry order → index/cancel",
            )
        return None

    def start(self, intent: TradeCycleIntent) -> CycleRecord:
        self.lease = self.store.acquire_lease(self.account)
        try:
            record = self.store.create_cycle(
                account=self.account,
                chain_id=self.settings.chain_id,
                intent=intent.to_dict(),
            )
            return self._drive(record)
        finally:
            if self.lease is not None:
                self.store.release_lease(self.lease)
                self.lease = None

    def resume(self) -> CycleRecord:
        self.lease = self.store.acquire_lease(self.account)
        try:
            record = self.store.get_active_cycle(self.account)
            if record is None:
                raise WorkflowError("No active cycle exists for this account")
            if record.state == CycleState.BLOCKED_FUNDS:
                record = self.store.transition(record, CycleState.FUNDS_CHECK)
            elif record.state == CycleState.DUST_BLOCKED:
                if self._is_kill_flatten():
                    # Skip residual loop — go straight to another reduce wave.
                    remote = self._remote()
                    pos = remote.positions[0] if remote.positions else None
                    updates: dict[str, Any] = {}
                    if pos is not None:
                        updates = {
                            "position_id": pos.position_id,
                            "position_size": str(pos.nominal_size),
                        }
                    record = self.store.transition(
                        record,
                        CycleState.EXIT_ARMED,
                        runtime_updates=updates or None,
                        error="Force-close: DUST_BLOCKED → EXIT_ARMED",
                    )
                else:
                    record = self.store.transition(
                        record, CycleState.RESIDUAL_RECONCILE
                    )
            elif record.state == CycleState.PAUSED:
                if self._signing_is_frozen():
                    return record
                raw_resume = record.runtime.get("resume_state")
                try:
                    resume_state = CycleState(str(raw_resume))
                except ValueError as exc:
                    raise WorkflowError("Paused cycle has no valid resume state") from exc
                record = self.store.transition(record, resume_state)
            elif record.state == CycleState.QUARANTINED:
                # Re-enter indexing after a confirmed write instead of dead-ending.
                actions = self.store.actions_for_cycle(record.cycle_id)
                entry = next(
                    (
                        action
                        for action in actions
                        if action.kind == "entry" and action.status == "CONFIRMED"
                    ),
                    None,
                )
                exit_action = next(
                    (
                        action
                        for action in actions
                        if action.kind == "exit" and action.status == "CONFIRMED"
                    ),
                    None,
                )
                if exit_action is not None:
                    target = (
                        CycleState.EXIT_DISCOVERY
                        if not record.runtime.get("exit_order_id")
                        else CycleState.RESIDUAL_RECONCILE
                    )
                    record = self.store.transition(
                        record,
                        target,
                        error="Resume from QUARANTINED after confirmed reduce",
                    )
                elif entry is not None:
                    intent = TradeCycleIntent.from_dict(record.intent)
                    updates: dict[str, Any] = {}
                    if record.runtime.get("position_id"):
                        target = CycleState.HOLDING
                        if "hold_deadline" not in record.runtime:
                            updates["hold_deadline"] = (
                                self.wall_clock() + intent.hold_seconds
                            )
                    elif record.runtime.get("entry_order_id"):
                        target = CycleState.POSITION_VERIFY
                    else:
                        target = CycleState.ENTRY_DISCOVERY
                    record = self.store.transition(
                        record,
                        target,
                        runtime_updates=updates or None,
                        error="Resume from QUARANTINED after confirmed entry; re-index",
                    )
                else:
                    # Journal lost entry proof, but live exposure may still exist.
                    # Rehydrate into the close path so volume farm is not stuck.
                    rehydrated = self._rehydrate_quarantine_from_remote(record)
                    if rehydrated is None:
                        return record
                    record = rehydrated
            return self._drive(record)
        finally:
            if self.lease is not None:
                self.store.release_lease(self.lease)
                self.lease = None
