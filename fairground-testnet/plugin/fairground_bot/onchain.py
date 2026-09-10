from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import re
import time
from typing import Any

from .client import FairgroundClient
from .deployment import (
    ARBITRUM_SEPOLIA,
    CANCEL_ORDER_SIGNATURE,
    OPEN_ORDER_SIGNATURE,
    REDUCE_ORDER_SIGNATURE,
    DeploymentManifest,
)
from .signer import LocalSigner


class OnchainError(RuntimeError):
    pass


class DeploymentMismatch(OnchainError):
    pass


class SimulationError(OnchainError):
    pass


class BroadcastUnknown(OnchainError):
    pass


class ReceiptReverted(OnchainError):
    pass


class OptionalDependencyMissing(OnchainError):
    pass


# Oracle-aware aggregate3 writes need more headroom than direct protocol calls.
# Both reduce and entry transactions have mined reverts near their padded RPC
# estimates; exact historical replays succeeded with a 2M limit.  Apply the
# floor to every oracle-backed protocol write so estimate variance cannot make
# otherwise-valid entry/close/cancel calldata fail on-chain.  Five million
# remains a strict reject ceiling, never a silent clamp, and workflow separately
# proves that the wallet can fund each bounded transaction.
HARD_MAX_GAS_LIMIT = 5_000_000
HARD_MAX_FEE_PER_GAS_WEI = 10_000_000_000  # 10 gwei on testnet
MAX_ORACLE_PAYLOAD_BYTES = 64 * 1024
ORACLE_WRITE_GAS_FLOOR = 2_000_000


def resolve_gas_limit(estimate: int, *, uses_oracle: bool) -> int:
    """Pad estimate, then clamp to the compiled ceiling if the pad is the overflow."""

    if estimate <= 0:
        raise SimulationError("Estimated gas must be greater than zero")
    gas_limit = int(estimate) + int(estimate) // 4
    if uses_oracle:
        gas_limit = max(gas_limit, ORACLE_WRITE_GAS_FLOOR)
    if gas_limit > HARD_MAX_GAS_LIMIT:
        return HARD_MAX_GAS_LIMIT
    return gas_limit


PERPS_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "openOrder",
        "inputs": [
            {
                "name": "input",
                "type": "tuple",
                "components": [
                    {"name": "marketId", "type": "uint64"},
                    {"name": "initialMargin", "type": "uint48"},
                    {"name": "initialNotional", "type": "uint48"},
                    {"name": "isLong", "type": "bool"},
                    {"name": "priceThreshold", "type": "uint32"},
                    {"name": "slPrice", "type": "uint32"},
                    {"name": "tpPrice", "type": "uint32"},
                ],
            }
        ],
        "outputs": [{"name": "", "type": "uint40"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "reduceOrder",
        "inputs": [
            {
                "name": "input",
                "type": "tuple",
                "components": [
                    {"name": "marketId", "type": "uint64"},
                    {"name": "openOrderId", "type": "uint40"},
                    {"name": "reduceSize", "type": "uint56"},
                    {"name": "priceThreshold", "type": "uint32"},
                    {"name": "orderType", "type": "uint8"},
                ],
            }
        ],
        "outputs": [{"name": "", "type": "uint40"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "cancelOrder",
        "inputs": [
            {"name": "marketId", "type": "uint64"},
            {"name": "openOrderId", "type": "uint40"},
            {"name": "reduceOnly", "type": "bool"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "getCollateralToken",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getCollateralTokenDecimals",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getPriceOracle",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getMarketConfig",
        "inputs": [{"name": "marketId", "type": "uint64"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "name", "type": "bytes32"},
                    {"name": "minLeverage", "type": "uint16"},
                    {"name": "maxLeverage", "type": "uint16"},
                    {"name": "minTradeSize", "type": "uint48"},
                    {"name": "maxTradeSize", "type": "uint48"},
                    {"name": "tickDecimals", "type": "uint8"},
                    {"name": "lotSize", "type": "uint24"},
                    {"name": "sizeDecimals", "type": "uint8"},
                ],
            }
        ],
        "stateMutability": "view",
    },
    {
        "type": "error",
        "name": "OracleDataRequired",
        "inputs": [
            {"name": "oracleContract", "type": "address"},
            {"name": "oracleQuery", "type": "bytes"},
            {"name": "feeRequired", "type": "uint256"},
        ],
    },
]


MULTICALL_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "aggregate3",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
            }
        ],
    }
]


