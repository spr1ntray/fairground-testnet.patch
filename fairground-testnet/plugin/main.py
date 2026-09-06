from __future__ import annotations

import random
import sys
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from eth_account import Account
from soft_hub.sdk import CancelledError, HubAccount, HubContext

from plugin.adspower import (
    AdsPowerClient,
    AdsPowerError,
    find_duplicate_profile_accounts,
    normalize_api_key,
    normalize_profile_id,
)
from plugin.fairground_bot.accounts import FarmAccount
from plugin.fairground_bot.adspower_faucet import AdsPowerFaucetConfig, run_arbitrum_sepolia_faucet
from plugin.fairground_bot.config import Settings
from plugin.fairground_bot.client import FairgroundAPIError
from plugin.fairground_bot.farm import FarmConfig, FarmError, VolumeFarm
from plugin.fairground_bot.human_log import redact
from plugin.fairground_bot.identity import BrowserIdentity, bank_identities, resolve_identity
from plugin.fairground_bot.operations import FORCE_FLATTEN_ACK, WalletOperations
from plugin.fairground_bot.proxy import parse_proxy
from plugin.fairground_bot.state import StateStore
from plugin.fairground_bot.timing import parse_account_gap, roll_session
from plugin.fairground_bot.validation import normalize_address

SUPPORTED_OS = frozenset({"darwin"})
CHAIN_ID = 421614
ETH_WEI = Decimal(10) ** 18
USDC_UNITS = Decimal(10) ** 6
CRYPTO_MARKETS = (
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "XRP-USD",
    "HYPE-USD",
    "NEAR-USD",
    "ZEC-USD",
    "LIT-USD",
)
WRITE_ACTIONS = frozenset({"farm", "flatten", "faucet"})


class HubHumanLog:
    def __init__(self, context: HubContext, account_id: str | None = None) -> None:
        self.context = context
        self.account_id = account_id

    def _emit(self, message: object, *, level: str = "info") -> None:
        self.context.log(redact(message), level=level, account_id=self.account_id)

    def info(self, message: object, *, scope: str = "SYSTEM") -> None:
        self._emit(message)

    def action(self, message: object, *, scope: str = "SYSTEM") -> None:
        self._emit(message)

    def success(self, message: object, *, scope: str = "SYSTEM") -> None:
        self._emit(message, level="success")

    def warning(self, message: object, *, scope: str = "SYSTEM") -> None:
        self._emit(message, level="warning")

    def error(self, message: object, *, scope: str = "SYSTEM") -> None:
        self._emit(message, level="error")

    def __call__(self, message: object) -> None:
        self._emit(message)


def run(context: HubContext) -> dict[str, Any]:
    if sys.platform not in SUPPORTED_OS:
        raise RuntimeError("Этот софт собран для macOS")
    if context.action_id not in {"inspect", "farm", "flatten", "faucet"}:
        raise ValueError("Неизвестное действие")

    options = _options(context)
    _protect_all(context)
    identities: dict[str, BrowserIdentity] = {}
    blocked: set[str] = set()
    if context.action_id in {"farm", "faucet", "inspect"}:
        blocked, identities = _resolve_identities(
            context, required=context.action_id == "faucet"
        )

    counters = {
        "total": len(context.accounts),
        "succeeded": 0,
        "partial": 0,
        "failed": 0,
        "blocked": len(blocked),
        "cancelled": 0,
        "transactions": 0,
    }
    lock = threading.Lock()
    store = StateStore(Path(context.scratch_dir) / "state.sqlite3")

    context.log(
        "Старт Fairground",
        data={
            "действие": context.action_id,
            "аккаунтов": len(context.accounts),
            "одновременно": context.account_concurrency,
            "сеть": CHAIN_ID,
        },
    )

    def worker(account: HubAccount) -> str:
        if account.id in blocked:
            return "blocked"
        try:
            status = _run_account(
                context,
                account,
                options=options,
                store=store,
                identity=identities.get(account.id),
            )
        except CancelledError:
            with lock:
                counters["cancelled"] += 1
            _terminal(context, account, "cancelled", "cancelled", "Остановлено")
            raise
        except Exception as exc:
            with lock:
                counters["failed"] += 1
            detail = str(exc).strip() or type(exc).__name__
            context.log(
                f"{account.label}: {type(exc).__name__}: {detail}",
                level="error",
                account_id=account.id,
            )
            _terminal(
                context,
                account,
                "failed",
                "failed",
                detail[:180],
                {"error": type(exc).__name__},
            )
            return "failed"
        with lock:
            counters[status] = counters.get(status, 0) + 1
        return status

    queue = [account for account in context.accounts if account.id not in blocked]
    random.shuffle(queue)
    batch_size = max(1, int(context.account_concurrency))
    total_batches = (len(queue) + batch_size - 1) // batch_size if queue else 0
    if hasattr(context, "map_accounts"):
        for batch_index in range(0, len(queue), batch_size):
            context.check_cancelled()
            batch = tuple(queue[batch_index : batch_index + batch_size])
            number = batch_index // batch_size + 1
            labels = ", ".join(account.label for account in batch)
            context.log(
                f"Пачка {number}/{total_batches}: {len(batch)} аккаунтов — {labels}",
                data={"batch": number, "size": len(batch)},
            )
            context.map_accounts(worker, accounts=batch)
    else:
        for account in queue:
            context.check_cancelled()
            worker(account)
    return {
        "total": counters["total"],
        "succeeded": counters["succeeded"],
        "partial": counters["partial"],
        "failed": counters["failed"],
        "blocked": counters["blocked"],
        "cancelled": counters["cancelled"],
        "chain_id": CHAIN_ID,
        "action": context.action_id,
        "account_concurrency": context.account_concurrency,
    }


