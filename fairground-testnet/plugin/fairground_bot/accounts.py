"""Vault-only execution accounts with mandatory 1:1 HTTP proxies."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from .proxy import ParsedProxy, ProxyError, parse_proxy
from .validation import ValidationError, normalize_address


class AccountError(RuntimeError):
    """Raised without embedding private-key material."""


PRIVATE_KEY_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")


@dataclass(slots=True)
class FarmAccount:
    """One execution account whose private key exists only in session memory."""

    index: int
    address: str
    proxy: ParsedProxy
    label: str
    # Vault-session secret: never expose it through the generated dataclass repr.
    private_key_hex: str | None = field(default=None, repr=False)
    adspower_profile_id: str = ""

    def __post_init__(self) -> None:
        if not self.private_key_hex:
            raise AccountError("FarmAccount requires an in-memory vault key")

    def redacted(self) -> dict[str, object]:
        return {
            "index": self.index,
            "address": self.address,
            "label": self.label,
            "keySource": "vault-memory",
            "proxy": self.proxy.redacted(),
            "adspowerProfile": self.adspower_profile_id or None,
        }

    def wipe_secret(self) -> None:
        """Best-effort clear of in-memory private key material."""

        secret = self.private_key_hex
        self.private_key_hex = None
        if secret is None:
            return
        # str is immutable; drop reference only. Callers should avoid copies.
        del secret


def _address_from_key(raw_key: str) -> str:
    if not PRIVATE_KEY_RE.fullmatch(raw_key.strip()):
        raise AccountError("Private key must be exactly 64 hex characters (optional 0x)")
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover - optional extra
        raise AccountError(
            "Execution dependencies are missing; install requirements.txt"
        ) from exc
    hex_key = raw_key.strip()
    if hex_key.startswith(("0x", "0X")):
        hex_key = hex_key[2:]
    try:
        account = Account.from_key(bytes.fromhex(hex_key))
    except ValueError as exc:
        raise AccountError("Private key is not a valid EVM secret") from exc
    return normalize_address(account.address)


def accounts_from_vault_records(records: list[dict[str, str]]) -> list[FarmAccount]:
    """Build farm accounts from decrypted vault records (keys stay in memory)."""

    if not records:
        raise AccountError("Vault is empty")
    seen_addresses: set[str] = set()
    accounts: list[FarmAccount] = []
    for index, item in enumerate(records, start=1):
        try:
            address = normalize_address(item["address"])
            private_key = item["private_key"].strip()
            derived_address = _address_from_key(private_key)
        except (
            KeyError,
            TypeError,
            AttributeError,
            AccountError,
            ValidationError,
        ) as exc:
            wipe_accounts(accounts)
            raise AccountError(f"Account #{index}: invalid vault record") from exc
        if derived_address != address:
            wipe_accounts(accounts)
            raise AccountError(
                f"Account #{index}: vault key does not match the stored address"
            )
        if address in seen_addresses:
            wipe_accounts(accounts)
            raise AccountError(f"Duplicate private key / address at account #{index}")
        seen_addresses.add(address)
        try:
            proxy = parse_proxy(item["proxy"])
        except (KeyError, TypeError, ProxyError) as exc:
            wipe_accounts(accounts)
            raise AccountError(f"Account #{index} proxy: {exc}") from exc
        profile_id = str(item.get("adspower_profile_id") or "").strip()
        accounts.append(
            FarmAccount(
                index=index,
                address=address,
                proxy=proxy,
                label=f"#{index} {address[:6]}…{address[-4:]}",
                private_key_hex=private_key,
                adspower_profile_id=profile_id,
            )
        )
    return accounts


def wipe_accounts(accounts: list[FarmAccount]) -> None:
    for account in accounts:
        account.wipe_secret()
    accounts.clear()
