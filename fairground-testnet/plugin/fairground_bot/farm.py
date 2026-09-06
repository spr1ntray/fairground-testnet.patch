"""Multi-account open/close trade cycles on Fairground."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from random import choice, randint, shuffle, uniform
from threading import Lock, local
import time
from typing import Any, Callable

from .accounts import AccountError, FarmAccount
from .client import FairgroundAPIError, FairgroundClient
from .config import Settings
from .onchain import OnchainError, OnchainExecutor, SimulationError
from .proxy import ProxyError
from .safety import MarketLimits
from .identity import BrowserIdentity
from .preview import TradePreviewService
from .signer import SignerError, load_private_key_hex
from .timing import (
    SessionStyle,
    after_account_delay,
    between_rounds_delay,
    configure_delay,
    hold_seconds as session_hold_seconds,
    roll_session,
    sleep_jitter,
)
from .state import CycleRecord, CycleState, StateStore, StoreError
from .validation import (
    ValidationError,
    decimal_value,
    normalize_market_id,
)
from .waitlist import WaitlistAdmission, admission_from_chain_and_api
from .workflow import (
    CycleRunner,
    PREFLIGHT_VALIDATION_PREFIX,
    TradeCycleIntent,
    WorkflowError,
    WorkflowNeedsReconciliation,
    terminalize_invalid_preflight_without_chain,
)


MARGIN_QUANTUM = Decimal("0.000001")
LEVERAGE_QUANTUM = Decimal("0.01")
PERCENT_DENOMINATOR = Decimal("100")
PERCENT_DISPLAY_QUANTUM = Decimal("0.01")
MARGIN_MODES = frozenset({"FIXED", "PERCENT"})
LEVERAGE_LEVEL_FACTORS = {
    "MEDIUM": Decimal("0.50"),
    "HIGH": Decimal("0.75"),
    "MAX": Decimal("1"),
}


class FarmError(RuntimeError):
    pass


def _looks_like_not_waitlisted(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "usdc balance is zero" in text


def _tally_live_finish(results: list[AccountFarmResult]) -> tuple[int, int, int]:
    skipped = sum(1 for item in results if item.waitlisted is False)
    failed = sum(
        1
        for item in results
        if item.waitlisted is not False and not item.success
    )
    ok = len(results) - skipped - failed
    return ok, skipped, failed


@dataclass(frozen=True, slots=True)
class FarmConfig:
    """Trade-cycle parameters (ranges become random picks per wallet/round)."""

    market_id: str
    market_name: str
    trades_count: tuple[int, int] = (2, 4)
    margin: tuple[str, str] = ("5", "10")
    cheapest_margin: bool = False
    leverage: str = "1"
    hold_seconds: tuple[int, int] = (5, 20)
    threads: int = 2
    shuffle_wallets: bool = True
    allow_long: bool = True
    allow_short: bool = True
    entry_ttl_seconds: int = 120
    partial_ttl_seconds: int = 30
    exit_ttl_seconds: int = 60
    poll_seconds: int = 3
    max_exit_attempts: int = 2
    exit_slippage_bps: int = 50
    entry_offset_bps: int = 200  # market-style ±2% open threshold
    sleep_between_rounds: tuple[int, int] = (8, 20)
    sleep_after_account: tuple[int, int] = (10, 25)
    allow_approval: bool = True
    tick_decimals: int = -1
    # Empty selectors preserve the original fixed MARKET_ID / MARKET_NAME mode.
    # ("ALL",) discovers every currently usable Fairground market.  Other
    # entries may be live market names or uint64 IDs.
    markets: tuple[str, ...] = ()
    # Empty levels preserve the original fixed numeric ``leverage``.
    leverage_levels: tuple[str, ...] = ()
    margin_mode: str = "FIXED"
    margin_percent: tuple[str, str] = ("10", "20")

    def __post_init__(self) -> None:
        selectors = tuple(str(value).strip() for value in self.markets)
        if any(not value or len(value) > 80 for value in selectors):
            raise FarmError("market selectors must be non-empty names/IDs up to 80 characters")
        normalized_selectors = tuple(
            "ALL" if value.upper() == "ALL" else value for value in selectors
        )
        if "ALL" in normalized_selectors and normalized_selectors != ("ALL",):
            raise FarmError("ALL must be the only market selector")
        if len({value.upper() for value in normalized_selectors}) != len(
            normalized_selectors
        ):
            raise FarmError("market selectors must be unique")
        object.__setattr__(self, "markets", normalized_selectors)

        levels = tuple(str(value).strip().upper() for value in self.leverage_levels)
        unknown_levels = set(levels).difference(LEVERAGE_LEVEL_FACTORS)
        if unknown_levels:
            raise FarmError("leverage_levels may contain only MEDIUM, HIGH, MAX")
        if len(set(levels)) != len(levels):
            raise FarmError("leverage_levels must be unique")
        object.__setattr__(self, "leverage_levels", levels)

        margin_mode = str(self.margin_mode).strip().upper()
        if margin_mode not in MARGIN_MODES:
            raise FarmError("margin_mode must be FIXED or PERCENT")
        object.__setattr__(self, "margin_mode", margin_mode)

        # Legacy singleton fields are genuinely optional in dynamic mode.
        # Keeping stale fallback values in parameters.py must not block ALL or
        # an explicit live market list.
        if normalized_selectors:
            object.__setattr__(self, "market_id", str(self.market_id).strip())
            object.__setattr__(self, "market_name", str(self.market_name).strip())
        else:
            market_id = normalize_market_id(self.market_id)
            object.__setattr__(self, "market_id", market_id)
            name = self.market_name.strip()
            if not name or len(name) > 80:
                raise FarmError("market_name must be 1..80 characters")
            object.__setattr__(self, "market_name", name)

        t0, t1 = self.trades_count
        if not (1 <= t0 <= t1 <= 500):
            raise FarmError("trades_count must satisfy 1 <= min <= max <= 500")
        h0, h1 = self.hold_seconds
        if not (0 <= h0 <= h1 <= 86_400):
            raise FarmError("hold_seconds range is invalid")
        if not 1 <= self.threads <= 200:
            raise FarmError("threads must be between 1 and 200")
        if not (self.allow_long or self.allow_short):
            raise FarmError("Enable at least one of allow_long / allow_short")
        if not 0 <= self.entry_offset_bps <= 1000:
            raise FarmError("entry_offset_bps must be 0..1000 (market tolerance bps)")
        for label, pair in (
            ("sleep_between_rounds", self.sleep_between_rounds),
            ("sleep_after_account", self.sleep_after_account),
        ):
            a, b = pair
            if not (0 <= a <= b <= 3600):
                raise FarmError(f"{label} range is invalid")
        if not isinstance(self.margin, (list, tuple)) or len(self.margin) != 2:
            raise FarmError("margin must be [min, max]")
        normalized_margin = tuple(str(value) for value in self.margin)
        object.__setattr__(self, "margin", normalized_margin)
        if margin_mode == "FIXED":
            for value in normalized_margin:
                try:
                    parsed = Decimal(value)
                except Exception as exc:
                    raise FarmError(f"Invalid margin value: {value!r}") from exc
                if not parsed.is_finite() or parsed <= 0:
                    raise FarmError("margin values must be positive")

        object.__setattr__(self, "leverage", str(self.leverage))
        if not levels:
            try:
                lev = Decimal(self.leverage)
            except Exception as exc:
                raise FarmError("Invalid leverage") from exc
            if not lev.is_finite() or lev <= 0:
                raise FarmError("leverage must be positive")

        if not isinstance(self.margin_percent, (list, tuple)) or len(
            self.margin_percent
        ) != 2:
            raise FarmError("margin_percent must be [min, max]")
        normalized_percent = tuple(str(value) for value in self.margin_percent)
        object.__setattr__(self, "margin_percent", normalized_percent)
        if margin_mode != "PERCENT":
            return
        try:
            percent_low, percent_high = (
                Decimal(normalized_percent[0]),
                Decimal(normalized_percent[1]),
            )
        except Exception as exc:
            raise FarmError("Invalid margin_percent value") from exc
        if (
            not percent_low.is_finite()
            or not percent_high.is_finite()
            or percent_low <= 0
            or percent_low > percent_high
            or percent_high > PERCENT_DENOMINATOR
        ):
            raise FarmError("margin_percent must satisfy 0 < min <= max <= 100")
        object.__setattr__(
            self,
            "margin_percent",
            (_plain(percent_low), _plain(percent_high)),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FarmConfig":
        def pair_int(key: str, default: tuple[int, int]) -> tuple[int, int]:
            value = raw.get(key, list(default))
            if isinstance(value, (list, tuple)) and len(value) == 2:
                return int(value[0]), int(value[1])
            raise FarmError(f"{key} must be [min, max]")

        def pair_str(key: str, default: tuple[str, str]) -> tuple[str, str]:
            value = raw.get(key, list(default))
            if isinstance(value, (list, tuple)) and len(value) == 2:
                return str(value[0]), str(value[1])
            raise FarmError(f"{key} must be [min, max]")

        def tuple_str(key: str) -> tuple[str, ...]:
            value = raw.get(key, ())
            if value is None:
                return ()
            if isinstance(value, (list, tuple)):
                return tuple(str(item) for item in value)
            raise FarmError(f"{key} must be a list")

        return cls(
            market_id=str(raw.get("market_id") or raw.get("marketId") or ""),
            market_name=str(raw.get("market_name") or raw.get("marketName") or ""),
            trades_count=pair_int("trades_count", (2, 4)),
            margin=pair_str("margin", ("5", "10")),
            cheapest_margin=bool(raw.get("cheapest_margin", False)),
            leverage=str(raw.get("leverage", "1")),
            hold_seconds=pair_int("hold_seconds", (5, 20)),
            threads=int(raw.get("threads", 2)),
            shuffle_wallets=bool(raw.get("shuffle_wallets", True)),
            allow_long=bool(raw.get("allow_long", True)),
            allow_short=bool(raw.get("allow_short", True)),
            entry_ttl_seconds=int(raw.get("entry_ttl_seconds", 120)),
            partial_ttl_seconds=int(raw.get("partial_ttl_seconds", 30)),
            exit_ttl_seconds=int(raw.get("exit_ttl_seconds", 60)),
            poll_seconds=int(raw.get("poll_seconds", 3)),
            max_exit_attempts=int(raw.get("max_exit_attempts", 2)),
            exit_slippage_bps=int(raw.get("exit_slippage_bps", 50)),
            entry_offset_bps=int(raw.get("entry_offset_bps", 5)),
            sleep_between_rounds=pair_int("sleep_between_rounds", (8, 20)),
            sleep_after_account=pair_int("sleep_after_account", (10, 25)),
            allow_approval=bool(raw.get("allow_approval", True)),
            tick_decimals=int(raw.get("tick_decimals", -1)),
            markets=tuple_str("markets"),
            leverage_levels=tuple_str("leverage_levels"),
            margin_mode=str(raw.get("margin_mode", "FIXED")),
            margin_percent=pair_str("margin_percent", ("10", "20")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "market_name": self.market_name,
            "trades_count": list(self.trades_count),
            "margin": list(self.margin),
            "cheapest_margin": self.cheapest_margin,
            "leverage": self.leverage,
            "hold_seconds": list(self.hold_seconds),
            "threads": self.threads,
            "shuffle_wallets": self.shuffle_wallets,
            "allow_long": self.allow_long,
            "allow_short": self.allow_short,
            "entry_ttl_seconds": self.entry_ttl_seconds,
            "partial_ttl_seconds": self.partial_ttl_seconds,
            "exit_ttl_seconds": self.exit_ttl_seconds,
            "poll_seconds": self.poll_seconds,
            "max_exit_attempts": self.max_exit_attempts,
            "exit_slippage_bps": self.exit_slippage_bps,
            "entry_offset_bps": self.entry_offset_bps,
            "sleep_between_rounds": list(self.sleep_between_rounds),
            "sleep_after_account": list(self.sleep_after_account),
            "allow_approval": self.allow_approval,
            "tick_decimals": self.tick_decimals,
            "markets": list(self.markets),
            "leverage_levels": list(self.leverage_levels),
            "margin_mode": self.margin_mode,
            "margin_percent": list(self.margin_percent),
        }


@dataclass(frozen=True, slots=True)
class FarmMarket:
    market_id: str
    market_name: str
    tick_decimals: int


@dataclass(frozen=True, slots=True)
class FarmMarketCatalog:
    markets: tuple[FarmMarket, ...]
    dynamic: bool

    def __post_init__(self) -> None:
        if not self.markets:
            raise FarmError("No usable Fairground markets were discovered")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(market.market_name for market in self.markets)


@dataclass(frozen=True, slots=True)
class FarmRuntimePlan:
    market_id: str
    market_name: str
    tick_decimals: int
    leverage: Decimal
    market_min_trade_size: Decimal
    market_max_trade_size: Decimal
    configured_margin: tuple[Decimal, Decimal]
    effective_margin: tuple[Decimal, Decimal]
    leverage_level: str = "FIXED"
    margin_mode: str = "FIXED"
    configured_margin_percent: tuple[Decimal, Decimal] | None = None
    collateral_balance_usdc: Decimal | None = None
    oracle_price: Decimal | None = None

    @property
    def effective_notional(self) -> tuple[Decimal, Decimal]:
        return (
            self.effective_margin[0] * self.leverage,
            self.effective_margin[1] * self.leverage,
        )

    @property
    def margin_was_adjusted(self) -> bool:
        return self.effective_margin != self.configured_margin

    def summary(self) -> str:
        low, high = self.effective_margin
        notional_low, notional_high = self.effective_notional
        return (
            f"{self.market_name} · live min notional={_plain(self.market_min_trade_size)} · "
            f"effective margin={_plain(low)}–{_plain(high)} · "
            f"notional={_plain(notional_low)}–{_plain(notional_high)} "
            f"at {_plain(self.leverage)}x"
            + (
                f" ({self.leverage_level})"
                if self.leverage_level != "FIXED"
                else ""
            )
            + (
                f" · balance={_plain(self.collateral_balance_usdc)} USDC · "
                f"{_plain(self.configured_margin_percent[0])}–"
                f"{_plain(self.configured_margin_percent[1])}%"
                if self.collateral_balance_usdc is not None
                and self.configured_margin_percent is not None
                else ""
            )
        )


@dataclass(frozen=True, slots=True)
class FarmStartupPlan:
    catalog: FarmMarketCatalog
    runtimes: tuple[FarmRuntimePlan, ...] = ()
    margin_mode: str = "FIXED"
    leverage_levels: tuple[str, ...] = ()
    margin_percent: tuple[str, str] = ("10", "20")

    @property
    def margin_was_adjusted(self) -> bool:
        return any(runtime.margin_was_adjusted for runtime in self.runtimes)

    def summary(self) -> str:
        if (
            len(self.catalog.markets) == 1
            and len(self.runtimes) == 1
            and not self.catalog.dynamic
        ):
            return self.runtimes[0].summary()
        pairs = ", ".join(self.catalog.names)
        leverage = (
            "/".join(self.leverage_levels)
            if self.leverage_levels
            else (
                _plain(self.runtimes[0].leverage)
                if self.runtimes
                else "fixed"
            )
        )
        margin = (
            f"balance {self.margin_percent[0]}–{self.margin_percent[1]}%"
            if self.margin_mode == "PERCENT"
            else "fixed USDC"
        )
        return (
            f"markets={len(self.catalog.markets)} [{pairs}] · "
            f"leverage={leverage} · margin={margin}"
        )


@dataclass
class AccountFarmResult:
    account: str
    label: str
    planned_trades: int
    completed_trades: int
    success: bool
    errors: list[str] = field(default_factory=list)
    last_state: str | None = None
    volume_notional_estimate: str = "0"
    waitlisted: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "label": self.label,
            "plannedTrades": self.planned_trades,
            "completedTrades": self.completed_trades,
            "success": self.success,
            "errors": self.errors,
            "lastState": self.last_state,
            "volumeNotionalEstimate": self.volume_notional_estimate,
            "waitlisted": self.waitlisted,
        }


LogFn = Callable[[str], None]
SleepFn = Callable[[float], None]


def _log_default(message: str) -> None:
    print(message, flush=True)


def _rand_decimal(low: str, high: str) -> Decimal:
    a = Decimal(low)
    b = Decimal(high)
    if a > b:
        a, b = b, a
    low_units = int((a / MARGIN_QUANTUM).to_integral_value(rounding=ROUND_CEILING))
    high_units = int((b / MARGIN_QUANTUM).to_integral_value(rounding=ROUND_FLOOR))
    if low_units > high_units:
        raise FarmError("Margin range has no value representable in 6-decimal USDC units")
    return Decimal(randint(low_units, high_units)) * MARGIN_QUANTUM


def _rand_int(bounds: tuple[int, int]) -> int:
    return randint(bounds[0], bounds[1])


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _ceil_margin(value: Decimal) -> Decimal:
    return value.quantize(MARGIN_QUANTUM, rounding=ROUND_CEILING)


def _floor_margin(value: Decimal) -> Decimal:
    return value.quantize(MARGIN_QUANTUM, rounding=ROUND_FLOOR)


def _used_margin_percent(margin: Decimal, balance: Decimal) -> Decimal:
    if balance <= 0:
        raise FarmError("Cannot calculate margin percentage from a zero balance")
    return (
        margin / balance * PERCENT_DENOMINATOR
    ).quantize(PERCENT_DISPLAY_QUANTUM, rounding=ROUND_FLOOR)


def _live_market_config(
    client: FairgroundClient,
    *,
    market_id: str,
    market_name: str,
    configured_tick_decimals: int,
) -> tuple[MarketLimits, int]:
    payload = client.get_market_config(market_id)
    raw = payload.get("marketConfig")
    if not isinstance(raw, dict):
        raise FarmError("Fairground не вернул live marketConfig; запуск заблокирован")
    try:
        market = MarketLimits.from_api(raw)
    except ValidationError as exc:
        raise FarmError(f"Некорректные live-лимиты рынка: {exc}") from exc
    if market.market_id != market_id or market.market_name != market_name:
        raise FarmError(
            f"Рынок {market_name} ({market_id}) больше не совпадает "
            "с live-конфигурацией"
        )
    tick_raw = raw.get("tickDecimals")
    if isinstance(tick_raw, bool):
        raise FarmError("Live tickDecimals имеет неверный формат")
    try:
        live_tick = int(tick_raw)
    except (TypeError, ValueError) as exc:
        raise FarmError("Live marketConfig не содержит tickDecimals") from exc
    if not 0 <= live_tick <= 12:
        raise FarmError("Live tickDecimals вне безопасного диапазона 0..12")
    if configured_tick_decimals >= 0 and configured_tick_decimals != live_tick:
        raise FarmError(
            f"tickDecimals для {market_name}: configured={configured_tick_decimals}, "
            f"live={live_tick}"
        )
    return market, live_tick


# Session markets that routinely report isOpen=false outside hours.
# Blocked in MARKETS=["ALL"]; multi-pair allowlists soft-skip when closed.
_DEFAULT_BLOCKED_MARKETS = frozenset(
    {
        "EUR-USD",
        "GBP-USD",
        "AUD-USD",
        "JPY-USD",
        "XAU-USD",
        "XAG-USD",
        "WTI-USD",
    }
)

_CLOSED_MARKERS = frozenset({"0", "false", "closed", "no", "off"})


def _raw_is_open_flag(raw_open: object) -> bool | None:
    """Parse Fairground isOpen. None = flag missing (unknown)."""

    if raw_open is None:
        return None
    if raw_open is False or raw_open == 0:
        return False
    if raw_open is True or raw_open == 1:
        return True
    text = str(raw_open).strip().lower()
    if not text:
        return None
    if text in _CLOSED_MARKERS:
        return False
    if text in {"1", "true", "open", "yes", "on"}:
        return True
    return None


def _market_is_open(client: FairgroundClient, market_id: str) -> bool:
    """False only when Fairground explicitly marks the market closed."""

    try:
        payload = client.get_market_summary(market_id)
        summary = payload.get("marketSummary")
        if not isinstance(summary, dict):
            return True
        parsed = _raw_is_open_flag(summary.get("isOpen"))
        if parsed is None:
            # Missing flag: treat as open; preview will re-check.
            return True
        return parsed
    except (FairgroundAPIError, AttributeError, TypeError, ValueError, KeyError):
        # Summary flaky / stub clients — discovery still has oracle/config gates.
        return True


def _is_closed_market_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "currently closed" in text
        or "market is closed" in text
        or "market is currently closed" in text
        or "is currently closed" in text
    )


def _is_recoverable_round_error(exc: BaseException | str) -> bool:
    """True when the account can keep farming after skipping this market."""

    if type(exc).__name__ == "SimulationError":
        return True
    text = str(exc).lower()
    markers = (
        "currently closed",
        "simulation reverted",
        "reverted on-chain",
        "transaction ceiling",
        "max fee per gas exceeds",
        "filled size regressed",
        "temporarily missing",
        "has not indexed",
        "oracle",
        "не подходит",
        "нет открытой пары",
    )
    return any(marker in text for marker in markers)


def discover_farm_market_catalog(
    client: FairgroundClient,
    farm: FarmConfig,
) -> FarmMarketCatalog:
    """Resolve dynamic selectors against live, open, priced markets.

    Legacy configurations intentionally remain a singleton and retain the old
    behavior (the exact config is still checked by ``resolve_farm_runtime_plan``).
    ALL mode and multi-pair allowlists are best-effort: closed / unpriced
    markets are omitted. A single explicit selector is strict (fail closed).
    """

    if not farm.markets:
        return FarmMarketCatalog(
            markets=(
                FarmMarket(
                    market_id=farm.market_id,
                    market_name=farm.market_name,
                    tick_decimals=farm.tick_decimals,
                ),
            ),
            dynamic=False,
        )

    payload = client.get_markets()
    raw_markets = payload.get("markets")
    if not isinstance(raw_markets, list):
        raise FarmError("Fairground не вернул live список рынков")

    candidates: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for raw in raw_markets:
        if not isinstance(raw, dict):
            continue
        try:
            market_id = normalize_market_id(raw.get("marketId"))
        except ValidationError:
            continue
        market_name = str(raw.get("marketName", "")).strip()
        if (
            not market_name
            or len(market_name) > 80
            or market_id in seen_ids
        ):
            continue
        seen_ids.add(market_id)
        candidates.append((market_id, market_name))
    if not candidates:
        raise FarmError("Fairground вернул пустой или некорректный список рынков")

    all_mode = farm.markets == ("ALL",)
    # Multi-pair allowlists (top-10) soft-skip closed/unavailable pairs so one
    # FX/metal session outage does not kill the whole farm start.
    soft_skip = all_mode or len(farm.markets) > 1
    selected: list[tuple[str, str]] = []
    if all_mode:
        selected = [
            (market_id, name)
            for market_id, name in candidates
            if name.upper() not in _DEFAULT_BLOCKED_MARKETS
        ]
        if not selected:
            selected = candidates
    else:
        by_id = {market_id: (market_id, name) for market_id, name in candidates}
        by_name: dict[str, list[tuple[str, str]]] = {}
        for item in candidates:
            by_name.setdefault(item[1].casefold(), []).append(item)
        for selector in farm.markets:
            if selector.isdecimal():
                try:
                    normalized = normalize_market_id(selector)
                except ValidationError as exc:
                    raise FarmError(f"Некорректный market selector: {selector}") from exc
                match = by_id.get(normalized)
                if match is None:
                    if soft_skip:
                        continue
                    raise FarmError(f"Запрошенный рынок ID {selector} не найден")
            else:
                matches = by_name.get(selector.casefold(), [])
                if len(matches) != 1:
                    if soft_skip:
                        continue
                    detail = "не найден" if not matches else "неоднозначен"
                    raise FarmError(f"Запрошенный рынок {selector} {detail}")
                match = matches[0]
            if any(existing[0] == match[0] for existing in selected):
                raise FarmError(
                    f"Market selectors resolve to duplicate market {match[1]}"
                )
            selected.append(match)

    usable: list[FarmMarket] = []
    failures: list[str] = []
    for market_id, market_name in selected:
        try:
            if not _market_is_open(client, market_id):
                raise FarmError("market is currently closed")
            _market, tick_decimals = _live_market_config(
                client,
                market_id=market_id,
                market_name=market_name,
                configured_tick_decimals=-1,
            )
            # A listed market without a live oracle price cannot produce a safe
            # limit threshold. This currently filters WTI-USD in ALL mode.
            fetch_oracle_price(client, market_id)
        except (
            FairgroundAPIError,
            FarmError,
            ValidationError,
            TypeError,
            ValueError,
        ) as exc:
            failures.append(f"{market_name}: {exc}")
            if not soft_skip:
                raise FarmError(
                    f"Запрошенный рынок {market_name} сейчас недоступен: {exc}"
                ) from exc
            continue
        usable.append(
            FarmMarket(
                market_id=market_id,
                market_name=market_name,
                tick_decimals=tick_decimals,
            )
        )
    if not usable:
        detail = "; ".join(failures[:3])
        raise FarmError(
            "Нет доступных рынков с live config, open status и oracle price"
            + (f": {detail}" if detail else "")
        )
    return FarmMarketCatalog(markets=tuple(usable), dynamic=True)


def _resolved_leverage(
    *,
    farm: FarmConfig,
    market: MarketLimits,
    settings: Settings,
    leverage_level: str | None,
) -> tuple[Decimal, str]:
    allowed_max = min(market.max_leverage, settings.max_leverage)
    if allowed_max < market.min_leverage:
        raise FarmError(
            f"Локальный max leverage={_plain(settings.max_leverage)}x ниже "
            f"live minimum={_plain(market.min_leverage)}x для {market.market_name}"
        )

    if not farm.leverage_levels:
        leverage = decimal_value(farm.leverage, "leverage")
        assert leverage is not None
        if leverage < market.min_leverage or leverage > allowed_max:
            raise FarmError(
                f"LEVERAGE={_plain(leverage)} недопустим: live/local диапазон "
                f"{_plain(market.min_leverage)}–{_plain(allowed_max)}x"
            )
        return leverage, "FIXED"

    level = (
        farm.leverage_levels[0]
        if leverage_level is None
        else str(leverage_level).strip().upper()
    )
    if level not in farm.leverage_levels or level not in LEVERAGE_LEVEL_FACTORS:
        raise FarmError(f"Leverage level {level!r} is not enabled")
    leverage = (allowed_max * LEVERAGE_LEVEL_FACTORS[level]).quantize(
        LEVERAGE_QUANTUM,
        rounding=ROUND_FLOOR,
    )
    minimum = market.min_leverage.quantize(
        LEVERAGE_QUANTUM,
        rounding=ROUND_CEILING,
    )
    leverage = max(leverage, minimum)
    if leverage > allowed_max:
        raise FarmError(
            f"Live leverage range for {market.market_name} cannot be represented "
            "with 0.01x precision"
        )
    return leverage, level


def resolve_farm_runtime_plan(
    client: FairgroundClient,
    farm: FarmConfig,
    settings: Settings,
    *,
    selected_market: FarmMarket | None = None,
    leverage_level: str | None = None,
    collateral_balance_units: int | None = None,
) -> FarmRuntimePlan:
    """Intersect user ranges with live market and compiled local limits.

    This is read-only and must run before a signer/executor is loaded.  The
    workflow still repeats its own live safety preview immediately before any
    entry, so a market-config change fails closed.
    """

    configured_market = selected_market or FarmMarket(
        market_id=farm.market_id,
        market_name=farm.market_name,
        tick_decimals=farm.tick_decimals,
    )
    market, live_tick = _live_market_config(
        client,
        market_id=configured_market.market_id,
        market_name=configured_market.market_name,
        configured_tick_decimals=configured_market.tick_decimals,
    )
    leverage, resolved_level = _resolved_leverage(
        farm=farm,
        market=market,
        settings=settings,
        leverage_level=leverage_level,
    )

    configured_percent: tuple[Decimal, Decimal] | None = None
    collateral_balance: Decimal | None = None
    try:
        if farm.margin_mode == "PERCENT":
            if (
                isinstance(collateral_balance_units, bool)
                or not isinstance(collateral_balance_units, int)
                or collateral_balance_units < 0
            ):
                raise FarmError(
                    "PERCENT margin requires a fresh collateral balance"
                )
            collateral_balance = (
                Decimal(collateral_balance_units) / Decimal(10**6)
            )
            if collateral_balance <= 0:
                raise FarmError("PERCENT margin: USDC balance is zero")
            configured_percent = (
                Decimal(farm.margin_percent[0]),
                Decimal(farm.margin_percent[1]),
            )
            configured_low = _ceil_margin(
                collateral_balance
                * configured_percent[0]
                / PERCENT_DENOMINATOR
            )
            configured_high = _floor_margin(
                collateral_balance
                * configured_percent[1]
                / PERCENT_DENOMINATOR
            )
        else:
            raw_margin = [decimal_value(value, "margin") for value in farm.margin]
            if any(
                value is None for value in raw_margin
            ):  # pragma: no cover - required values
                raise FarmError("MARGIN должен содержать два положительных числа")
            configured_low = _ceil_margin(min(raw_margin))  # type: ignore[arg-type]
            configured_high = _floor_margin(max(raw_margin))  # type: ignore[arg-type]
        minimum_margin = _ceil_margin(market.min_trade_size / leverage)
        notional_cap = min(market.max_trade_size, settings.max_notional_usdc)
        maximum_margin = min(
            _floor_margin(settings.max_margin_usdc),
            _floor_margin(notional_cap / leverage),
        )
        if collateral_balance is not None:
            maximum_margin = min(
                maximum_margin,
                _floor_margin(collateral_balance),
            )
    except InvalidOperation as exc:
        raise FarmError(
            "MARGIN/live limits не помещаются в 6-decimal USDC диапазон"
        ) from exc
    if configured_low > configured_high:
        raise FarmError(
            "Настроенный margin range не содержит значения с точностью USDC"
        )
    if maximum_margin < minimum_margin:
        raise FarmError(
            f"Live/local limits for {market.market_name} leave no legal margin: "
            f"min={_plain(minimum_margin)} max={_plain(maximum_margin)} "
            f"at leverage={_plain(leverage)}x "
            f"(minTrade={_plain(market.min_trade_size)} "
            f"maxNotional={_plain(notional_cap)})"
        )
    effective_low = max(configured_low, minimum_margin)
    effective_high = min(configured_high, maximum_margin)
    if effective_low > effective_high:
        if farm.margin_mode == "PERCENT":
            # Volume farm: clamp percent intent into the only live-safe window.
            # Typical case: MARGIN_PERCENT 70–95% of a fat test balance exceeds
            # MAX_MARGIN_USDC / maxNotional÷leverage — use the market cap.
            if configured_low > maximum_margin:
                effective_low = maximum_margin
                effective_high = maximum_margin
            elif configured_high < minimum_margin:
                effective_low = minimum_margin
                effective_high = minimum_margin
            else:
                # Quantize edge — take full live-safe span.
                effective_low = minimum_margin
                effective_high = maximum_margin
        else:
            raise FarmError(
                f"MARGIN={_plain(configured_low)}–{_plain(configured_high)} при "
                f"LEVERAGE={_plain(leverage)}x не пересекается с live/local диапазоном; "
                f"для minTradeSize={_plain(market.min_trade_size)} нужен margin не ниже "
                f"{_plain(minimum_margin)}, допустимый максимум — {_plain(maximum_margin)}"
            )
    if farm.cheapest_margin:
        # Pin every new round to the smallest live-safe value allowed by the
        # effective range after live clamps.
        effective_high = effective_low
    if effective_low * leverage < market.min_trade_size:
        raise FarmError("Внутренняя ошибка расчёта: notional ниже live minTradeSize")
    if effective_high * leverage > notional_cap:
        raise FarmError("Внутренняя ошибка расчёта: notional выше live/local maximum")
    if collateral_balance is not None and effective_high > _floor_margin(
        collateral_balance
    ):
        raise FarmError("Внутренняя ошибка: margin превысил collateral balance")
    return FarmRuntimePlan(
        market_id=market.market_id,
        market_name=market.market_name,
        tick_decimals=live_tick,
        leverage=leverage,
        market_min_trade_size=market.min_trade_size,
        market_max_trade_size=market.max_trade_size,
        configured_margin=(configured_low, configured_high),
        effective_margin=(effective_low, effective_high),
        leverage_level=resolved_level,
        margin_mode=farm.margin_mode,
        configured_margin_percent=configured_percent,
        collateral_balance_usdc=collateral_balance,
    )


def quantize_price(
    price: Decimal,
    *,
    tick_decimals: int,
    side: str,
    entry_offset_bps: int,
) -> Decimal:
    """Build a market-style protective threshold from the oracle.

    Fairground openOrder has no separate market enum — UI "Market + N%" is a
    priceThreshold around the oracle so the order fills immediately.
    """

    if tick_decimals < 0:
        raise FarmError("tick_decimals is required to build market thresholds")
    if price <= 0:
        raise FarmError("Oracle price must be positive")
    # Minimum 50 bps so we never place a tight resting limit by accident.
    bps = max(50, int(entry_offset_bps))
    offset = Decimal(bps) / Decimal(10_000)
    if side == "long":
        # Buy market: accept up to oracle*(1+tol).
        adjusted = price * (Decimal(1) + offset)
        rounding = ROUND_CEILING
    else:
        # Sell market: accept down to oracle*(1-tol).
        adjusted = price * (Decimal(1) - offset)
        rounding = ROUND_FLOOR
    scale = Decimal(10) ** tick_decimals
    units = (adjusted * scale).to_integral_value(rounding=rounding)
    if units <= 0:
        raise FarmError("Quantized market threshold is not positive")
    return units / scale


def fetch_oracle_price(client: FairgroundClient, market_id: str) -> Decimal:
    payload = client.get_price(market_id)
    price_raw = payload.get("price")
    if not isinstance(price_raw, dict):
        raise FairgroundAPIError("Invalid oracle price payload")
    if normalize_market_id(price_raw.get("marketId")) != normalize_market_id(market_id):
        raise FairgroundAPIError("Oracle market mismatch")
    value = price_raw.get("price")
    try:
        price = Decimal(str(value))
    except Exception as exc:
        raise FairgroundAPIError("Oracle price is not numeric") from exc
    if not price.is_finite() or price <= 0:
        raise FairgroundAPIError("Oracle price is not positive")
    return price


@dataclass(frozen=True, slots=True)
class _AddressOnlySigner:
    """Bind read-only RPC calls to an account without exposing signing."""

    address: str

    def sign_transaction(self, _transaction: dict[str, Any]) -> Any:
        raise FarmError("Read-only percent-margin executor cannot sign")


class VolumeFarm:
    """Run open/close loops across many Fairground accounts."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StateStore,
        farm: FarmConfig,
        accounts: list[FarmAccount],
        log: LogFn = _log_default,
        sleep: SleepFn = time.sleep,
        dry_run: bool = False,
        recovery_only: bool = False,
    ) -> None:
        if not accounts:
            raise FarmError("No accounts loaded")
        if dry_run and recovery_only:
            raise FarmError("recovery_only is unavailable in dry-run")
        self.settings = settings
        self.store = store
        self.farm = farm
        self.accounts = list(accounts)
        self.log = log
        self.sleep = sleep
        self.dry_run = dry_run
        self._identities: dict[str, BrowserIdentity] = {}
        self._tls = local()
        self._cancel_check: Callable[[], None] | None = None
        self._run_id = ""
        self._on_round: Callable[[dict[str, Any]], None] | None = None
        self._browser_fetch: Callable[[str, dict[str, str], bytes], tuple[int, bytes]] | None = None
        self._cookie_header = ""
        # Used only when main.py could not validate current parameters but a
        # persisted LIVE cycle still needs its exact saved intent recovered.
        # This is a hard execution boundary: no catalog, preview, intent, or
        # CycleRunner.start path is reachable after recovery.
        self.recovery_only = recovery_only
        self._startup_plan: FarmStartupPlan | None = None
        self._catalog: FarmMarketCatalog | None = None
        self._catalog_lock = Lock()

    def _catalog_for(self, client: FairgroundClient) -> FarmMarketCatalog:
        cached = self._catalog
        if cached is not None:
            return cached
        with self._catalog_lock:
            if self._catalog is None:
                self._catalog = discover_farm_market_catalog(client, self.farm)
            return self._catalog

    def preflight(self) -> FarmStartupPlan:
        """Read live limits without loading a signer or touching chain state."""

        client = self._client_for(self.accounts[0])
        catalog = self._catalog_for(client)
        runtimes: tuple[FarmRuntimePlan, ...] = ()
        # Preserve the original strict, signerless startup validation for old
        # single-market FIXED configurations. Dynamic/percent plans need a
        # per-account market choice and (for percent) a fresh account balance.
        if not catalog.dynamic and self.farm.margin_mode == "FIXED":
            runtimes = (
                resolve_farm_runtime_plan(
                    client,
                    self.farm,
                    self.settings,
                    selected_market=catalog.markets[0],
                ),
            )
        plan = FarmStartupPlan(
            catalog=catalog,
            runtimes=runtimes,
            margin_mode=self.farm.margin_mode,
            leverage_levels=self.farm.leverage_levels,
            margin_percent=self.farm.margin_percent,
        )
        self._startup_plan = plan
        return plan

    def active_cycles(self) -> list[tuple[FarmAccount, CycleRecord]]:
        """Return persisted recoveries without consulting current entry config."""

        active: list[tuple[FarmAccount, CycleRecord]] = []
        for account in self.accounts:
            record = self.store.get_active_cycle(account.address)
            if record is not None:
                active.append((account, record))
        return active

    def bind_identity(self, address: str, identity: BrowserIdentity) -> None:
        self._identities[address.lower()] = identity

    def bind_cancel(self, cancel_check: Callable[[], None] | None) -> None:
        self._cancel_check = cancel_check

    def bind_run_id(self, run_id: str) -> None:
        self._run_id = str(run_id or "")

    def bind_on_round(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._on_round = callback

    def bind_browser_http(
        self,
        *,
        cookie_header: str = "",
        browser_fetch: Callable[[str, dict[str, str], bytes], tuple[int, bytes]] | None = None,
    ) -> None:
        self._cookie_header = str(cookie_header or "")
        self._browser_fetch = browser_fetch

    def _waitlist_for(
        self, client: FairgroundClient, account: FarmAccount, usdc_units: int
    ) -> WaitlistAdmission:
        positions = None
        orders = None
        portfolio = None
        try:
            positions = client.get_open_positions(account.address)
        except Exception:
            pass
        try:
            orders = client.get_orders(account.address)
        except Exception:
            pass
        try:
            portfolio = client.get_portfolio(account.address)
        except Exception:
            pass
        return admission_from_chain_and_api(
            usdc_units=usdc_units,
            positions_payload=positions,
            orders_payload=orders,
            portfolio_payload=portfolio,
        )

    def _log_live_finish(self, results: list[AccountFarmResult]) -> None:
        ok, skipped, failed = _tally_live_finish(results)
        marker = "✓" if failed == 0 else ("-" if ok == 0 and skipped == 0 else "!")
        self.log(
            f"[{marker}] LIVE FINISH · success={ok}/{len(results)} · "
            f"не в WL={skipped}/{len(results)} · failed={failed}/{len(results)}"
        )

    def _session(self) -> SessionStyle | None:
        return getattr(self._tls, "session", None)

    def _pause(self, seconds: float) -> None:
        lo = max(0.0, float(seconds) * 0.82)
        hi = max(lo, float(seconds) * 1.28)
        sleep_jitter(lo, hi, cancel_check=self._cancel_check)

    def _client_for(self, account: FarmAccount) -> FairgroundClient:
        identity = self._identities.get(account.address.lower())
        return FairgroundClient(
            self.settings.api_url,
            timeout_seconds=self.settings.timeout_seconds,
            proxy_url=account.proxy.url,
            identity=identity,
            chain_id=self.settings.chain_id,
            cookie_header=self._cookie_header,
            browser_fetch=self._browser_fetch,
        )

    def _executor_for(
        self, account: FarmAccount, client: FairgroundClient
    ) -> OnchainExecutor:
        if not account.private_key_hex:
            raise FarmError(f"{account.label}: no key material available")
        signer = load_private_key_hex(
            account.private_key_hex, expected_address=account.address
        )
        # The signer now owns the minimum key material required for this
        # workflow. Drop the unlocked vault string before any RPC setup.
        account.wipe_secret()
        return OnchainExecutor(
            rpc_url=self.settings.rpc_url,
            client=client,
            signer=signer,
            timeout_seconds=self.settings.timeout_seconds,
            proxy_url=account.proxy.url,
        )

    def _read_executor_for(
        self, account: FarmAccount, client: FairgroundClient
    ) -> OnchainExecutor:
        return OnchainExecutor(
            rpc_url=self.settings.rpc_url,
            client=client,
            signer=_AddressOnlySigner(account.address),  # type: ignore[arg-type]
            timeout_seconds=self.settings.timeout_seconds,
            proxy_url=account.proxy.url,
        )

    _UI_MARGIN_STEPS = (
        8, 10, 12, 14, 15, 16, 18, 20, 22, 25, 28, 30, 33, 35, 40, 42, 45, 50, 55, 60, 67, 70, 75
    )

    def _roll_margin(self, runtime: FarmRuntimePlan) -> Decimal:
        lo, hi = runtime.effective_margin
        if hi <= lo:
            return lo
        session = self._session()
        rng = session.rng if session is not None else None
        if runtime.margin_mode == "PERCENT" and runtime.collateral_balance_usdc:
            balance = runtime.collateral_balance_usdc
            cfg = runtime.configured_margin_percent or (Decimal("10"), Decimal("45"))
            pct_lo = float(cfg[0])
            pct_hi = float(cfg[1])
            if pct_hi < pct_lo:
                pct_lo, pct_hi = pct_hi, pct_lo
            style = session.margin_style if session is not None else "spread"
            draw = (rng.random() if rng is not None else uniform(0, 1))
            if style == "steps":
                steps = [step for step in self._UI_MARGIN_STEPS if pct_lo - 0.4 <= step <= pct_hi + 0.4]
                if steps:
                    # Bias toward lower steps when margin_low_weight is high.
                    weight = session.margin_low_weight if session is not None else 0.4
                    ranked = list(steps)
                    if rng is not None:
                        ranked.sort(
                            key=lambda value: (value - pct_lo) * (1.0 - weight)
                            + rng.random() * 12.0
                        )
                        pct = float(ranked[0])
                    else:
                        pct = float(choice(steps))
                else:
                    pct = pct_lo + (pct_hi - pct_lo) * draw
            elif style == "conservative":
                power = 1.35 + (session.margin_low_weight if session is not None else 0.4)
                pct = pct_lo + (pct_hi - pct_lo) * (draw ** power)
            else:
                mid = pct_lo + (pct_hi - pct_lo) * (session.margin_low_weight if session is not None else 0.45)
                if rng is not None:
                    pct = rng.triangular(pct_lo, pct_hi, min(pct_hi, max(pct_lo, mid)))
                else:
                    pct = pct_lo + (pct_hi - pct_lo) * draw
            jitter = (rng.uniform(-2.8, 2.8) if rng is not None else uniform(-1.5, 1.5))
            pct = max(pct_lo, min(pct_hi, pct + jitter))
            margin = (Decimal(str(round(pct, 2))) / PERCENT_DENOMINATOR) * balance
            margin = _floor_margin(margin)
            if margin < lo:
                margin = lo
            if margin > hi:
                margin = hi
            return margin
        # FIXED: log-leaning draw inside the live-safe window.
        unit = (rng.random() ** 0.85) if rng is not None else uniform(0, 1)
        span = hi - lo
        picked = lo + span * Decimal(str(unit))
        return _floor_margin(picked)

    def _pick_side(self) -> str:
        options: list[str] = []
        if self.farm.allow_long:
            options.append("long")
        if self.farm.allow_short:
            options.append("short")
        if not options:
            return "long"
        session = self._session()
        if session is None or len(options) == 1:
            return choice(options)
        if "long" in options and session.rng.random() < session.long_bias:
            return "long"
        if "short" in options:
            return "short"
        return choice(options)

    def _runtime_for_round(
        self,
        *,
        client: FairgroundClient,
        catalog: FarmMarketCatalog,
        settings: Settings,
        collateral_balance_units: int | None,
        excluded_markets: set[str] | None = None,
    ) -> FarmRuntimePlan:
        levels: tuple[str | None, ...] = (
            tuple(self.farm.leverage_levels)
            if self.farm.leverage_levels
            else (None,)
        )
        blocked = excluded_markets or set()
        candidates = [
            (market, level)
            for market in catalog.markets
            if market.market_name not in blocked
            for level in levels
        ]
        shuffle(candidates)
        failures: list[str] = []
        for selected_market, leverage_level in candidates:
            try:
                runtime = resolve_farm_runtime_plan(
                    client,
                    self.farm,
                    settings,
                    selected_market=selected_market,
                    leverage_level=leverage_level,
                    collateral_balance_units=collateral_balance_units,
                )
                # The catalog is cached for the session, but an oracle can
                # disappear after discovery. Probe it again while alternatives
                # are still available so one stale pair does not stop the
                # entire account.
                if not _market_is_open(client, runtime.market_id):
                    if excluded_markets is not None:
                        excluded_markets.add(selected_market.market_name)
                    raise FarmError("Selected market is currently closed")
                oracle_price = fetch_oracle_price(client, runtime.market_id)
                return replace(runtime, oracle_price=oracle_price)
            except (FarmError, FairgroundAPIError, ValidationError) as exc:
                label = leverage_level or "FIXED"
                if _is_closed_market_error(exc) and excluded_markets is not None:
                    excluded_markets.add(selected_market.market_name)
                failures.append(
                    f"{selected_market.market_name}/{label}: {str(exc)[:140]}"
                )
        detail = "; ".join(failures[:3])
        if len(failures) > 3:
            detail += f"; +{len(failures) - 3} more"
        raise FarmError(
            "Ни одна market/leverage комбинация не подходит"
            + (f": {detail}" if detail else "")
        )

    def _pick_ready_intent(
        self,
        *,
        client: FairgroundClient,
        catalog: FarmMarketCatalog,
        settings: Settings,
        collateral_balance_units: int | None,
        excluded_markets: set[str],
        account_label: str,
    ) -> tuple[FarmRuntimePlan, TradeCycleIntent]:
        """Pick an open market, build intent, run preview — soft-skip closed pairs."""

        last_error: Exception | None = None
        # Bound attempts: each market × each leverage level once.
        levels_n = max(1, len(self.farm.leverage_levels) or 1)
        max_attempts = max(1, len(catalog.markets) * levels_n)
        for _ in range(max_attempts):
            available = [
                market
                for market in catalog.markets
                if market.market_name not in excluded_markets
            ]
            if not available:
                break
            runtime: FarmRuntimePlan | None = None
            try:
                runtime = self._runtime_for_round(
                    client=client,
                    catalog=FarmMarketCatalog(
                        markets=tuple(available),
                        dynamic=catalog.dynamic,
                    ),
                    settings=settings,
                    collateral_balance_units=collateral_balance_units,
                    excluded_markets=excluded_markets,
                )
                intent = self._build_intent(client=client, runtime=runtime)
                self._preview_intent(client, intent)
                return runtime, intent
            except (
                FarmError,
                FairgroundAPIError,
                ValidationError,
                WorkflowError,
            ) as exc:
                last_error = exc
                text = str(exc)
                # _runtime_for_round already walked every remaining pair —
                # do not spin the same failure N more times.
                if "Ни одна market/leverage комбинация не подходит" in text:
                    break
                name = runtime.market_name if runtime is not None else None
                if name:
                    excluded_markets.add(str(name))
                if _is_closed_market_error(exc):
                    self.log(
                        f"[!] {account_label} | market closed/skip · {exc} · "
                        "пробую другую пару"
                    )
                else:
                    short = text if len(text) <= 180 else text[:177] + "…"
                    self.log(
                        f"[!] {account_label} | pair skip · "
                        f"{type(exc).__name__}: {short} · пробую другую"
                    )
                continue
        if last_error is not None:
            raise FarmError(
                f"Нет открытой пары для нового round: {last_error}"
            ) from last_error
        raise FarmError("Нет открытой пары для нового round")

    def _build_intent(
        self,
        *,
        client: FairgroundClient,
        runtime: FarmRuntimePlan,
    ) -> TradeCycleIntent:
        side = self._pick_side()
        # `_runtime_for_round` probes the oracle while it can still fall back
        # to another market and carries that exact value into intent creation.
        # Direct callers retain the legacy on-demand read.
        oracle = (
            runtime.oracle_price
            if runtime.oracle_price is not None
            else fetch_oracle_price(client, runtime.market_id)
        )
        session = self._session()
        offset_bps = self.farm.entry_offset_bps
        if session is not None:
            offset_bps = int(
                session.rng.randint(session.offset_bps_lo, session.offset_bps_hi)
            )
        limit = quantize_price(
            oracle,
            tick_decimals=runtime.tick_decimals,
            side=side,
            entry_offset_bps=offset_bps,
        )
        margin = self._roll_margin(runtime)
        notional = margin * runtime.leverage
        if notional < runtime.market_min_trade_size:
            raise FarmError("Generated notional is below live minTradeSize")
        if notional > min(
            runtime.market_max_trade_size,
            self.settings.max_notional_usdc,
        ):
            raise FarmError("Generated notional exceeds live/local maximum")
        hold = session_hold_seconds(session, self.farm.hold_seconds)
        return TradeCycleIntent.from_values(
            market_id=runtime.market_id,
            market_name=runtime.market_name,
            side=side,
            margin=str(margin),
            leverage=str(runtime.leverage),
            limit_price=str(limit),
            hold_seconds=hold,
            entry_ttl_seconds=self.farm.entry_ttl_seconds,
            partial_ttl_seconds=self.farm.partial_ttl_seconds,
            exit_ttl_seconds=self.farm.exit_ttl_seconds,
            poll_seconds=self.farm.poll_seconds,
            max_exit_attempts=self.farm.max_exit_attempts,
            exit_slippage_bps=self.farm.exit_slippage_bps,
            allow_approval=self.farm.allow_approval,
        )

    def _preview_intent(
        self,
        client: FairgroundClient,
        intent: TradeCycleIntent,
    ) -> None:
        TradePreviewService(self.settings, client=client).preview(
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

    def _remote_is_flat(self, client: FairgroundClient, address: str) -> bool:
        """Signerless flat check for safe pre-entry cycle abort."""

        try:
            orders_payload = client.get_orders(address)
            positions_payload = client.get_open_positions(address)
        except (FairgroundAPIError, AttributeError, TypeError, ValueError):
            return False
        orders_raw = orders_payload.get("orders")
        positions_raw = positions_payload.get("positions")
        if not isinstance(orders_raw, list) or not isinstance(positions_raw, list):
            return False
        if positions_raw:
            return False
        for item in orders_raw:
            if not isinstance(item, dict):
                return False
            status = str(
                item.get("status") or item.get("orderStatus") or ""
            ).strip().upper()
            if status.startswith("ORDER_STATUS_"):
                status = status.removeprefix("ORDER_STATUS_")
            # Only an explicit terminal allowlist can support a flat proof.
            # Missing, pending and future/unknown enum values all fail closed.
            if not status or status not in {
                "FULLY_FILLED",
                "CLOSED",
                "CANCELLED",
                "CANCELED",
                "MERGED",
                "PARTIALLY_CANCELLED",
                "REJECTED",
                "EXPIRED",
            }:
                return False
        return True

    def _no_inflight_writes(self, record: CycleRecord) -> bool:
        """True when nothing is mid-broadcast (safe to drop local stuck cycle)."""

        if self.store.account_has_unresolved_actions(record.account):
            return False
        for action in self.store.actions_for_cycle(record.cycle_id):
            if action.status in {"SIGNED", "BROADCAST", "UNKNOWN"}:
                return False
            if action.status == "PREPARED" and (
                action.tx_hash is not None or action.raw_transaction is not None
            ):
                return False
        return True

    def _abort_pre_entry_if_safe(
        self,
        *,
        account: FarmAccount,
        client: FairgroundClient,
        record: CycleRecord,
        reason: str,
    ) -> CycleRecord | None:
        """Force-drop local stuck cycles when the wallet is remotely flat.

        Testnet volume farm: if POS=0 and ORD=0 and no in-flight write, never
        resume a dead QUARANTINED/BLOCKED cycle — that only reverts again.

        Also clears QUARANTINED/BLOCKED/PAUSED when the account is remotely flat
        even without pre-entry proof (API lag / legacy journals), so volume farm
        is not permanently stuck after a failed entry.
        """

        if record.state not in {
            CycleState.BLOCKED_FUNDS,
            CycleState.FUNDS_CHECK,
            CycleState.PREFLIGHT,
            CycleState.ENTRY_ARMED,
            CycleState.APPROVAL_PENDING,
            CycleState.ENTRY_TX_PENDING,
            CycleState.QUARANTINED,
            CycleState.PAUSED,
            CycleState.DUST_BLOCKED,
        }:
            return None
        if not self._remote_is_flat(client, account.address):
            return None
        if not self._no_inflight_writes(record):
            return None
        # Prefer durable pre-entry proof. Without it we still clear only the
        # operator-resolvable flat states (quarantine / blocked / paused).
        proven_pre_entry = self.store.is_provably_pre_entry(record)
        flat_resolvable = record.state in {
            CycleState.BLOCKED_FUNDS,
            CycleState.QUARANTINED,
            CycleState.PAUSED,
            CycleState.DUST_BLOCKED,
        }
        if not proven_pre_entry and not flat_resolvable:
            return None

        lease = self.store.acquire_lease(account.address)
        try:
            current = self.store.get_active_cycle(account.address)
            if current is None or current.cycle_id != record.cycle_id:
                return current
            # Re-check under lease — API lag right after a fill is the risk.
            if not self._remote_is_flat(client, account.address):
                return current
            if not self._no_inflight_writes(current):
                return current
            proven_now = self.store.is_provably_pre_entry(current)
            flat_ok = current.state in {
                CycleState.BLOCKED_FUNDS,
                CycleState.QUARANTINED,
                CycleState.PAUSED,
                CycleState.DUST_BLOCKED,
            }
            if not proven_now and not flat_ok:
                return None
            clean = (reason or "flat auto-abort").strip()[:240]
            if not proven_now:
                clean = f"{clean} · remote-flat override (no pre-entry proof)"[:240]
            for action in self.store.actions_for_cycle(current.cycle_id):
                if action.status == "PREPARED":
                    self.store.mark_action(action.action_id, "NOT_BROADCAST")
            if current.state in {
                CycleState.BLOCKED_FUNDS,
                CycleState.QUARANTINED,
                CycleState.PAUSED,
                CycleState.DUST_BLOCKED,
            }:
                return self.store.resolve_cycle_flat(current, reason=clean)
            return self.store.transition(
                current,
                CycleState.ABORTED,
                error=clean,
            )
        finally:
            self.store.release_lease(lease)

    def _run_one_account(self, account: FarmAccount) -> AccountFarmResult:
        session = None if self.recovery_only else roll_session(account.address, self._run_id)
        self._tls.session = session
        planned = 0 if self.recovery_only else _rand_int(self.farm.trades_count)
        if session is not None and session.skip_last_p > 0 and planned > 1:
            if session.rng.random() < session.skip_last_p:
                planned -= 1
        result = AccountFarmResult(
            account=account.address,
            label=account.label,
            planned_trades=planned,
            completed_trades=0,
            success=False,
        )
        volume = Decimal(0)
        if self.recovery_only:
            self.log(
                f"[•] {account.label} | START · RECOVERY ONLY · "
                f"proxy={account.proxy.redacted()}"
            )
        else:
            family = session.family if session is not None else "steady"
            self.log(
                f"[•] {account.label} | START · rounds={planned} · "
                f"style={family} · proxy={account.proxy.redacted()}"
            )
        try:
            if session is not None:
                start_wait = sleep_jitter(
                    session.start_lo, session.start_hi, cancel_check=self._cancel_check
                )
                if start_wait:
                    self.log(f"[•] {account.label} | WARMUP · {start_wait:.1f}s")
            client = self._client_for(account)
            chain: OnchainExecutor | None = None
            scoped_settings = replace(self.settings, account=account.address)

            # Recovery always has priority over validating parameters for a
            # future entry. A changed/invalid FarmConfig must never block the
            # reduce-only close path of a persisted risk-bearing cycle.
            active = (
                None
                if self.dry_run
                else self.store.get_active_cycle(account.address)
            )
            if active is not None and active.state is CycleState.PREFLIGHT:
                repaired = terminalize_invalid_preflight_without_chain(
                    settings=scoped_settings,
                    client=client,
                    store=self.store,
                    account=account.address,
                )
                if repaired is not None and repaired.state is CycleState.ABORTED:
                    self.log(
                        f"[✓] {account.label} | старый невалидный PREFLIGHT "
                        "закрыт как ABORTED без signer/подписи/отправки"
                    )
                    active = None
                else:
                    active = repaired

            # Flat + pre-entry stuck (BLOCKED_FUNDS / QUARANTINED after revert /
            # stale FUNDS_CHECK): drop local cycle WITHOUT resume. Resume would
            # re-broadcast a dead entry and can quarantine a healthy wallet.
            if active is not None:
                pre_clear = self._abort_pre_entry_if_safe(
                    account=account,
                    client=client,
                    record=active,
                    reason=(
                        "Auto-abort pre-entry stuck cycle (wallet flat, "
                        f"no live exposure): {active.state.value}"
                        + (
                            f" · {active.last_error}"
                            if active.last_error
                            else ""
                        )
                    ),
                )
                if pre_clear is not None and pre_clear.state is CycleState.ABORTED:
                    self.log(
                        f"[✓] {account.label} | старый {active.state.value} "
                        f"({active.cycle_id[:8]}…) сброшен без on-chain write · "
                        "аккаунт flat · "
                        + (
                            "RECOVERY ONLY завершён; новые rounds запрещены"
                            if self.recovery_only
                            else "стартую новые rounds"
                        )
                    )
                    result.last_state = CycleState.ABORTED.value
                    active = None
                elif pre_clear is None and active.state in {
                    CycleState.BLOCKED_FUNDS,
                    CycleState.QUARANTINED,
                    CycleState.FUNDS_CHECK,
                    CycleState.ENTRY_ARMED,
                }:
                    self.log(
                        f"[!] {account.label} | pre-entry auto-clear недоступен · "
                        f"state={active.state.value} · "
                        f"pre_entry_proof={self.store.is_provably_pre_entry(active)} · "
                        f"flat={self._remote_is_flat(client, account.address)}"
                    )

            if active is not None:
                self.log(
                    f"[!] {account.label} | RECOVERY · state={active.state.value} · "
                    f"cycle={active.cycle_id[:8]}… · live risk path (resume)"
                )
                chain = self._executor_for(account, client)
                chain.verify_deployment()
                runner = CycleRunner(
                    settings=scoped_settings,
                    client=client,
                    store=self.store,
                    chain=chain,
                    sleep=self.sleep,
                )
                record = runner.resume()
                result.last_state = getattr(
                    getattr(record, "state", None),
                    "value",
                    str(getattr(record, "state", "")),
                )
                if record.state is not CycleState.COMPLETE:
                    repaired_invalid_preflight = (
                        active.state is CycleState.PREFLIGHT
                        and record.state is CycleState.ABORTED
                        and str(getattr(record, "last_error", "") or "").startswith(
                            PREFLIGHT_VALIDATION_PREFIX
                        )
                    )
                    # After a failed resume, try one more zero-chain clear
                    # (e.g. resume flipped BLOCKED_FUNDS → QUARANTINED via revert).
                    # Always re-read the durable store row — resume mocks / partial
                    # objects are not trusted for journal proofs.
                    live_after = self.store.get_active_cycle(account.address)
                    cleared = None
                    if live_after is not None:
                        cleared = self._abort_pre_entry_if_safe(
                            account=account,
                            client=client,
                            record=live_after,
                            reason=(
                                "Auto-abort after failed resume (wallet still flat): "
                                f"{getattr(record, 'last_error', None) or record.state.value}"
                            ),
                        )
                    if repaired_invalid_preflight:
                        self.log(
                            f"[✓] {account.label} | старый невалидный PREFLIGHT "
                            "закрыт как ABORTED без подписи/отправки"
                            + (
                                "; RECOVERY ONLY завершён"
                                if self.recovery_only
                                else "; продолжаю с live-лимитами"
                            )
                        )
                    elif cleared is not None and cleared.state is CycleState.ABORTED:
                        detail = record.last_error or record.state.value
                        self.log(
                            f"[!] {account.label} | post-resume pre-entry "
                            f"{record.state.value} сброшен · {detail}"
                        )
                        self.log(
                            f"[•] {account.label} | "
                            + (
                                "RECOVERY ONLY завершён; новые rounds запрещены"
                                if self.recovery_only
                                else "продолжаю новые rounds"
                            )
                        )
                        result.last_state = cleared.state.value
                    else:
                        err = record.last_error or record.state.value
                        live_after = self.store.get_active_cycle(account.address)
                        if live_after is None:
                            # Cycle already terminal in DB — free to open new rounds.
                            self.log(
                                f"[•] {account.label} | resume left no active cycle · "
                                f"last={record.state.value} · continue"
                            )
                            result.last_state = record.state.value
                        elif self._remote_is_flat(client, account.address):
                            freed = self._abort_pre_entry_if_safe(
                                account=account,
                                client=client,
                                record=live_after,
                                reason=f"Post-resume flat free: {err}",
                            )
                            if freed is None and live_after.state in {
                                CycleState.DUST_BLOCKED,
                                CycleState.QUARANTINED,
                                CycleState.PAUSED,
                                CycleState.BLOCKED_FUNDS,
                            }:
                                try:
                                    lease = self.store.acquire_lease(account.address)
                                    try:
                                        current = self.store.get_active_cycle(
                                            account.address
                                        )
                                        if (
                                            current is not None
                                            and self._remote_is_flat(
                                                client, account.address
                                            )
                                            and self._no_inflight_writes(current)
                                        ):
                                            for action in self.store.actions_for_cycle(
                                                current.cycle_id
                                            ):
                                                if action.status == "PREPARED":
                                                    self.store.mark_action(
                                                        action.action_id,
                                                        "NOT_BROADCAST",
                                                    )
                                            freed = self.store.resolve_cycle_flat(
                                                current,
                                                reason=(
                                                    "Flat after resume · free account "
                                                    f"for volume farm: {err}"
                                                )[:240],
                                            )
                                    finally:
                                        self.store.release_lease(lease)
                                except Exception:
                                    freed = None
                            if freed is not None and freed.state is CycleState.ABORTED:
                                self.log(
                                    f"[✓] {account.label} | {record.state.value} "
                                    f"снят (wallet flat) · стартую новые rounds"
                                )
                                result.last_state = CycleState.ABORTED.value
                            else:
                                result.errors.append(
                                    f"Resume finished in {record.state.value}: {err}"
                                )
                                self.log(
                                    f"[-] {account.label} | resume not COMPLETE: "
                                    f"{record.state.value} · {err}"
                                )
                                return result
                        else:
                            result.errors.append(
                                f"Resume finished in {record.state.value}: {err}"
                            )
                            self.log(
                                f"[-] {account.label} | resume not COMPLETE: "
                                f"{record.state.value} · {err}"
                            )
                            if record.state is CycleState.BLOCKED_FUNDS:
                                self.log(
                                    f"[-] {account.label} | BLOCKED_FUNDS = нет test USDC "
                                    "и/или Sepolia ETH на gas, либо allowance."
                                )
                            elif record.state is CycleState.QUARANTINED:
                                self.log(
                                    f"[-] {account.label} | QUARANTINED + POS/ORD > 0 — "
                                    "меню «Принудительное закрытие»."
                                )
                            elif record.state is CycleState.DUST_BLOCKED:
                                self.log(
                                    f"[-] {account.label} | DUST_BLOCKED — "
                                    "меню «Принудительное закрытие»."
                                )
                            return result
                else:
                    if not self.recovery_only:
                        result.completed_trades += 1
                        try:
                            volume += Decimal(
                                str(active.intent.get("margin", "0"))
                            ) * Decimal(str(active.intent.get("leverage", "1")))
                        except Exception:
                            pass

            if self.recovery_only:
                # Current new-entry parameters were invalid. Recovery above is
                # allowed to use only the durable intent; even after it becomes
                # flat this invocation must stop instead of silently switching
                # to a placeholder/current strategy.
                remaining = self.store.get_active_cycle(account.address)
                if remaining is not None:
                    result.errors.append(
                        f"Recovery remains in {remaining.state.value}"
                    )
                    result.last_state = remaining.state.value
                else:
                    result.last_state = result.last_state or "NO_ACTIVE_CYCLE"
                result.success = remaining is None and not result.errors
                return result

            if result.completed_trades >= planned:
                result.success = (
                    self.store.get_active_cycle(account.address) is None
                    and not result.errors
                )
                return result

            # Waitlist is on-chain test USDC, not the HTTP market catalog.
            # Catalog GetMarkets must not run first: a gzip/API blip would
            # mark admitted wallets as a generic processing error.
            excluded_markets: set[str] = set()
            first_balance_units: int | None = None
            if self.farm.margin_mode == "PERCENT":
                if self.dry_run:
                    read_chain = self._read_executor_for(account, client)
                    snapshot = read_chain.verify_deployment()
                else:
                    if chain is None:
                        chain = self._executor_for(account, client)
                    snapshot = chain.verify_deployment()
                first_balance_units = int(snapshot.collateral_balance_units)
            admission = self._waitlist_for(client, account, int(first_balance_units or 0))
            result.waitlisted = admission.admitted
            if not admission.admitted:
                self.log(
                    f"[•] {account.label} | не в waitlist · test USDC=0 · "
                    "к торговле не допущен"
                )
                result.last_state = "NOT_WAITLISTED"
                result.success = True
                result.errors.clear()
                return result
            catalog = self._catalog_for(client)
            runtime, first_intent = self._pick_ready_intent(
                client=client,
                catalog=catalog,
                settings=scoped_settings,
                collateral_balance_units=first_balance_units,
                excluded_markets=excluded_markets,
                account_label=account.label,
            )
            self.log(f"[✓] {account.label} | MARKET CHECK · {runtime.summary()}")
            if runtime.margin_was_adjusted:
                configured_low, configured_high = runtime.configured_margin
                self.log(
                    f"[!] {account.label} | MARGIN {_plain(configured_low)}–"
                    f"{_plain(configured_high)} → effective "
                    f"{_plain(runtime.effective_margin[0])}–"
                    f"{_plain(runtime.effective_margin[1])} по live market/local limits"
                )
            if self.dry_run:
                intent = first_intent
                percent_detail = ""
                if runtime.collateral_balance_usdc is not None:
                    used_percent = _used_margin_percent(
                        intent.margin,
                        runtime.collateral_balance_usdc,
                    )
                    percent_detail = (
                        f" balance={_plain(runtime.collateral_balance_usdc)} "
                        f"used={_plain(used_percent)}%"
                    )
                self.log(
                    f"[✓] {account.label} | dry-run live preview "
                    f"{runtime.market_name} {intent.side} "
                    f"leverage={_plain(runtime.leverage)}x/{runtime.leverage_level} "
                    f"margin={_plain(intent.margin)}{percent_detail} "
                    f"notional={_plain(intent.margin * intent.leverage)} "
                    f"market_tol={intent.limit_price}"
                )
                result.completed_trades = 0
                result.success = True
                result.last_state = "DRY_RUN"
                return result

            first_new_round = result.completed_trades + 1
            self.log(
                f"[✓] {account.label} | ENTRY CHECK · live-лимиты и баланс USDC подтверждены"
            )
            if session is not None:
                think = configure_delay(cancel_check=self._cancel_check, style=session)
                self.log(f"[•] {account.label} | THINK · {think:.1f}s перед первым ордером")
            if chain is None:
                chain = self._executor_for(account, client)
                chain.verify_deployment()
            assert chain is not None

            for round_index in range(first_new_round, planned + 1):
                if self.store.get_active_cycle(account.address) is not None:
                    result.errors.append("Active cycle still present before new round")
                    break
                try:
                    if round_index == first_new_round:
                        intent = first_intent
                    else:
                        # Refresh balance, market, price, preview every later
                        # round. Closed pairs are skipped without killing the
                        # whole account mid-session.
                        balance_units = (
                            chain.read_collateral_balance_units()
                            if self.farm.margin_mode == "PERCENT"
                            else None
                        )
                        runtime, intent = self._pick_ready_intent(
                            client=client,
                            catalog=catalog,
                            settings=scoped_settings,
                            collateral_balance_units=balance_units,
                            excluded_markets=excluded_markets,
                            account_label=account.label,
                        )
                except FarmError as exc:
                    # No open pair left — stop gracefully with whatever rounds
                    # already completed instead of crashing the worker.
                    self.log(
                        f"[!] {account.label} | stop rounds · {exc} · "
                        f"done={result.completed_trades}/{planned}"
                    )
                    if result.completed_trades == 0:
                        result.errors.append(str(exc))
                    break
                percent_detail = ""
                if runtime.collateral_balance_usdc is not None:
                    used_percent = _used_margin_percent(
                        intent.margin,
                        runtime.collateral_balance_usdc,
                    )
                    percent_detail = (
                        f" · balance={_plain(runtime.collateral_balance_usdc)} USDC"
                        f" · used={_plain(used_percent)}%"
                    )
                self.log(
                    f"[•] {account.label} | ROUND {round_index}/{planned} · "
                    f"{runtime.market_name} · OPEN {intent.side.upper()} · "
                    f"leverage={_plain(runtime.leverage)}x/{runtime.leverage_level} · "
                    f"margin={_plain(intent.margin)} USDC{percent_detail} · "
                    f"notional={_plain(intent.margin * intent.leverage)} USDC · "
                    f"hold={intent.hold_seconds}s · market±{self.farm.entry_offset_bps}bps "
                    f"px={intent.limit_price}"
                )
                runner = CycleRunner(
                    settings=scoped_settings,
                    client=client,
                    store=self.store,
                    chain=chain,
                    sleep=self.sleep,
                )
                try:
                    record = runner.start(intent)
                except (
                    WorkflowNeedsReconciliation,
                    SimulationError,
                    WorkflowError,
                    OnchainError,
                    FairgroundAPIError,
                    StoreError,
                ) as exc:
                    err = str(exc)
                    self.log(
                        f"[!] {account.label} | ROUND {round_index}/{planned} "
                        f"exception · {type(exc).__name__}: {err}"
                    )
                    excluded_markets.add(runtime.market_name)
                    live = self.store.get_active_cycle(account.address)
                    if live is not None:
                        # Prefer resume (rehydrate/close) then flat abort.
                        try:
                            recovered = CycleRunner(
                                settings=scoped_settings,
                                client=client,
                                store=self.store,
                                chain=chain,
                                sleep=self.sleep,
                            ).resume()
                            if recovered.state is CycleState.COMPLETE:
                                self.log(
                                    f"[✓] {account.label} | stuck cycle recovered "
                                    f"after exception · continue"
                                )
                                live = None
                        except Exception:
                            recovered = None
                        live = self.store.get_active_cycle(account.address)
                        if live is not None:
                            cleared = self._abort_pre_entry_if_safe(
                                account=account,
                                client=client,
                                record=live,
                                reason=f"Auto-clear after round exception: {err}",
                            )
                            if (
                                cleared is not None
                                and cleared.state is CycleState.ABORTED
                            ):
                                self.log(
                                    f"[✓] {account.label} | cycle cleared after "
                                    f"exception · continue next market"
                                )
                                live = None
                    if live is not None:
                        result.errors.append(f"Round {round_index}: {err}")
                        result.last_state = live.state.value
                        break
                    if _is_recoverable_round_error(exc):
                        result.last_state = "RECOVERED_CONTINUE"
                        continue
                    result.errors.append(f"Round {round_index}: {err}")
                    break

                result.last_state = record.state.value
                if record.state is CycleState.COMPLETE:
                    result.completed_trades += 1
                    volume += intent.margin * intent.leverage
                    self.log(
                        f"[✓] {account.label} | ROUND {round_index}/{planned} COMPLETE · "
                        f"progress={result.completed_trades}/{planned}"
                    )
                    if self._on_round is not None:
                        tx_hash = ""
                        try:
                            for action in self.store.actions_for_cycle(record.cycle_id):
                                if action.tx_hash:
                                    tx_hash = action.tx_hash
                                    break
                        except Exception:
                            tx_hash = ""
                        try:
                            self._on_round(
                                {
                                    "address": account.address,
                                    "market": intent.market_name,
                                    "side": intent.side,
                                    "margin": str(intent.margin),
                                    "leverage": str(intent.leverage),
                                    "price": str(intent.limit_price),
                                    "tx": tx_hash,
                                }
                            )
                        except Exception:
                            pass
                else:
                    err = record.last_error or record.state.value
                    self.log(
                        f"[-] {account.label} | ROUND {round_index}/{planned} STOPPED · "
                        f"state={record.state.value} · {err}"
                    )
                    if record.state is CycleState.BLOCKED_FUNDS:
                        self.log(
                            f"[-] {account.label} | Нужны test USDC (margin) + "
                            "Arbitrum Sepolia ETH (gas). Смотри «Парсинг аккаунтов»."
                        )
                        result.errors.append(f"Round {round_index}: {err}")
                        break
                    excluded_markets.add(runtime.market_name)
                    live = self.store.get_active_cycle(account.address)
                    if live is not None and live.state in {
                        CycleState.QUARANTINED,
                        CycleState.BLOCKED_FUNDS,
                        CycleState.PAUSED,
                        CycleState.ABORTED,
                    }:
                        # Try close path first (rehydrate), then flat abort.
                        if live.state is CycleState.QUARANTINED:
                            try:
                                recovered = CycleRunner(
                                    settings=scoped_settings,
                                    client=client,
                                    store=self.store,
                                    chain=chain,
                                    sleep=self.sleep,
                                ).resume()
                                if recovered.state is CycleState.COMPLETE:
                                    self.log(
                                        f"[✓] {account.label} | quarantine closed "
                                        f"via rehydrate · continue"
                                    )
                                    live = None
                            except Exception:
                                pass
                            live = self.store.get_active_cycle(account.address)
                        if live is not None:
                            cleared = self._abort_pre_entry_if_safe(
                                account=account,
                                client=client,
                                record=live,
                                reason=f"Auto-clear after stopped round: {err}",
                            )
                            if (
                                cleared is not None
                                and cleared.state is CycleState.ABORTED
                            ):
                                self.log(
                                    f"[✓] {account.label} | stopped cycle cleared · "
                                    f"continue next market"
                                )
                                live = None
                    if live is not None and live.state is not CycleState.ABORTED:
                        result.errors.append(f"Round {round_index}: {err}")
                        result.last_state = live.state.value
                        # Non-flat stuck risk — stop this account.
                        if not self._remote_is_flat(client, account.address):
                            break
                        # Flat but uncleared — still stop to avoid loop.
                        break
                    # Recoverable: skip market, keep farming.
                    if _is_recoverable_round_error(err):
                        continue
                    result.errors.append(f"Round {round_index}: {err}")
                    break
                if round_index < planned:
                    pause = between_rounds_delay(
                        cancel_check=self._cancel_check,
                        style=session,
                        fallback=self.farm.sleep_between_rounds,
                    )
                    self.log(
                        f"[•] {account.label} | WAIT · {pause:.1f}s до следующего round"
                    )

            stuck = self.store.get_active_cycle(account.address)
            # Working session = any completed rounds and no stuck risk cycle.
            # Full plan is ideal; partial volume is still success for the farm.
            result.success = (
                result.completed_trades > 0
                and stuck is None
            )
            if (
                result.success
                and result.completed_trades < planned
                and result.errors
            ):
                # Soft failures that we continued past shouldn't flip fail.
                result.errors = [
                    item
                    for item in result.errors
                    if not _is_recoverable_round_error(item)
                ]
            if result.completed_trades >= planned and stuck is None:
                result.success = True
                result.errors.clear()
        except (
            AccountError,
            ProxyError,
            SignerError,
            OnchainError,
            StoreError,
            WorkflowError,
            FairgroundAPIError,
            ValidationError,
            FarmError,
        ) as exc:
            # Last-ditch: if we already printed volume, don't lose success flag
            # later in finally based only on this exception.
            if result.completed_trades == 0 and _looks_like_not_waitlisted(exc):
                result.waitlisted = False
                result.last_state = "NOT_WAITLISTED"
                result.success = True
                result.errors.clear()
                self.log(
                    f"[•] {account.label} | не в waitlist · test USDC=0 · "
                    "к торговле не допущен"
                )
            elif result.completed_trades == 0:
                result.errors.append(str(exc))
                self.log(f"[-] {account.label} | error: {exc}")
            else:
                self.log(f"[-] {account.label} | error: {exc}")
        except Exception as exc:  # safety net — never leak secrets
            if result.completed_trades == 0:
                result.errors.append(f"unexpected: {type(exc).__name__}")
            self.log(f"[-] {account.label} | unexpected error: {type(exc).__name__}: {exc}")
        finally:
            result.volume_notional_estimate = format(volume.normalize(), "f")
            try:
                active_after = self.store.get_active_cycle(account.address)
            except Exception:
                active_after = None
            should_pause = (
                not self.dry_run
                and result.success
                and result.completed_trades > 0
            )
            if should_pause:
                after = after_account_delay(
                    cancel_check=self._cancel_check,
                    style=session,
                    fallback=self.farm.sleep_after_account,
                )
                self.log(
                    f"[•] {account.label} | DONE · rounds={result.completed_trades}/"
                    f"{planned} · success={result.success} · wait={after:.1f}s"
                )
            else:
                self.log(
                    f"[•] {account.label} | DONE · rounds={result.completed_trades}/"
                    f"{planned} · success={result.success}"
                )
        return result

    def run(self) -> list[AccountFarmResult]:
        # Local single-operator console: free leases left by Ctrl+C / crash so
        # HOLDING recovery is not blocked by a ghost lease.
        try:
            purged = self.store.purge_expired_leases()
            stolen = 0
            for account in self.accounts:
                if self.store.force_release_account_lease(account.address):
                    stolen += 1
            if purged or stolen:
                self.log(
                    f"[•] lease cleanup · expired={purged} · "
                    f"account_reset={stolen}"
                )
        except Exception:
            pass
        wallets = list(self.accounts)
        if self.farm.shuffle_wallets:
            shuffle(wallets)
        effective_workers = min(len(wallets), self.farm.threads)
        if self.recovery_only:
            market_label = "RECOVERY ONLY"
        elif self._catalog is not None:
            market_label = ",".join(self._catalog.names)
        elif self.farm.markets:
            market_label = ",".join(self.farm.markets)
        else:
            market_label = self.farm.market_name
        if self.recovery_only:
            self.log(
                f"[•] LIVE START · {market_label} · "
                f"accounts={len(wallets)} · workers={effective_workers} · "
                "new_entries=DISABLED"
            )
        else:
            self.log(
                f"[•] LIVE START · markets={market_label} · "
                f"accounts={len(wallets)} · workers={effective_workers} · "
                f"rounds={self.farm.trades_count[0]}–{self.farm.trades_count[1]}"
            )
        if self._startup_plan is not None:
            self.log(f"[✓] GLOBAL MARKET CHECK · {self._startup_plan.summary()}")
        results: list[AccountFarmResult] = []
        stagger_lo, stagger_hi = 0.4, 4.8

        done_count = 0
        total = len(wallets)
        if total == 1:
            try:
                results.append(self._run_one_account(wallets[0]))
            except Exception:
                account = wallets[0]
                results.append(
                    AccountFarmResult(
                        account=account.address,
                        label=account.label,
                        planned_trades=0,
                        completed_trades=0,
                        success=False,
                        errors=["worker crashed"],
                    )
                )
            self._log_live_finish(results)
            return results
        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = {}
            for index, account in enumerate(wallets):
                if index > 0 and stagger_hi > 0:
                    delay = uniform(stagger_lo, stagger_hi)
                    self.sleep(delay)
                futures[pool.submit(self._run_one_account, account)] = account
                if (index + 1) % max(1, effective_workers) == 0 or index + 1 == total:
                    self.log(
                        f"[•] QUEUE · submitted {index + 1}/{total} · "
                        f"live_workers≤{effective_workers}"
                    )
            for future in as_completed(futures):
                try:
                    item = future.result()
                    results.append(item)
                except Exception as exc:
                    account = futures[future]
                    item = AccountFarmResult(
                        account=account.address,
                        label=account.label,
                        planned_trades=0,
                        completed_trades=0,
                        success=False,
                        errors=[f"worker crashed: {type(exc).__name__}"],
                    )
                    results.append(item)
                done_count += 1
                if done_count == total or done_count % max(1, effective_workers) == 0:
                    ok_so_far = sum(1 for row in results if row.success)
                    self.log(
                        f"[•] PROGRESS · finished {done_count}/{total} · "
                        f"ok={ok_so_far} · fail={done_count - ok_so_far}"
                    )
        self._log_live_finish(results)
        return results