def _run_account(
    context: HubContext,
    hub: HubAccount,
    *,
    options: dict[str, Any],
    store: StateStore,
    identity: BrowserIdentity | None,
) -> str:
    context.check_cancelled()
    context.account_state(
        hub.id, status="running", stage="preflight", progress=0.06, message="Проверяю ключ и прокси"
    )
    farm_account = _farm_account(context, hub)
    settings = _settings()
    log = HubHumanLog(context, hub.id)
    if identity is not None:
        context.log(
            f"{hub.label}: отпечаток {identity.source} · {identity.platform} Chrome {identity.chrome_major}",
            account_id=hub.id,
            data=identity.summary(),
        )

    if context.action_id == "inspect":
        parse_account_gap(cancel_check=context.check_cancelled)
        context.account_state(hub.id, status="running", stage="inspect", progress=0.42, message="Читаю портфель")
        ops = WalletOperations(settings=settings, store=store, log=log)
        if identity is not None:
            ops.bind_identity(farm_account.address, identity)
        overview = ops.scan(farm_account)
        farm_account.wipe_secret()
        data = _overview_data(overview)
        status = "failed" if overview.error else "succeeded"
        if overview.waitlisted is False and not overview.error:
            status = "blocked"
        context.result(
            f"{hub.label}: портфель",
            kind="account_snapshot",
            status=status,
            account_id=hub.id,
            data=data,
        )
        if overview.waitlisted is False and not overview.error:
            message = "Не в waitlist"
        else:
            message = overview.error or f"USDC {data['usdc']} · объём {data['volume']}"
        _terminal(context, hub, status, "completed" if status == "succeeded" else "inspect_failed", message)
        return status

    if context.action_id == "faucet":
        context.account_state(hub.id, status="running", stage="faucet", progress=0.2, message="Открываю кран в AdsPower")
        claimed = _run_faucet(context, hub, farm_account, log)
        farm_account.wipe_secret()
        context.result(
            f"{hub.label}: кран",
            kind="account_summary",
            status="succeeded",
            account_id=hub.id,
            data={"style": "faucet", "faucet": claimed},
        )
        _terminal(context, hub, "succeeded", "completed", "Кран отработал" if claimed else "Кран пропущен")
        return "succeeded"

    if context.action_id == "flatten":
        context.account_state(hub.id, status="running", stage="flatten", progress=0.28, message="Закрываю позиции")
        ops = WalletOperations(settings=settings, store=store, log=log)
        if identity is not None:
            ops.bind_identity(farm_account.address, identity)
        results = ops.force_flatten_all([farm_account], acknowledge=FORCE_FLATTEN_ACK)
        row = results[0] if results else None
        ok = bool(row and row.success)
        farm_account.wipe_secret()
        context.result(
            f"{hub.label}: закрытие",
            kind="account_summary",
            status="succeeded" if ok else "partial",
            account_id=hub.id,
            data={
                "style": "flatten",
                "transactions": int(row.chain_transactions) if row else 0,
            },
        )
        _terminal(
            context,
            hub,
            "succeeded" if ok else "partial",
            "completed",
            row.message if row else "Нет результата закрытия",
        )
        return "succeeded" if ok else "partial"

    context.account_state(hub.id, status="running", stage="session", progress=0.16, message="HTTP-сделки ключом Hub")

    style = roll_session(farm_account.address, str(context.run_id))
    family = style.family
    context.log(
        f"{hub.label}: стиль {family}",
        account_id=hub.id,
        data=style.public(),
    )

    farm_cfg = _farm_config(options)
    engine = VolumeFarm(
        settings=settings,
        store=store,
        farm=farm_cfg,
        accounts=[farm_account],
        log=log,
        sleep=lambda seconds: _sleep(seconds, context.check_cancelled),
    )
    if identity is not None:
        engine.bind_identity(farm_account.address, identity)
    engine.bind_cancel(context.check_cancelled)
    engine.bind_run_id(str(context.run_id))
    completed = 0
    planned = 0
    volume = "0"
    http_ok = False
    row = None
    try:
        try:
            engine.preflight()
        except FairgroundAPIError as exc:
            context.log(
                f"{hub.label}: каталог рынков пока недоступен · {exc}",
                level="warning",
                account_id=hub.id,
            )
        context.account_state(hub.id, status="running", stage="trade", progress=0.48, message="HTTP open/close")
        rows = engine.run()
        row = rows[0] if rows else None
        completed = int(row.completed_trades) if row else 0
        planned = int(row.planned_trades) if row else 0
        volume = str(row.volume_notional_estimate) if row else "0"
        http_ok = bool(row and row.success)
    except (FarmError, FairgroundAPIError) as exc:
        context.log(str(exc), level="warning", account_id=hub.id)
        http_ok = False

    leftover = 0
    not_waitlisted = bool(row and row.waitlisted is False)
    if not not_waitlisted:
        try:
            flat_account = _farm_account(context, hub)
            ops = WalletOperations(settings=settings, store=store, log=log)
            if identity is not None:
                ops.bind_identity(flat_account.address, identity)
            flat = ops.force_flatten_all([flat_account], acknowledge=FORCE_FLATTEN_ACK)
            leftover = 0 if flat and flat[0].success else 1
            flat_account.wipe_secret()
        except Exception:
            leftover = 0 if completed > 0 else 1
    farm_account.wipe_secret()
    if not_waitlisted:
        context.result(
            f"{hub.label}: работа",
            kind="account_summary",
            status="blocked",
            account_id=hub.id,
            data={
                "rounds": 0,
                "volume": "0",
                "style": family,
                "planned": planned,
                "waitlist": False,
            },
        )
        _terminal(context, hub, "blocked", "waitlist", "Не в waitlist")
        return "blocked"
    ok = completed > 0 and leftover == 0 and http_ok
    partial = completed > 0 and not ok
    status = "succeeded" if ok else ("partial" if partial else "failed")
    context.result(
        f"{hub.label}: работа",
        kind="account_summary",
        status=status,
        account_id=hub.id,
        data={
            "rounds": completed,
            "volume": volume,
            "style": family,
            "planned": planned,
            "waitlist": True if row and row.waitlisted else None,
        },
    )
    message = f"кругов {completed}/{planned} · объём {volume}"
    if status == "succeeded":
        context.account_state(hub.id, status="succeeded", stage="completed", progress=1.0, message=message)
    else:
        context.account_state(hub.id, status=status, stage="farm_done", message=message)
    return status


