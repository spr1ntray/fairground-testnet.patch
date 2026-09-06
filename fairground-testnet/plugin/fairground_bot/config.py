from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from .validation import normalize_address


class ConfigError(ValueError):
    """Raised when runtime configuration is unsafe or malformed."""


# Testnet volume farm: high local ceilings. Live market min/max still apply.
HARD_MAX_MARGIN_USDC = Decimal("100000")
HARD_MAX_NOTIONAL_USDC = Decimal("2000000")
HARD_MAX_LEVERAGE = Decimal("50")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _validate_https_url(
    name: str,
    value: str,
    *,
    allow_http_local: bool = False,
    allow_path: bool = False,
) -> str:
    parsed = urlparse(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ConfigError(f"{name} has an invalid port") from exc
    local_http = allow_http_local and parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS
    if parsed.scheme != "https" and not local_http:
        raise ConfigError(f"{name} must use HTTPS")
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (not allow_path and parsed.path not in {"", "/"})
        or ".." in parsed.path
    ):
        raise ConfigError(f"{name} is not a safe base URL")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class Settings:
    api_url: str = "https://api.fairground.fi"
    rpc_url: str = "https://sepolia-rollup.arbitrum.io/rpc"
    chain_id: int = 421614
    account: str | None = None
    timeout_seconds: int = 15
    max_margin_usdc: Decimal = Decimal("1000")
    max_notional_usdc: Decimal = Decimal("10000")
    max_leverage: Decimal = Decimal("20")
    allow_custom_api: bool = False

    def __post_init__(self) -> None:
        api_url = _validate_https_url(
            "FAIRGROUND_API_URL", self.api_url, allow_http_local=self.allow_custom_api
        )
        rpc_url = _validate_https_url(
            "FAIRGROUND_RPC_URL",
            self.rpc_url,
            allow_http_local=self.allow_custom_api,
            allow_path=True,
        )
        object.__setattr__(self, "api_url", api_url)
        object.__setattr__(self, "rpc_url", rpc_url)

        parsed_api = urlparse(api_url)
        api_host = parsed_api.hostname
        if not self.allow_custom_api and api_host != "api.fairground.fi":
            raise ConfigError(
                "Custom API hosts are disabled in the production launcher"
            )
        if api_host != "api.fairground.fi" and api_host not in LOOPBACK_HOSTS:
            raise ConfigError("Custom API hosts are restricted to loopback in this build")
        if api_host == "api.fairground.fi" and parsed_api.port not in {None, 443}:
            raise ConfigError("The official Fairground API must use the standard HTTPS port")
        if self.chain_id != 421614:
            raise ConfigError("This build is pinned to Arbitrum Sepolia chain ID 421614")
        if self.account is not None:
            object.__setattr__(self, "account", normalize_address(self.account))
        if self.timeout_seconds < 1 or self.timeout_seconds > 60:
            raise ConfigError("Timeout must be between 1 and 60 seconds")
        limits = {
            "max_margin_usdc": (self.max_margin_usdc, HARD_MAX_MARGIN_USDC),
            "max_notional_usdc": (self.max_notional_usdc, HARD_MAX_NOTIONAL_USDC),
            "max_leverage": (self.max_leverage, HARD_MAX_LEVERAGE),
        }
        for name, (value, hard_ceiling) in limits.items():
            if not value.is_finite() or value <= 0:
                raise ConfigError(f"{name} must be finite and greater than zero")
            if value > hard_ceiling:
                raise ConfigError(f"{name} exceeds the compiled safety ceiling of {hard_ceiling}")
