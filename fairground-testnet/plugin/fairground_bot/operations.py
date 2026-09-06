"""Read-only wallet parsing and crash-safe tracked emergency flattening."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from .accounts import FarmAccount
from .client import FairgroundAPIError, FairgroundClient
from .identity import BrowserIdentity
from .config import Settings
from .human_log import HumanLogger, redact
from .onchain import OnchainError, OnchainExecutor, SimulationError, to_base_units
from .signer import load_private_key_hex
from .state import CycleRecord, CycleState, StateStore
from .validation import normalize_market_id
from .waitlist import admission_from_balances
from .workflow import (
    CycleRunner,
    OrderView,
    PositionView,
    RemoteSnapshot,
    entry_gas_reserve_wei,
)


FORCE_FLATTEN_ACK = "I_UNDERSTAND_FORCE_FLATTEN"
_UNRESOLVED_ACTIONS = {"SIGNED", "BROADCAST", "UNKNOWN"}
_DIRECT_FLAT_RESOLUTION = {
    CycleState.BLOCKED_FUNDS,
    CycleState.DUST_BLOCKED,
    CycleState.PAUSED,
    CycleState.QUARANTINED,
}
_PRE_ENTRY_STATES = {
    CycleState.PREFLIGHT,
    CycleState.FUNDS_CHECK,
    CycleState.APPROVAL_PENDING,
    CycleState.ENTRY_ARMED,
    CycleState.BLOCKED_FUNDS,
}


class OperationsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WalletOverview:
    label: str
    address: str
    eth_balance_wei: int
    collateral_balance_units: int
    allowance_units: int
    active_orders: int
    open_positions: int
    fills: int
    cycle_state: str
    kill_mode: str | None
    gas_required_wei: int | None = None
    gas_shortfall_wei: int | None = None
    gas_action: str | None = None
    gas_context: str | None = None
    error: str | None = None
    volume_usdc: str = "0"
    total_trades: int = 0
    total_fees: str = "0"
    waitlisted: bool | None = None


@dataclass(frozen=True, slots=True)
class FlattenPreview:
    label: str
    address: str
    active_orders: int
    open_positions: int
    cycle_state: str
    kill_mode: str | None
    unresolved_actions: int = 0
    unresolved_statuses: tuple[str, ...] = ()
    replacement_safe: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FlattenResult:
    label: str
    address: str
    success: bool
    status: str
    message: str
    chain_transactions: int = 0


@dataclass(frozen=True, slots=True)
class _AddressOnlySigner:
    """Makes all parser RPC reads account-bound without exposing a key."""

    address: str

    def sign_transaction(self, _transaction: dict[str, Any]) -> Any:
        raise OperationsError("Read-only parser cannot sign transactions")


class WalletOperations:
    def __init__(
        self,
        *,
        settings: Settings,
        store: StateStore,
        log: HumanLogger,
    ) -> None:
        self.settings = settings
        self.store = store
        self.log = log
        self._identities: dict[str, BrowserIdentity] = {}

    def bind_identity(self, address: str, identity: BrowserIdentity) -> None:
        self._identities[address.lower()] = identity

    def _client(self, account: FarmAccount) -> FairgroundClient:
        identity = self._identities.get(account.address.lower())
        return FairgroundClient(
            self.settings.api_url,
            timeout_seconds=self.settings.timeout_seconds,
            proxy_url=account.proxy.url,
            identity=identity,
            chain_id=self.settings.chain_id,
        )

    def _read_chain(
        self,
        account: FarmAccount,
        client: FairgroundClient,
    ) -> OnchainExecutor:
        return OnchainExecutor(
            rpc_url=self.settings.rpc_url,
            client=client,
            signer=_AddressOnlySigner(account.address),  # type: ignore[arg-type]
            timeout_seconds=self.settings.timeout_seconds,
            proxy_url=account.proxy.url,
        )

    def _write_chain(
        self,
        account: FarmAccount,
        client: FairgroundClient,
    ) -> OnchainExecutor:
        if not account.private_key_hex:
            raise OperationsError("Нет signer для принудительного закрытия")
        signer = load_private_key_hex(
            account.private_key_hex,
            expected_address=account.address,
        )
        # Keep exactly one signer/executor for the remainder of this recovery
        # workflow and discard the decrypted vault string immediately.
        account.wipe_secret()
        return OnchainExecutor(
            rpc_url=self.settings.rpc_url,
            client=client,
            signer=signer,
            timeout_seconds=self.settings.timeout_seconds,
            proxy_url=account.proxy.url,
        )

    @staticmethod
    def _remote(client: FairgroundClient, address: str) -> RemoteSnapshot:
        orders_raw = client.get_orders(address).get("orders")
        positions_raw = client.get_open_positions(address).get("positions")
        if not isinstance(orders_raw, list) or any(
            not isinstance(item, dict) for item in orders_raw
        ):
            raise FairgroundAPIError("Fairground вернул некорректный список ордеров")
        if not isinstance(positions_raw, list) or any(
            not isinstance(item, dict) for item in positions_raw
        ):
            raise FairgroundAPIError("Fairground вернул некорректный список позиций")
        orders = tuple(OrderView.from_api(item) for item in orders_raw)
        if any(
            order.owner is not None and order.owner != address
            for order in orders
        ):
            raise FairgroundAPIError("Fairground вернул ордер другого аккаунта")
        positions = tuple(PositionView.from_api(item) for item in positions_raw)
        return RemoteSnapshot(orders=orders, positions=positions)

    @staticmethod
    def _fill_count(payload: dict[str, Any]) -> int:
        fills = payload.get("fills")
        if not isinstance(fills, list):
            raise FairgroundAPIError("Fairground вернул некорректную историю сделок")
        return len(fills)

    def scan(self, account: FarmAccount) -> WalletOverview:
        """Parser: deployment, balances, exposure, fills and local controls."""

        try:
            client = self._client(account)
            chain = self._read_chain(account, client)
            snapshot = chain.verify_deployment()
            remote = self._remote(client, account.address)
            fills = self._fill_count(client.get_user_fills(account.address, limit=100))
            volume_usdc = "0"
            total_trades = fills
            total_fees = "0"
            try:
                portfolio = client.get_portfolio(account.address).get("portfolio")
                if isinstance(portfolio, dict):
                    volume_usdc = str(portfolio.get("totalVolume") or "0")
                    raw_trades = portfolio.get("totalTrades")
                    if raw_trades not in (None, ""):
                        total_trades = int(str(raw_trades))
                    total_fees = str(portfolio.get("totalFees") or "0")
            except (FairgroundAPIError, TypeError, ValueError):
                pass
            active = self.store.get_active_cycle(account.address)
            kill = self.store.get_kill(account.address)
            required: int | None = None
            gas_action: str | None = None
            gas_context: str | None = None
            if active is not None:
                if active.state is CycleState.PAUSED:
                    raw_required = active.runtime.get("close_gas_required_wei")
                    if isinstance(raw_required, int) and raw_required > 0:
                        required = raw_required
                        gas_action = self._paused_gas_action(active)
                        gas_context = "persisted_close_requirement"
                elif active.state is CycleState.BLOCKED_FUNDS:
                    required = self._fresh_open_requirement(active, chain)
                    if required is not None:
                        gas_action = "OPEN"
                        gas_context = "fresh_open_simulation"
                    else:
                        raw_required = active.runtime.get("gas_required_wei")
                        if isinstance(raw_required, int) and raw_required > 0:
                            required = raw_required
                            gas_action = (
                                "APPROVAL"
                                if "approval_gas_limit" in active.runtime
                                and "entry_gas_limit" not in active.runtime
                                else "OPEN"
                            )
                            gas_context = "persisted_entry_requirement"
                else:
                    raw_required = active.runtime.get("gas_required_wei")
                    if isinstance(raw_required, int) and raw_required > 0:
                        required = raw_required
                        gas_action = "OPEN"
                        gas_context = "persisted_entry_requirement"
                    else:
                        required = self._fresh_open_requirement(active, chain)
                        if required is not None:
                            gas_action = "OPEN"
                            gas_context = "fresh_open_simulation"
            shortfall = (
                max(0, required - snapshot.eth_balance_wei)
                if required is not None
                else None
            )
            admission = admission_from_balances(
                usdc_units=snapshot.collateral_balance_units,
                positions=len(remote.positions),
                orders=len(remote.active_orders),
                fills=fills,
                volume_usdc=volume_usdc,
            )
            return WalletOverview(
                label=account.label,
                address=account.address,
                eth_balance_wei=snapshot.eth_balance_wei,
                collateral_balance_units=snapshot.collateral_balance_units,
                allowance_units=snapshot.allowance_units,
                active_orders=len(remote.active_orders),
                open_positions=len(remote.positions),
                fills=fills,
                volume_usdc=volume_usdc,
                total_trades=total_trades,
                total_fees=total_fees,
                waitlisted=admission.admitted,
                cycle_state=active.state.value if active is not None else "FLAT",
                kill_mode=kill.get("kill_mode") if kill else None,
                gas_required_wei=required,
                gas_shortfall_wei=shortfall,
                gas_action=gas_action,
                gas_context=gas_context,
            )
        except Exception as exc:
            return WalletOverview(
                label=account.label,
                address=account.address,
                eth_balance_wei=0,
                collateral_balance_units=0,
                allowance_units=0,
                active_orders=0,
                open_positions=0,
                fills=0,
                volume_usdc="0",
                total_trades=0,
                total_fees="0",
                cycle_state="ERROR",
                kill_mode=None,
                error=redact(f"{type(exc).__name__}: {exc}"),
            )

    @staticmethod
    def _fresh_open_requirement(
        active: CycleRecord,
        chain: OnchainExecutor,
    ) -> int | None:
        if not all(
            key in active.runtime
            for key in ("margin_units", "notional_units", "limit_price_units")
        ):
            return None
        try:
            prepared = chain.build_open(
                market_id=int(active.intent["marketId"]),
                margin_units=int(active.runtime["margin_units"]),
                notional_units=int(active.runtime["notional_units"]),
                is_long=str(active.intent.get("side", "")).lower() == "long",
                limit_price_units=int(active.runtime["limit_price_units"]),
            )
        except Exception:
            # Parsing must remain useful when a read-only simulation is unavailable.
            return None
        return entry_gas_reserve_wei(prepared)

    @staticmethod
    def _paused_gas_action(active: CycleRecord) -> str:
        persisted = str(active.runtime.get("close_gas_action", "")).lower()
        if persisted in {"exit", "cancel_entry", "cancel_exit"}:
            return persisted.upper()
        resume_state = str(active.runtime.get("resume_state", ""))
        if resume_state in {
            CycleState.ENTRY_PENDING.value,
            CycleState.ENTRY_PARTIAL.value,
        }:
            return "CANCEL_ENTRY"
        if resume_state in {
            CycleState.EXIT_PENDING.value,
            CycleState.EXIT_PARTIAL.value,
        }:
            return "CANCEL_EXIT"
        if resume_state == CycleState.EXIT_ARMED.value:
            return "EXIT"
        return "CLOSE"

    def preview_flatten(self, account: FarmAccount) -> FlattenPreview:
        try:
            client = self._client(account)
            remote = self._remote(client, account.address)
            active = self.store.get_active_cycle(account.address)
            kill = self.store.get_kill(account.address)
            unresolved = self._unresolved_statuses(active)
            replacement_safe = bool(
                not remote.active_orders
                and not remote.positions
                and not unresolved
                and (
                    active is None
                    or self.store.is_provably_pre_entry(active)
                )
            )
            return FlattenPreview(
                label=account.label,
                address=account.address,
                active_orders=len(remote.active_orders),
                open_positions=len(remote.positions),
                cycle_state=active.state.value if active is not None else "FLAT",
                kill_mode=kill.get("kill_mode") if kill else None,
                unresolved_actions=len(unresolved),
                unresolved_statuses=unresolved,
                replacement_safe=replacement_safe,
            )
        except Exception as exc:
            return FlattenPreview(
                label=account.label,
                address=account.address,
                active_orders=0,
                open_positions=0,
                cycle_state="ERROR",
                kill_mode=None,
                error=redact(f"{type(exc).__name__}: {exc}"),
            )

    def _unresolved(self, record: CycleRecord) -> bool:
        return bool(self._unresolved_statuses(record))

    def _unresolved_statuses(
        self,
        record: CycleRecord | None,
    ) -> tuple[str, ...]:
        if record is None:
            return ()
        return tuple(
            f"{action.kind}:{action.status}"
            for action in self.store.actions_for_cycle(record.cycle_id)
            if action.status in _UNRESOLVED_ACTIONS
        )

    def _provably_pre_entry(self, record: CycleRecord) -> bool:
        """Only permit signer-free ABORT when the ledger proves no entry landed."""
        return self.store.is_provably_pre_entry(record)

    def preview_vault_replacement(self, account: FarmAccount) -> FlattenPreview:
        """Signerless proof used before an old account may leave the vault."""
        preview = self.preview_flatten(account)
        if preview.error or not self.store.account_has_unresolved_actions(
            account.address
        ):
            return preview
        return replace(
            preview,
            unresolved_actions=max(1, preview.unresolved_actions),
            unresolved_statuses=(
                *preview.unresolved_statuses,
                "history:UNRESOLVED",
            ),
            replacement_safe=False,
        )

    def _resolve_proven_flat(
        self,
        account: FarmAccount,
        client: FairgroundClient,
    ) -> CycleRecord | None:
        """Re-prove flat under lease, then close eligible local state signer-free."""

        lease = self.store.acquire_lease(account.address)
        try:
            record = self.store.get_active_cycle(account.address)
            if record is None:
                return None
            if record.state not in _DIRECT_FLAT_RESOLUTION:
                return record
            remote = self._remote(client, account.address)
            if (
                remote.active_orders
                or remote.positions
                or self._unresolved(record)
                or not self._provably_pre_entry(record)
            ):
                return record
            for action in self.store.actions_for_cycle(record.cycle_id):
                if action.status == "PREPARED":
                    self.store.mark_action(action.action_id, "NOT_BROADCAST")
            return self.store.resolve_cycle_flat(
                record,
                reason="Force-close: Fairground API verified flat; no unresolved transaction",
            )
        finally:
            self.store.release_lease(lease)

    def _oracle_price(self, client: FairgroundClient, market_id: str) -> Decimal:
        payload = client.get_price(market_id)
        price_raw = payload.get("price")
        if not isinstance(price_raw, dict):
            raise OperationsError("oracle price missing")
        if normalize_market_id(price_raw.get("marketId")) != normalize_market_id(
            market_id
        ):
            raise OperationsError("oracle market mismatch")
        return Decimal(str(price_raw.get("price")))

    def _cancel_all_orders(
        self,
        *,
        account: FarmAccount,
        client: FairgroundClient,
        chain: OnchainExecutor,
    ) -> int:
        """Cancel every active order (entry + reduce). HAR shows residual reduce
        orders block a clean flat after a partial market close."""

        txs = 0
        remote = self._remote(client, account.address)
        for order in remote.active_orders:
            try:
                prepared = chain.build_cancel(
                    market_id=int(order.market_id),
                    order_id=int(order.order_id),
                    reduce_only=bool(order.reduce_only),
                )
                signed = chain.sign(prepared)
                tx_hash = chain.broadcast(signed)
                chain.wait_for_receipt(tx_hash, timeout_seconds=90, confirmations=1)
                txs += 1
                self.log.info(
                    f"cancel order {order.order_id} "
                    f"({'reduce' if order.reduce_only else 'entry'}) · {tx_hash[:12]}…",
                    scope=account.label,
                )
            except Exception as exc:
                self.log.warning(
                    f"cancel order {order.order_id}: {type(exc).__name__}: {exc}",
                    scope=account.label,
                )
        return txs

    def _wait_position_gone(
        self,
        *,
        client: FairgroundClient,
        account: FarmAccount,
        position_id: int,
        timeout_seconds: float = 45.0,
    ) -> bool:
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remote = self._remote(client, account.address)
            if not any(p.position_id == position_id for p in remote.positions):
                return True
            if not remote.active_orders and not remote.positions:
                return True
            time.sleep(2.0)
        return False

    def _direct_market_reduce_all(
        self,
        *,
        account: FarmAccount,
        client: FairgroundClient,
        chain: OnchainExecutor,
        tolerance_bps: int = 200,
    ) -> int:
        """Bypass cycle machine: UI-style market close (HAR: orderType=0 MANUAL
        + threshold ±2%, size = nominalSize * 10^sizeDecimals, via oracle multicall).
        """

        txs = 0
        # 1) Cancel residual reduce/entry orders so we can re-place cleanly.
        txs += self._cancel_all_orders(account=account, client=client, chain=chain)

        remote = self._remote(client, account.address)
        for position in list(remote.positions):
            try:
                market = chain.get_market_config(int(position.market_id))
                oracle = self._oracle_price(client, position.market_id)
                # HAR: UI Market ≈ protective MANUAL threshold ±2% of mark.
                tol = Decimal(max(200, tolerance_bps)) / Decimal(10_000)
                if position.side.upper() == "LONG":
                    # Close long = sell: threshold BELOW mark (HAR BTC close).
                    threshold = oracle * (Decimal(1) - tol)
                    rounding = ROUND_FLOOR
                else:
                    # Close short = buy: threshold ABOVE mark (HAR SOL close 76.61 vs 75.11).
                    threshold = oracle * (Decimal(1) + tol)
                    rounding = ROUND_CEILING
                tick = int(market.tick_decimals)
                scale = Decimal(10) ** tick
                price_units = int(
                    (threshold * scale).to_integral_value(rounding=rounding)
                )
                if price_units <= 0:
                    raise OperationsError("bad market threshold")
                size_decimals = int(market.size_decimals)
                # HAR: reduceSize raw = nominalSize * 10^sizeDecimals (SOL: *1e9).
                size_units = to_base_units(
                    position.nominal_size, size_decimals, bits=56
                )
                lot = int(market.lot_size_raw or 0)
                if lot > 0 and size_units >= lot:
                    size_units = (size_units // lot) * lot
                if size_units <= 0:
                    self.log.warning(
                        f"skip dust pos={position.position_id} "
                        f"size={position.nominal_size} dec={size_decimals}",
                        scope=account.label,
                    )
                    continue
                if lot > 0 and size_units < lot:
                    self.log.warning(
                        f"pos={position.position_id} below lot · "
                        f"units={size_units} lot={lot}",
                        scope=account.label,
                    )
                    continue
                human_px = format(
                    (Decimal(price_units) / scale).normalize(), "f"
                )
                self.log.action(
                    f"MARKET CLOSE pos={position.position_id} · {position.side} · "
                    f"size={position.nominal_size} · units={size_units} · "
                    f"dec={size_decimals} · tol={tolerance_bps}bps · "
                    f"px={human_px} · orderType=MANUAL(0)",
                    scope=account.label,
                )
                # HAR: orderType=0 MANUAL + market-band threshold (±2%).
                # If full size reverts (race / fee haircut), retry with size-1 lot.
                size_try = size_units
                last_exc: Exception | None = None
                for shrink in range(3):
                    try:
                        prepared = chain.build_reduce(
                            market_id=int(position.market_id),
                            position_id=int(position.position_id),
                            reduce_size_units=size_try,
                            limit_price_units=price_units,
                            order_type=0,
                        )
                        signed = chain.sign(prepared)
                        tx_hash = chain.broadcast(signed)
                        chain.wait_for_receipt(
                            tx_hash, timeout_seconds=120, confirmations=1
                        )
                        txs += 1
                        last_exc = None
                        self.log.success(
                            f"reduce tx mined · {tx_hash[:14]}… · units={size_try} · "
                            f"waiting fill…",
                            scope=account.label,
                        )
                        break
                    except Exception as exc:
                        last_exc = exc
                        if lot > 0 and size_try > lot:
                            size_try = size_try - lot
                            self.log.warning(
                                f"reduce revert · shrink size → {size_try} · "
                                f"{type(exc).__name__}",
                                scope=account.label,
                            )
                            continue
                        # 1% haircut
                        size_try = max(lot or 1, (size_try * 99) // 100)
                        self.log.warning(
                            f"reduce revert · haircut size → {size_try} · "
                            f"{type(exc).__name__}",
                            scope=account.label,
                        )
                if last_exc is not None:
                    raise last_exc
                gone = self._wait_position_gone(
                    client=client,
                    account=account,
                    position_id=position.position_id,
                    timeout_seconds=40.0,
                )
                if gone:
                    self.log.success(
                        f"pos={position.position_id} FLAT after market close",
                        scope=account.label,
                    )
                else:
                    self.log.warning(
                        f"pos={position.position_id} still open after tx · "
                        "cancel residual & caller may retry",
                        scope=account.label,
                    )
                    txs += self._cancel_all_orders(
                        account=account, client=client, chain=chain
                    )
            except Exception as exc:
                self.log.error(
                    f"direct market reduce failed: {type(exc).__name__}: {exc}",
                    scope=account.label,
                )
        return txs

    def prepare_account_flat(self, account: FarmAccount) -> bool:
        """Testnet simple path: make account trade-ready.

        1) clear KILL locks
        2) market-close live POS/ORD
        3) free local stuck cycle if API flat
        Returns True if account has no open POS/ORD (ready for new rounds).
        """

        scope = account.label
        try:
            self.store.clear_kill(account.address)
        except Exception:
            pass
        try:
            self.store.force_release_account_lease(account.address)
        except Exception:
            pass

        client = self._client(account)
        remote = self._remote(client, account.address)
        active = self.store.get_active_cycle(account.address)
        if not remote.positions and not remote.active_orders:
            self._terminal_local_cycle_if_flat(
                account, client, reason="Normalize: already flat"
            )
            self.store.clear_kill(account.address)
            self.log.success(
                f"NORMALIZE OK · flat · cycle="
                f"{'cleared' if active else 'none'}",
                scope=scope,
            )
            return True

        self.log.action(
            f"NORMALIZE · close pos={len(remote.positions)} "
            f"ord={len(remote.active_orders)} before trade",
            scope=scope,
        )
        try:
            chain = self._write_chain(account, client)
            chain.verify_deployment()
            for tol in (200, 300, 500):
                self._direct_market_reduce_all(
                    account=account,
                    client=client,
                    chain=chain,
                    tolerance_bps=tol,
                )
                after = self._remote(client, account.address)
                if not after.positions and not after.active_orders:
                    break
        except Exception as exc:
            self.log.error(
                f"NORMALIZE close failed: {type(exc).__name__}: {exc}",
                scope=scope,
            )

        after = self._remote(client, account.address)
        if not after.positions and not after.active_orders:
            self._terminal_local_cycle_if_flat(
                account, client, reason="Normalize: flat after market close"
            )
            self.store.clear_kill(account.address)
            self.log.success("NORMALIZE OK · ready to trade", scope=scope)
            return True

        # Still dirty — free local cycle if we at least cancelled, log skip.
        self.log.error(
            f"NORMALIZE incomplete · pos={len(after.positions)} · "
            f"ord={len(after.active_orders)} · skip new entries this run",
            scope=scope,
        )
        return False

    def _terminal_local_cycle_if_flat(
        self,
        account: FarmAccount,
        client: FairgroundClient,
        *,
        reason: str,
    ) -> CycleRecord | None:
        """Operator force-close: if API is flat, free any stuck local cycle."""

        remote = self._remote(client, account.address)
        if remote.active_orders or remote.positions:
            return None
        active = self.store.get_active_cycle(account.address)
        if active is None:
            return None
        lease = self.store.acquire_lease(account.address)
        try:
            current = self.store.get_active_cycle(account.address)
            if current is None:
                return None
            if self._unresolved(current):
                for action in self.store.actions_for_cycle(current.cycle_id):
                    if action.status == "PREPARED":
                        self.store.mark_action(action.action_id, "NOT_BROADCAST")
            if current.state in _DIRECT_FLAT_RESOLUTION:
                return self.store.resolve_cycle_flat(current, reason=reason[:240])
            # Walk common post-entry states to COMPLETE when API is flat.
            try:
                if current.state is CycleState.HOLDING:
                    current = self.store.transition(
                        current,
                        CycleState.FLAT_CONFIRM,
                        error=reason[:240],
                    )
                if current.state is CycleState.EXIT_ARMED:
                    current = self.store.transition(
                        current,
                        CycleState.FLAT_CONFIRM,
                        error=reason[:240],
                    )
                if current.state is CycleState.FLAT_CONFIRM:
                    return self.store.transition(
                        current,
                        CycleState.COMPLETE,
                        error=reason[:240],
                    )
                if current.state is CycleState.RESIDUAL_RECONCILE:
                    current = self.store.transition(
                        current,
                        CycleState.FLAT_CONFIRM,
                        error=reason[:240],
                    )
                    return self.store.transition(
                        current,
                        CycleState.COMPLETE,
                        error=reason[:240],
                    )
            except Exception:
                pass
            return current
        finally:
            self.store.release_lease(lease)

    def _force_one(self, account: FarmAccount) -> FlattenResult:
        scope = account.label
        client = self._client(account)
        remote = self._remote(client, account.address)
        active = self.store.get_active_cycle(account.address)

        if not remote.active_orders and not remote.positions:
            if active is None:
                self.log.success("Уже FLAT · закрывать нечего · транзакций 0", scope=scope)
                self.store.clear_kill(account.address)
                return FlattenResult(
                    account.label,
                    account.address,
                    True,
                    "FLAT",
                    "Уже FLAT; транзакций не было",
                )
            resolved = self._terminal_local_cycle_if_flat(
                account, client, reason="Force-close: already flat"
            )
            self.store.clear_kill(account.address)
            self.log.success(
                f"FLAT · local cycle → {getattr(getattr(resolved, 'state', None), 'value', 'NONE')}",
                scope=scope,
            )
            return FlattenResult(
                account.label,
                account.address,
                True,
                "ABORTED_FLAT",
                "FLAT подтверждён; локальный цикл сброшен",
            )

        # Live exposure: ALWAYS use direct market reduce (ignore broken DUST/QUARANTINE).
        self.log.action(
            f"DIRECT MARKET CLOSE · pos={len(remote.positions)} · "
            f"ord={len(remote.active_orders)} · state="
            f"{active.state.value if active else 'NO_CYCLE'} · engine=v4",
            scope=scope,
        )
        chain = self._write_chain(account, client)
        chain.verify_deployment()
        txs = 0
        try:
            txs = self._direct_market_reduce_all(
                account=account,
                client=client,
                chain=chain,
                tolerance_bps=200,
            )
            # Second pass if first leave residual (partial fill / indexing lag).
            after = self._remote(client, account.address)
            if after.positions:
                self.log.warning(
                    f"residual after pass1 · pos={len(after.positions)} · retry 3%",
                    scope=scope,
                )
                txs += self._direct_market_reduce_all(
                    account=account,
                    client=client,
                    chain=chain,
                    tolerance_bps=300,
                )
        except Exception as exc:
            self.log.error(
                f"direct close crash: {type(exc).__name__}: {exc}",
                scope=scope,
            )

        after = self._remote(client, account.address)
        if not after.active_orders and not after.positions:
            self._terminal_local_cycle_if_flat(
                account,
                client,
                reason="Force-close: flat after direct market reduce",
            )
            self.store.clear_kill(account.address)
            self.log.success(
                f"FLAT после market close · tx={txs}",
                scope=scope,
            )
            return FlattenResult(
                account.label,
                account.address,
                True,
                "FLAT",
                f"Market-close OK · tx={txs}",
                txs,
            )

        # Still not flat — leave diagnostic, do not block other accounts.
        message = (
            f"Market-close partial · pos={len(after.positions)} · "
            f"ord={len(after.active_orders)} · tx={txs} · "
            "закрой остаток в UI Market+2% если нужно"
        )
        self.log.error(message, scope=scope)
        return FlattenResult(
            account.label,
            account.address,
            False,
            active.state.value if active else "EXPOSURE",
            message,
            txs,
        )

    def force_flatten_all(
        self,
        accounts: list[FarmAccount],
        *,
        acknowledge: str,
    ) -> list[FlattenResult]:
        if acknowledge != FORCE_FLATTEN_ACK:
            raise OperationsError("Принудительное закрытие не подтверждено")

        results: list[FlattenResult] = []
        eligible: list[FarmAccount] = []
        # Phase barrier: every eligible account is kill-latched before the
        # first signer can be loaded for any account.
        for account in accounts:
            control = self.store.get_kill(account.address)
            if control and control.get("kill_mode") == "FREEZE_SIGNER":
                message = "FREEZE_SIGNER активен; режим не понижен, подпись запрещена"
                self.log.warning(message, scope=account.label)
                results.append(
                    FlattenResult(
                        account.label,
                        account.address,
                        False,
                        "FROZEN_REQUIRES_EXPLICIT_OVERRIDE",
                        message,
                    )
                )
                continue
            self.store.set_kill(
                account.address,
                "KILL_FLATTEN",
                "Operator selected force-close from main.py",
            )
            eligible.append(account)

        self.log.warning(
            f"KILL_FLATTEN установлен: {len(eligible)}/{len(accounts)} аккаунтов · "
            "новые сделки запрещены"
        )
        for account in eligible:
            try:
                results.append(self._force_one(account))
            except Exception as exc:
                message = redact(f"{type(exc).__name__}: {exc}")
                self.log.error(message, scope=account.label)
                results.append(
                    FlattenResult(
                        account.label,
                        account.address,
                        False,
                        "ERROR",
                        message,
                    )
                )
        return results