def _run_faucet(
    context: HubContext,
    hub: HubAccount,
    farm_account: FarmAccount,
    log: Callable[[str], None],
) -> bool:
    try:
        api_key = context.settings.secret("adspower_api")
        profile_id = hub.secret("adspower_profile")
    except KeyError:
        context.log("Нет AdsPower для крана", level="warning", account_id=hub.id)
        return False
    _protect(context, api_key)
    _protect(context, profile_id)
    farm_account.adspower_profile_id = normalize_profile_id(profile_id)
    cfg = AdsPowerFaucetConfig(max_profiles_parallel=1)
    try:
        rows = run_arbitrum_sepolia_faucet(accounts=[farm_account], api_key=api_key, config=cfg, log=log)
    except Exception as exc:
        context.log(f"Кран не удался: {type(exc).__name__}", level="warning", account_id=hub.id)
        return False
    return bool(rows and getattr(rows[0], "success", False))


def _farm_account(context: HubContext, hub: HubAccount) -> FarmAccount:
    private_key = str(hub.secret("evm_private_key")).strip()
    if private_key.startswith(("0x", "0X")):
        hex_key = private_key[2:]
    else:
        hex_key = private_key
    derived = Account.from_key(bytes.fromhex(hex_key)).address
    if hub.evm_address and derived.lower() != hub.evm_address.lower():
        raise RuntimeError("Ключ не совпадает с адресом в Hub")
    proxy = parse_proxy(hub.secret("proxy"))
    _protect(context, private_key)
    _protect(context, hex_key)
    _protect(context, proxy.url)
    address = normalize_address(derived)
    profile_id = ""
    try:
        profile_id = normalize_profile_id(hub.secret("adspower_profile"))
    except KeyError:
        profile_id = ""
    return FarmAccount(
        index=1,
        address=address,
        proxy=proxy,
        label=hub.label,
        private_key_hex=private_key,
        adspower_profile_id=profile_id,
    )


