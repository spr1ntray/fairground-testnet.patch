from __future__ import annotations

import re
from typing import Any

from .validation import ValidationError, normalize_address


class SignerError(RuntimeError):
    """Raised without ever including secret key material."""


class LocalSigner:
    """A narrowly scoped signer initialized from an unlocked vault secret."""

    __slots__ = ("_account", "address")

    def __init__(self, account: Any) -> None:
        self._account = account
        self.address = normalize_address(account.address)

    def __repr__(self) -> str:
        return f"LocalSigner(address={self.address!r})"

    def sign_transaction(self, transaction: dict[str, Any]) -> Any:
        return self._account.sign_transaction(transaction)


def load_private_key_hex(
    raw_key: str,
    *,
    expected_address: str | None = None,
) -> LocalSigner:
    """Load a signer from an in-memory hex key (vault unlock path).

    The caller must zeroize/discard the source secret when finished. This helper
    never writes the key to disk.
    """

    try:
        text = (raw_key or "").strip()
        raw = bytearray(text.encode("ascii"))
    except (AttributeError, UnicodeEncodeError) as exc:
        raise SignerError("In-memory private key must be 64 hex characters") from exc
    if text.startswith(("0x", "0X")):
        del raw[:2]
    try:
        if not re.fullmatch(rb"[0-9a-fA-F]{64}", raw):
            raise SignerError("In-memory private key must be 64 hex characters")
        try:
            from eth_account import Account
        except ImportError as exc:  # pragma: no cover
            raise SignerError(
                "Execution dependencies are missing; install requirements.txt"
            ) from exc
        account = Account.from_key(bytes.fromhex(raw.decode("ascii")))
    except ValueError as exc:
        raise SignerError("In-memory private key is not a valid EVM key") from exc
    finally:
        for index in range(len(raw)):
            raw[index] = 0

    signer = LocalSigner(account)
    if expected_address is not None:
        try:
            expected = normalize_address(expected_address)
        except (AttributeError, ValidationError) as exc:
            raise SignerError("Expected account is not a valid EVM address") from exc
        if signer.address != expected:
            raise SignerError("The in-memory key address does not match the expected account")
    return signer