ORACLE_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "fulfillOracleQuery",
        "stateMutability": "payable",
        "inputs": [{"name": "signedOffchainData", "type": "bytes"}],
        "outputs": [],
    }
]


ERC20_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "balanceOf",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "allowance",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "approve",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "decimals",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
    },
]


KNOWN_REVERTS = {
    "0x01735734": "Cannot open opposite side order while position exists",
    "0x25e2f413": "Order must be pending or partially filled to cancel",
    "0x7451ecdf": "Pending orders on opposite side",
    "0xad6857d1": "Manual reduce order already exists",
    "0x0a62b8f3": "No position found",
    "0x0428f2af": "Position must be filled to reduce",
    "0x93a4b908": "Reduce size exceeds open position size",
    "0xd36d8965": "Order not found",
    "0x59caa480": "Price threshold cannot be zero",
}


def to_base_units(value: Decimal, decimals: int, *, bits: int) -> int:
    if not value.is_finite() or value < 0:
        raise SimulationError("Amount must be a finite non-negative Decimal")
    scaled = (value * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN)
    integer = int(scaled)
    if integer >= 2**bits:
        raise SimulationError(f"Amount exceeds uint{bits}")
    return integer


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    chain_id: int
    block_number: int
    block_timestamp: int
    account: str
    eth_balance_wei: int
    collateral_balance_units: int
    allowance_units: int
    collateral_token: str
    price_oracle: str


@dataclass(frozen=True, slots=True)
class OnchainMarketConfig:
    name: str
    min_leverage_raw: int
    max_leverage_raw: int
    min_trade_size_units: int
    max_trade_size_units: int
    tick_decimals: int
    lot_size_raw: int
    size_decimals: int


@dataclass(frozen=True, slots=True)
class PreparedWrite:
    kind: str
    market_id: int | None
    transaction: dict[str, Any]
    calldata_hash: str
    uses_oracle: bool

    @property
    def nonce(self) -> int:
        return int(self.transaction["nonce"])

    @property
    def max_cost_wei(self) -> int:
        """Conservative EIP-1559 ceiling: padded gas limit × max fee."""

        return int(self.transaction["gas"]) * int(
            self.transaction["maxFeePerGas"]
        )


@dataclass(frozen=True, slots=True)
class SignedWrite:
    tx_hash: str
    raw_transaction: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ConfirmedReceipt:
    tx_hash: str
    block_number: int
    block_hash: str
    gas_used: int