def _settings() -> Settings:
    return Settings(
        api_url="https://api.fairground.fi",
        rpc_url="https://sepolia-rollup.arbitrum.io/rpc",
        chain_id=CHAIN_ID,
        timeout_seconds=20,
        max_margin_usdc=Decimal("100000"),
        max_notional_usdc=Decimal("2000000"),
        max_leverage=Decimal("50"),
    )


def _farm_config(options: dict[str, Any]) -> FarmConfig:
    return FarmConfig(
        market_id="2607855276726473749",
        market_name="ETH-USD",
        trades_count=(int(options["trades_from"]), int(options["trades_to"])),
        hold_seconds=(int(options["hold_from"]), int(options["hold_to"])),
        threads=1,
        shuffle_wallets=True,
        allow_long=True,
        allow_short=True,
        entry_offset_bps=200,
        exit_slippage_bps=200,
        sleep_between_rounds=(2, 12),
        sleep_after_account=(2, 10),
        markets=CRYPTO_MARKETS,
        leverage_levels=("MEDIUM", "HIGH"),
        margin_mode="PERCENT",
        margin_percent=(str(options["margin_from"]), str(options["margin_to"])),
    )


def _sleep(seconds: float, cancel: Callable[[], None]) -> None:
    from plugin.fairground_bot.timing import sleep_jitter

    sleep_jitter(max(0.0, float(seconds)), max(0.0, float(seconds)), cancel_check=cancel)


def _options(context: HubContext) -> dict[str, Any]:
    raw = dict(context.options or {})

    def integer(name: str, default: int, low: int, high: int) -> int:
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} должен быть целым")
        if value < low or value > high:
            raise ValueError(f"{name} вне диапазона")
        return value

    trades_from = integer("trades_from", 8, 1, 40)
    trades_to = integer("trades_to", 16, 1, 40)
    if trades_from > trades_to:
        raise ValueError("Диапазон кругов перевёрнут")
    margin_from = integer("margin_from", 12, 5, 90)
    margin_to = integer("margin_to", 48, 5, 90)
    if margin_from > margin_to:
        raise ValueError("Диапазон маржи перевёрнут")
    hold_from = integer("hold_from", 8, 3, 180)
    hold_to = integer("hold_to", 55, 3, 180)
    if hold_from > hold_to:
        raise ValueError("Диапазон удержания перевёрнут")
    return {
        "trades_from": trades_from,
        "trades_to": trades_to,
        "margin_from": margin_from,
        "margin_to": margin_to,
        "hold_from": hold_from,
        "hold_to": hold_to,
    }


def _overview_data(overview: Any) -> dict[str, Any]:
    eth = _fmt_decimal(Decimal(overview.eth_balance_wei) / ETH_WEI)
    usdc = _fmt_decimal(Decimal(overview.collateral_balance_units) / USDC_UNITS)
    return {
        "eth": eth,
        "usdc": usdc,
        "volume": str(overview.volume_usdc or "0"),
        "trades": int(overview.total_trades or overview.fills or 0),
        "positions": int(overview.open_positions),
        "orders": int(overview.active_orders),
        "waitlist": overview.waitlisted,
    }


def _fmt_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def _identity_seed(account: HubAccount) -> str:
    addr = (getattr(account, "evm_address", None) or "").strip()
    return addr.lower() if addr else str(account.id)


def _block_account(context: HubContext, account: HubAccount, message: str) -> None:
    context.account_state(account.id, status="blocked", stage="preflight_blocked", message=message)
    context.log(message, level="warning", account_id=account.id)


def _resolve_identities(
    context: HubContext, *, required: bool
) -> tuple[set[str], dict[str, BrowserIdentity]]:
    blocked: set[str] = set()
    identities: dict[str, BrowserIdentity] = {}
    pairs: list[tuple[str, str]] = []
    for account in context.accounts:
        context.check_cancelled()
        try:
            profile_id = normalize_profile_id(account.secret("adspower_profile"))
        except KeyError:
            profile_id = ""
        if hasattr(context, "protect_secret") and len(profile_id) >= 4:
            try:
                context.protect_secret(profile_id)
            except Exception:
                pass
        if len(profile_id) < 4:
            if required:
                _block_account(context, account, "Нет AdsPower профиля")
                blocked.add(account.id)
            continue
        pairs.append((account.id, profile_id))

    for group in find_duplicate_profile_accounts(pairs):
        for account in context.accounts:
            if account.id in group:
                _block_account(context, account, "Один AdsPower-профиль нельзя назначать двум аккаунтам")
                blocked.add(account.id)

    try:
        api_key = normalize_api_key(context.settings.secret("adspower_api"))
    except KeyError:
        api_key = ""
    if hasattr(context, "protect_secret") and len(api_key or "") >= 4:
        try:
            context.protect_secret(api_key)
        except Exception:
            pass
    if len(api_key) < 4:
        if required:
            for account in context.accounts:
                if account.id not in blocked:
                    _block_account(context, account, "Нет AdsPower API key в настройках Hub")
                    blocked.add(account.id)
            return blocked, identities
        _fill_bank_identities(context, blocked, identities)
        return blocked, identities

    ads: AdsPowerClient | None = None
    by_id = dict(pairs)
    try:
        ads = AdsPowerClient(api_key)
        ads.health()
        context.log("AdsPower Local API доступен")
    except AdsPowerError:
        if required:
            for account in context.accounts:
                if account.id not in blocked:
                    _block_account(context, account, "AdsPower Local API недоступен — запусти AdsPower")
                    blocked.add(account.id)
            return blocked, identities
        context.log("AdsPower недоступен — беру локальный отпечаток", level="warning")
        _fill_bank_identities(context, blocked, identities)
        return blocked, identities

    try:
        for account in context.accounts:
            context.check_cancelled()
            if account.id in blocked:
                continue
            profile_id = by_id.get(account.id, "")
            if not profile_id:
                continue
            try:
                row = ads.get_profile(profile_id)
            except AdsPowerError as exc:
                if required:
                    _block_account(context, account, str(exc) or "AdsPower не нашёл профиль")
                    blocked.add(account.id)
                continue
            ident = resolve_identity(_identity_seed(account), row)
            if ident.source == "ads":
                identities[account.id] = ident
    finally:
        if ads is not None:
            ads.close()
    if not required:
        _fill_bank_identities(context, blocked, identities)
    return blocked, identities


def _fill_bank_identities(
    context: HubContext,
    blocked: set[str],
    identities: dict[str, BrowserIdentity],
) -> None:
    pending: list[tuple[str, str]] = []
    for account in context.accounts:
        if account.id in blocked or account.id in identities:
            continue
        pending.append((account.id, _identity_seed(account)))
    if not pending:
        return
    by_seed = bank_identities([seed for _, seed in pending])
    for account_id, seed in pending:
        identities[account_id] = by_seed[seed]


def _protect(context: HubContext, value: str | None) -> None:
    if not value or not hasattr(context, "protect_secret"):
        return
    try:
        context.protect_secret(str(value))
    except Exception:
        return


def _protect_all(context: HubContext) -> None:
    for account in context.accounts:
        for kind in ("evm_private_key", "proxy", "adspower_profile"):
            try:
                _protect(context, account.secret(kind))
            except KeyError:
                continue
    try:
        _protect(context, context.settings.secret("adspower_api"))
    except KeyError:
        return


def _terminal(
    context: HubContext,
    account: HubAccount,
    status: str,
    stage: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    kwargs: dict[str, Any] = {"status": status, "stage": stage, "message": message}
    if status == "succeeded":
        kwargs["progress"] = 1.0
    context.account_state(account.id, **kwargs)
    if data:
        context.log(message, account_id=account.id, data=data)