class OnchainExecutor:
    """Single-account transaction builder matching the official web SDK flow."""

    def __init__(
        self,
        *,
        rpc_url: str,
        client: FairgroundClient,
        signer: LocalSigner,
        manifest: DeploymentManifest = ARBITRUM_SEPOLIA,
        timeout_seconds: int = 20,
        proxy_url: str | None = None,
    ) -> None:
        try:
            import requests
            from web3 import HTTPProvider, Web3
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise OptionalDependencyMissing(
                "Install requirements.txt before enabling writes"
            ) from exc
        session = requests.Session()
        # Disable ambient env proxies; bind only the explicit per-wallet proxy.
        session.trust_env = False
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}
        provider = HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": timeout_seconds},
            session=session,
            exception_retry_configuration=None,
        )
        self.Web3 = Web3
        self.w3 = Web3(provider)
        self.client = client
        self.signer = signer
        self.manifest = manifest
        self.account = Web3.to_checksum_address(signer.address)
        self.perps_address = Web3.to_checksum_address(manifest.perps_contract)
        self.multicall_address = Web3.to_checksum_address(manifest.multicall_forwarder)
        self.collateral_address = Web3.to_checksum_address(manifest.collateral_token)
        self.perps = self.w3.eth.contract(address=self.perps_address, abi=PERPS_ABI)
        self.multicall = self.w3.eth.contract(
            address=self.multicall_address, abi=MULTICALL_ABI
        )
        self.collateral = self.w3.eth.contract(
            address=self.collateral_address, abi=ERC20_ABI
        )
        self._oracle_selector = Web3.keccak(
            text="OracleDataRequired(address,bytes,uint256)"
        )[:4]

    def _code_hash(self, address: str) -> str:
        code = bytes(self.w3.eth.get_code(address))
        if not code:
            raise DeploymentMismatch("Expected contract bytecode is missing")
        return self.Web3.keccak(code).hex().removeprefix("0x")

    def _facet_address(self, signature: str) -> str:
        namespace = int(self.manifest.selector_storage_namespace, 16) - 1
        selector = self.Web3.keccak(text=signature)[:4]
        mapping_key = selector + (b"\x00" * 28)
        storage_slot = self.Web3.keccak(
            mapping_key + namespace.to_bytes(32, byteorder="big")
        )
        raw = bytes(self.w3.eth.get_storage_at(self.perps_address, storage_slot))
        address = self.Web3.to_checksum_address(raw[-20:].hex())
        if int(address, 16) == 0:
            raise DeploymentMismatch("A required Fairground function selector is unbound")
        return address

    def _eip1967_implementation(self, proxy: str) -> str:
        slot = (
            int.from_bytes(
                self.Web3.keccak(text="eip1967.proxy.implementation"), "big"
            )
            - 1
        ).to_bytes(32, "big")
        raw = bytes(self.w3.eth.get_storage_at(proxy, slot))
        implementation = self.Web3.to_checksum_address(raw[-20:].hex())
        if int(implementation, 16) == 0:
            raise DeploymentMismatch("Upgradeable contract has no EIP-1967 implementation")
        return implementation

    def verify_deployment(self) -> ChainSnapshot:
        if not self.w3.is_connected():
            raise DeploymentMismatch("Arbitrum Sepolia RPC is unavailable")
        chain_id = int(self.w3.eth.chain_id)
        if chain_id != self.manifest.chain_id:
            raise DeploymentMismatch(
                f"Wrong chain: expected {self.manifest.chain_id}, got {chain_id}"
            )
        proxy_hash = self._code_hash(self.perps_address)
        if proxy_hash != self.manifest.proxy_code_hash:
            raise DeploymentMismatch("Fairground proxy bytecode changed; manifest review required")
        multicall_hash = self._code_hash(self.multicall_address)
        if multicall_hash != self.manifest.multicall_code_hash:
            raise DeploymentMismatch("Fairground multicall bytecode changed; manifest review required")

        for signature in (OPEN_ORDER_SIGNATURE, REDUCE_ORDER_SIGNATURE):
            facet = self._facet_address(signature)
            if self._code_hash(facet) != self.manifest.open_reduce_facet_code_hash:
                raise DeploymentMismatch("Fairground open/reduce facet changed; manifest review required")
        cancel_facet = self._facet_address(CANCEL_ORDER_SIGNATURE)
        if self._code_hash(cancel_facet) != self.manifest.cancel_facet_code_hash:
            raise DeploymentMismatch("Fairground cancel facet changed; manifest review required")
        for signature in (
            "getCollateralToken()",
            "getCollateralTokenDecimals()",
            "getPriceOracle()",
        ):
            if self._code_hash(self._facet_address(signature)) != self.manifest.getter_facet_code_hash:
                raise DeploymentMismatch("Fairground getter facet changed; manifest review required")
        if self._code_hash(self._facet_address("getMarketConfig(uint64)")) != self.manifest.market_config_facet_code_hash:
            raise DeploymentMismatch("Fairground market-config facet changed; manifest review required")

        contract_collateral = self.Web3.to_checksum_address(
            self.perps.functions.getCollateralToken().call()
        )
        contract_decimals = int(
            self.perps.functions.getCollateralTokenDecimals().call()
        )
        token_decimals = int(self.collateral.functions.decimals().call())
        if contract_collateral != self.collateral_address:
            raise DeploymentMismatch("Fairground collateral address differs from the pinned manifest")
        if contract_decimals != self.manifest.collateral_decimals or token_decimals != contract_decimals:
            raise DeploymentMismatch("Fairground collateral decimals differ from the pinned manifest")
        if self._code_hash(self.collateral_address) != self.manifest.collateral_proxy_code_hash:
            raise DeploymentMismatch("Collateral proxy bytecode changed; manifest review required")
        collateral_impl = self._eip1967_implementation(self.collateral_address)
        if collateral_impl.lower() != self.manifest.collateral_implementation.lower():
            raise DeploymentMismatch("Collateral implementation changed; manifest review required")
        if self._code_hash(collateral_impl) != self.manifest.collateral_implementation_code_hash:
            raise DeploymentMismatch("Collateral implementation bytecode changed")

        account_code = bytes(self.w3.eth.get_code(self.account))
        if account_code:
            raise DeploymentMismatch("The local signer address is not an EOA")
        latest = self.w3.eth.get_block("latest")
        timestamp = int(latest["timestamp"])
        now = int(datetime.now(timezone.utc).timestamp())
        if abs(now - timestamp) > 600:
            raise DeploymentMismatch("RPC head timestamp is stale")
        oracle = self.Web3.to_checksum_address(
            self.perps.functions.getPriceOracle().call()
        )
        if oracle.lower() != self.manifest.price_oracle.lower():
            raise DeploymentMismatch("Fairground price oracle changed; manifest review required")
        if self._code_hash(oracle) != self.manifest.oracle_proxy_code_hash:
            raise DeploymentMismatch("Oracle proxy bytecode changed; manifest review required")
        oracle_impl = self._eip1967_implementation(oracle)
        if oracle_impl.lower() != self.manifest.oracle_implementation.lower():
            raise DeploymentMismatch("Oracle implementation changed; manifest review required")
        if self._code_hash(oracle_impl) != self.manifest.oracle_implementation_code_hash:
            raise DeploymentMismatch("Oracle implementation bytecode changed")
        return ChainSnapshot(
            chain_id=chain_id,
            block_number=int(latest["number"]),
            block_timestamp=timestamp,
            account=self.account.lower(),
            eth_balance_wei=int(self.w3.eth.get_balance(self.account)),
            collateral_balance_units=self.read_collateral_balance_units(),
            allowance_units=int(
                self.collateral.functions.allowance(
                    self.account, self.perps_address
                ).call()
            ),
            collateral_token=contract_collateral,
            price_oracle=oracle,
        )

    def read_collateral_balance_units(self) -> int:
        """Return the account's current collateral balance without signing.

        Percentage-based margin planning calls this immediately before every
        new round.  Deployment verification remains a separate fail-closed
        boundary; this lightweight read only refreshes the already-pinned
        collateral token balance between verified rounds.
        """

        try:
            balance = int(self.collateral.functions.balanceOf(self.account).call())
        except Exception as exc:
            raise OnchainError("Could not read the collateral balance") from exc
        if balance < 0:
            raise OnchainError("Collateral balance cannot be negative")
        return balance

    def get_market_config(self, market_id: int) -> OnchainMarketConfig:
        if market_id <= 0 or market_id >= 2**64:
            raise OnchainError("Market ID is outside uint64")
        try:
            raw = self.perps.functions.getMarketConfig(int(market_id)).call()
        except Exception as exc:
            raise OnchainError("Could not read on-chain market configuration") from exc
        if not isinstance(raw, (list, tuple)) or len(raw) != 8:
            raise OnchainError("On-chain market configuration has an unexpected shape")
        name_bytes = bytes(raw[0])
        try:
            name = name_bytes.rstrip(b"\x00").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OnchainError("On-chain market name is malformed") from exc
        return OnchainMarketConfig(
            name=name,
            min_leverage_raw=int(raw[1]),
            max_leverage_raw=int(raw[2]),
            min_trade_size_units=int(raw[3]),
            max_trade_size_units=int(raw[4]),
            tick_decimals=int(raw[5]),
            lot_size_raw=int(raw[6]),
            size_decimals=int(raw[7]),
        )

    def _fee_fields(self) -> dict[str, int]:
        latest = self.w3.eth.get_block("latest")
        base_fee = int(latest.get("baseFeePerGas", self.w3.eth.gas_price))
        try:
            priority = int(self.w3.eth.max_priority_fee)
        except Exception:
            priority = max(1, int(self.w3.eth.gas_price) - base_fee)
        priority = max(1, priority + priority // 4)
        max_fee = max(int(self.w3.eth.gas_price), (base_fee * 2) + priority)
        max_fee += max_fee // 4
        if max_fee > HARD_MAX_FEE_PER_GAS_WEI:
            raise SimulationError("Current max fee per gas exceeds the compiled testnet ceiling")
        return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}

    def _finalize_transaction(
        self,
        *,
        kind: str,
        market_id: int | None,
        target: str,
        data: str,
        gas_estimate: int,
        uses_oracle: bool,
    ) -> PreparedWrite:
        gas_limit = resolve_gas_limit(int(gas_estimate), uses_oracle=uses_oracle)
        transaction: dict[str, Any] = {
            "from": self.account,
            "to": self.Web3.to_checksum_address(target),
            "data": data,
            "value": 0,
            "nonce": int(self.w3.eth.get_transaction_count(self.account, "pending")),
            "chainId": self.manifest.chain_id,
            "gas": gas_limit,
            "type": 2,
            **self._fee_fields(),
        }
        calldata = bytes.fromhex(data.removeprefix("0x"))
        return PreparedWrite(
            kind=kind,
            market_id=market_id,
            transaction=transaction,
            calldata_hash="0x" + self.Web3.keccak(calldata).hex().removeprefix("0x"),
            uses_oracle=uses_oracle,
        )

    @staticmethod
    def _hex_candidates(value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, (bytes, bytearray)):
            found.append("0x" + bytes(value).hex())
        elif isinstance(value, str):
            found.extend(re.findall(r"0x[0-9a-fA-F]{8,}", value))
        elif isinstance(value, dict):
            for nested in value.values():
                found.extend(OnchainExecutor._hex_candidates(nested))
        elif isinstance(value, (list, tuple)):
            for nested in value:
                found.extend(OnchainExecutor._hex_candidates(nested))
        return found

    def _oracle_requirement(self, error: Exception) -> tuple[str, int] | None:
        try:
            from eth_abi import decode
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise OptionalDependencyMissing("eth-abi is required for oracle errors") from exc
        selector_hex = self._oracle_selector.hex().lower()
        blobs: list[Any] = [error.args, str(error)]
        for attr in ("data", "message"):
            if hasattr(error, attr):
                blobs.append(getattr(error, attr))
        for candidate in self._hex_candidates(blobs):
            raw_hex = candidate.removeprefix("0x")
            if not raw_hex.lower().startswith(selector_hex):
                continue
            try:
                raw = bytes.fromhex(raw_hex)
                oracle, _query, fee = decode(
                    ["address", "bytes", "uint256"], raw[4:]
                )
                checked_oracle = self.Web3.to_checksum_address(oracle)
                if checked_oracle.lower() != self.manifest.price_oracle.lower():
                    raise SimulationError("Contract requested an unpinned oracle")
                if int(fee) != 0:
                    raise SimulationError("Oracle requested a non-zero fee unsupported by aggregate3")
                return checked_oracle, int(fee)
            except (ValueError, TypeError):
                continue
        return None

    def _safe_simulation_error(self, error: Exception) -> SimulationError:
        for candidate in self._hex_candidates(error.args):
            selector = candidate[:10].lower()
            if selector in KNOWN_REVERTS:
                return SimulationError(KNOWN_REVERTS[selector])
        return SimulationError("Fairground transaction simulation reverted")

    def _signed_oracle_payload(self, market_id: int) -> bytes:
        response = self.client.get_price(str(market_id))
        payload = response.get("signedPricePayload")
        if not isinstance(payload, str) or not re.fullmatch(
            r"0x(?:[0-9a-fA-F]{2})+", payload
        ):
            raise SimulationError("Fairground returned a malformed signed oracle payload")
        raw = bytes.fromhex(payload[2:])
        if len(raw) > MAX_ORACLE_PAYLOAD_BYTES:
            raise SimulationError("Fairground oracle payload exceeds the safety limit")
        return raw

    def _build_protocol_write(
        self,
        *,
        kind: str,
        market_id: int,
        function: Any,
    ) -> PreparedWrite:
        oracle_payloads: dict[str, bytes] = {}
        protocol_data = function._encode_transaction_data()
        for _ in range(10):
            if oracle_payloads:
                calls: list[tuple[str, bool, bytes]] = []
                for oracle_address, payload in oracle_payloads.items():
                    oracle = self.w3.eth.contract(
                        address=oracle_address, abi=ORACLE_ABI
                    )
                    fulfill = bytes.fromhex(
                        oracle.functions.fulfillOracleQuery(payload)
                        ._encode_transaction_data()
                        .removeprefix("0x")
                    )
                    calls.append((oracle_address, False, fulfill))
                calls.append(
                    (
                        self.perps_address,
                        False,
                        bytes.fromhex(protocol_data.removeprefix("0x")),
                    )
                )
                executable = self.multicall.functions.aggregate3(calls)
                target = self.multicall_address
            else:
                executable = function
                target = self.perps_address
            try:
                gas = int(executable.estimate_gas({"from": self.account, "value": 0}))
            except Exception as exc:
                requirement = self._oracle_requirement(exc)
                if requirement is None:
                    raise self._safe_simulation_error(exc) from exc
                oracle_address, _fee_required = requirement
                oracle_payloads[oracle_address] = self._signed_oracle_payload(market_id)
                continue
            data = executable._encode_transaction_data()
            return self._finalize_transaction(
                kind=kind,
                market_id=market_id,
                target=target,
                data=data,
                gas_estimate=gas,
                uses_oracle=bool(oracle_payloads),
            )
        raise SimulationError("Oracle-aware simulation did not converge")

    def build_approval(self, amount_units: int) -> PreparedWrite:
        if amount_units <= 0:
            raise SimulationError("Approval amount must be greater than zero")
        function = self.collateral.functions.approve(self.perps_address, amount_units)
        try:
            gas = int(function.estimate_gas({"from": self.account}))
        except Exception as exc:
            raise self._safe_simulation_error(exc) from exc
        return self._finalize_transaction(
            kind="approval",
            market_id=None,
            target=self.collateral_address,
            data=function._encode_transaction_data(),
            gas_estimate=gas,
            uses_oracle=False,
        )

    def build_open(
        self,
        *,
        market_id: int,
        margin_units: int,
        notional_units: int,
        is_long: bool,
        limit_price_units: int,
        stop_loss_units: int = 0,
        take_profit_units: int = 0,
    ) -> PreparedWrite:
        values = (margin_units, notional_units, limit_price_units, stop_loss_units, take_profit_units)
        if (
            margin_units <= 0
            or notional_units <= 0
            or limit_price_units <= 0
            or any(value < 0 for value in values)
        ):
            raise SimulationError("Open-order units are invalid")
        if market_id <= 0 or market_id >= 2**64:
            raise SimulationError("Market ID is outside uint64")
        if not isinstance(is_long, bool):
            raise SimulationError("Open-order side must be a boolean")
        if margin_units >= 2**48 or notional_units >= 2**48:
            raise SimulationError("Open-order margin or notional exceeds uint48")
        if any(value >= 2**32 for value in values[2:]):
            raise SimulationError("Open-order price exceeds uint32")
        function = self.perps.functions.openOrder(
            (
                int(market_id),
                int(margin_units),
                int(notional_units),
                bool(is_long),
                int(limit_price_units),
                int(stop_loss_units),
                int(take_profit_units),
            )
        )
        return self._build_protocol_write(
            kind="entry", market_id=int(market_id), function=function
        )

    def build_reduce(
        self,
        *,
        market_id: int,
        position_id: int,
        reduce_size_units: int,
        limit_price_units: int,
        order_type: int = 1,
    ) -> PreparedWrite:
        if market_id <= 0 or market_id >= 2**64:
            raise SimulationError("Market ID is outside uint64")
        if reduce_size_units <= 0 or reduce_size_units >= 2**56:
            raise SimulationError("Reduce size is outside uint56")
        if position_id <= 0 or position_id >= 2**40:
            raise SimulationError("Position/order ID is outside uint40")
        if limit_price_units <= 0 or limit_price_units >= 2**32:
            raise SimulationError("Reduce price must be a non-zero uint32 threshold")
        # SDK enum: 0=MANUAL (resting limit), 1=MARKET (fill with threshold band).
        # Volume farm always closes market-style so residuals actually clear.
        order_type_i = int(order_type)
        if order_type_i not in {0, 1}:
            raise SimulationError("Reduce orderType must be 0 (MANUAL) or 1 (MARKET)")
        function = self.perps.functions.reduceOrder(
            (
                int(market_id),
                int(position_id),
                int(reduce_size_units),
                int(limit_price_units),
                order_type_i,
            )
        )
        return self._build_protocol_write(
            kind="exit", market_id=int(market_id), function=function
        )

    def build_cancel(
        self,
        *,
        market_id: int,
        order_id: int,
        reduce_only: bool,
    ) -> PreparedWrite:
        if market_id <= 0 or market_id >= 2**64:
            raise SimulationError("Market ID is outside uint64")
        if order_id <= 0 or order_id >= 2**40:
            raise SimulationError("Order ID is outside uint40")
        if not isinstance(reduce_only, bool):
            raise SimulationError("Cancel reduce-only flag must be a boolean")
        function = self.perps.functions.cancelOrder(
            int(market_id), int(order_id), bool(reduce_only)
        )
        return self._build_protocol_write(
            kind="cancel_exit" if reduce_only else "cancel_entry",
            market_id=int(market_id),
            function=function,
        )

    def sign(self, prepared: PreparedWrite) -> SignedWrite:
        # Re-verify upgradeable deployment immediately before every signature,
        # including a close after a long hold.
        self.verify_deployment()
        if prepared.transaction.get("from", "").lower() != self.account.lower():
            raise OnchainError("Prepared transaction account mismatch")
        if int(prepared.transaction.get("chainId", 0)) != self.manifest.chain_id:
            raise OnchainError("Prepared transaction chain mismatch")
        allowed_selectors = {
            "approval": self.Web3.keccak(text="approve(address,uint256)")[:4],
            "entry": self.Web3.keccak(text=OPEN_ORDER_SIGNATURE)[:4],
            "exit": self.Web3.keccak(text=REDUCE_ORDER_SIGNATURE)[:4],
            "cancel_entry": self.Web3.keccak(text=CANCEL_ORDER_SIGNATURE)[:4],
            "cancel_exit": self.Web3.keccak(text=CANCEL_ORDER_SIGNATURE)[:4],
        }
        if prepared.kind not in allowed_selectors:
            raise OnchainError("Prepared transaction kind is unsupported")
        expected_target = (
            self.collateral_address
            if prepared.kind == "approval"
            else self.multicall_address
            if prepared.uses_oracle
            else self.perps_address
        )
        try:
            actual_target = self.Web3.to_checksum_address(prepared.transaction["to"])
        except (KeyError, ValueError, TypeError) as exc:
            raise OnchainError("Prepared transaction target is malformed") from exc
        if actual_target != expected_target:
            raise OnchainError("Prepared transaction target does not match its simulated kind")
        if int(prepared.transaction.get("value", -1)) != 0:
            raise OnchainError("Prepared transaction unexpectedly transfers native value")
        if int(prepared.transaction.get("type", -1)) != 2:
            raise OnchainError("Prepared transaction must be EIP-1559 type 2")
        gas = int(prepared.transaction.get("gas", 0))
        max_fee = int(prepared.transaction.get("maxFeePerGas", 0))
        priority = int(prepared.transaction.get("maxPriorityFeePerGas", 0))
        if not 0 < gas <= HARD_MAX_GAS_LIMIT:
            raise OnchainError("Prepared transaction gas is outside the safety ceiling")
        if not 0 < priority <= max_fee <= HARD_MAX_FEE_PER_GAS_WEI:
            raise OnchainError("Prepared transaction fee fields are outside safety ceilings")
        data_value = prepared.transaction.get("data", "")
        if not isinstance(data_value, str) or not re.fullmatch(
            r"0x(?:[0-9a-fA-F]{2})+", data_value
        ):
            raise OnchainError("Prepared transaction calldata is malformed")
        data = data_value
        try:
            calldata = bytes.fromhex(data.removeprefix("0x"))
        except ValueError as exc:
            raise OnchainError("Prepared transaction calldata is malformed") from exc
        actual_calldata_hash = "0x" + self.Web3.keccak(calldata).hex().removeprefix("0x")
        if actual_calldata_hash.lower() != prepared.calldata_hash.lower():
            raise OnchainError("Prepared calldata changed after simulation")
        expected_selector = (
            self.Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]
            if prepared.uses_oracle
            else allowed_selectors[prepared.kind]
        )
        if calldata[:4] != expected_selector:
            raise OnchainError("Prepared calldata selector does not match its kind")
        pending_nonce = int(self.w3.eth.get_transaction_count(self.account, "pending"))
        if int(prepared.transaction.get("nonce", -1)) != pending_nonce:
            raise OnchainError("Account pending nonce changed after simulation")
        signed = self.signer.sign_transaction(dict(prepared.transaction))
        raw = bytes(
            getattr(signed, "raw_transaction", getattr(signed, "rawTransaction", b""))
        )
        if not raw:
            raise OnchainError("Signer returned no raw transaction")
        tx_hash = "0x" + bytes(signed.hash).hex()
        return SignedWrite(tx_hash=tx_hash.lower(), raw_transaction=raw)

    def broadcast(self, signed: SignedWrite) -> str:
        local_hash = "0x" + self.Web3.keccak(signed.raw_transaction).hex().removeprefix(
            "0x"
        )
        if local_hash.lower() != signed.tx_hash.lower():
            raise OnchainError(
                "Signed transaction bytes do not match the persisted transaction hash"
            )
        try:
            returned = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:
            # The deterministic hash is already persisted by the caller.  A
            # timeout may mean the node accepted the tx, so never resubmit here.
            raise BroadcastUnknown(
                "Broadcast result is unknown; reconcile the persisted transaction hash"
            ) from exc
        returned_hash = "0x" + bytes(returned).hex()
        if returned_hash.lower() != signed.tx_hash.lower():
            raise BroadcastUnknown("RPC returned a different transaction hash")
        return returned_hash.lower()

    def get_receipt(self, tx_hash: str) -> ConfirmedReceipt | None:
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except Exception as exc:
            if exc.__class__.__name__ == "TransactionNotFound":
                return None
            raise OnchainError("Could not read the transaction receipt") from exc
        if int(receipt["status"]) != 1:
            raise ReceiptReverted("Fairground transaction reverted on-chain")
        return ConfirmedReceipt(
            tx_hash=tx_hash.lower(),
            block_number=int(receipt["blockNumber"]),
            block_hash="0x" + bytes(receipt["blockHash"]).hex(),
            gas_used=int(receipt["gasUsed"]),
        )

    def current_block(self) -> int:
        return int(self.w3.eth.block_number)

    def wait_for_receipt(
        self,
        tx_hash: str,
        *,
        timeout_seconds: int = 120,
        confirmations: int = 2,
        on_poll: Any | None = None,
    ) -> ConfirmedReceipt:
        deadline = time.monotonic() + max(1, timeout_seconds)
        receipt: ConfirmedReceipt | None = None
        while time.monotonic() < deadline:
            if on_poll is not None:
                on_poll()
            receipt = self.get_receipt(tx_hash)
            if receipt is not None:
                target = receipt.block_number + max(0, confirmations - 1)
                if int(self.w3.eth.block_number) >= target:
                    # Re-read to detect a receipt disappearing during a reorg.
                    final = self.get_receipt(tx_hash)
                    if final is not None and final.block_hash == receipt.block_hash:
                        return final
            time.sleep(2)
        raise BroadcastUnknown(
            "Receipt deadline expired; reconcile the persisted transaction hash"
        )
