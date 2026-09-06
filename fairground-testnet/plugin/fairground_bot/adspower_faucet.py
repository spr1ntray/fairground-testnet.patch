"""Arbitrum Sepolia ETH faucet via AdsPower browser profiles.

Ported from sekai_testnet_clean AdsPower assist:
  - Local AdsPower API start/stop
  - Playwright CDP attach
  - Fill wallet address + click claim on QuickNode (or similar) faucet
  - Poll native ETH balance on Arbitrum Sepolia RPC

One AdsPower profile per wallet (1:1). Profiles are rate-limited on start.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
import random
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .accounts import FarmAccount
from .human_log import redact


LogFn = Callable[[str], None]
_START_LOCK = threading.Lock()
_LAST_START_MONO = 0.0
_MIN_START_INTERVAL_SEC = 1.8
_START_MAX_RETRIES = 6
# AdsPower Local API aliases that resolve to loopback on the operator machine.
# `local.adspower.net` is the official AdsPower default (not a remote cloud host).
_LOCAL_HOSTS = frozenset(
    {
        "127.0.0.1",
        "localhost",
        "::1",
        "local.adspower.net",
    }
)


class FaucetError(RuntimeError):
    """Raised without embedding AdsPower API keys or private material."""


@dataclass(frozen=True, slots=True)
class AdsPowerFaucetConfig:
    api_base: str = "http://local.adspower.net:50325"
    faucet_url: str = "https://faucet.quicknode.com/arbitrum/sepolia"
    rpc_url: str = "https://sepolia-rollup.arbitrum.io/rpc"
    chain_id: int = 421614
    target_eth: str = "0.05"
    max_profiles_parallel: int = 3
    open_tabs: int = 0
    ip_tab: int = 0
    headless: int = 0
    close_profile_after: bool = True
    page_timeout_seconds: int = 20
    max_wait_seconds: int = 180
    poll_interval_seconds: float = 3.0
    post_fill_delay_seconds: float = 0.5
    human_captcha_solve_seconds: float = 8.0
    click_claim_button: bool = True
    claim_attempts: int = 5
    reload_between_claims: bool = True
    claim_retry_delay_seconds: float = 2.5
    request_timeout_seconds: int = 30
    address_selectors: tuple[str, ...] = (
        'input[name="address"]',
        'input[placeholder*="address" i]',
        'input[placeholder*="wallet" i]',
        'input[type="text"]',
        "textarea",
    )
    claim_button_selectors: tuple[str, ...] = (
        'button:has-text("Continue")',
        'button:has-text("Claim")',
        'button:has-text("Request")',
        'button:has-text("Send")',
        'button:has-text("Get")',
        'button[type="submit"]',
        '[role="button"]:has-text("Claim")',
        '[role="button"]:has-text("Continue")',
    )


@dataclass
class FaucetResult:
    address: str
    label: str
    success: bool
    start_eth: str
    final_eth: str
    error: str = ""


def _log_default(message: str) -> None:
    print(redact(message), flush=True)


def _safe_error(exc: BaseException, *secrets: str) -> str:
    """Build an exception summary that never echoes API keys / proxies."""

    text = redact(f"{type(exc).__name__}: {exc}")
    for secret in secrets:
        if secret and str(secret) in text:
            text = text.replace(str(secret), "<redacted>")
    return text


def _assert_local_http_base(url: str, *, label: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise FaucetError(f"{label} must be http(s)")
    host = (parsed.hostname or "").lower()
    allow_remote = os.getenv("FAIRGROUND_ALLOW_REMOTE_ADSPOWER") in {"1", "true", "yes"}
    if host not in _LOCAL_HOSTS and not allow_remote:
        raise FaucetError(
            f"{label} must point at local AdsPower "
            f"(127.0.0.1 / localhost / local.adspower.net). Got host={host!r}. "
            "Set FAIRGROUND_ALLOW_REMOTE_ADSPOWER=1 only if you accept the risk."
        )
    if parsed.username or parsed.password:
        raise FaucetError(f"{label} must not embed credentials in the URL")
    return url.rstrip("/")


def _assert_local_cdp_ws(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss", "http", "https"}:
        raise FaucetError("AdsPower returned an unsafe browser endpoint scheme")
    host = (parsed.hostname or "").lower()
    allow_remote = os.getenv("FAIRGROUND_ALLOW_REMOTE_ADSPOWER") in {"1", "true", "yes"}
    if host not in _LOCAL_HOSTS and not allow_remote:
        raise FaucetError(
            "Refusing non-local browser CDP endpoint from AdsPower "
            f"(host={host!r}). This blocks SSRF via a compromised Local API."
        )
    return ws_url


def _to_wei(value: str) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FaucetError(f"Invalid ETH amount: {value!r}") from exc
    if amount < 0:
        raise FaucetError("ETH amount must be non-negative")
    return int(amount * Decimal(10**18))


def _from_wei(value: int) -> str:
    amount = Decimal(max(0, int(value))) / Decimal(10**18)
    rendered = format(amount.quantize(Decimal("0.000000000001")), "f")
    return rendered.rstrip("0").rstrip(".") or "0"


def _native_balance_wei(rpc_url: str, address: str, *, timeout: int = 20) -> int:
    try:
        from web3 import Web3
    except ImportError as exc:  # pragma: no cover
        raise FaucetError("web3 is required for faucet balance checks") from exc
    session = requests.Session()
    session.trust_env = False
    from web3 import HTTPProvider

    w3 = Web3(
        HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": timeout},
            session=session,
        )
    )
    if not w3.is_connected():
        raise FaucetError("Arbitrum Sepolia RPC is unavailable")
    checksum = Web3.to_checksum_address(address)
    return int(w3.eth.get_balance(checksum))


def run_arbitrum_sepolia_faucet(
    *,
    accounts: list[FarmAccount],
    api_key: str,
    config: AdsPowerFaucetConfig,
    log: LogFn = _log_default,
) -> list[FaucetResult]:
    """Claim Arbitrum Sepolia ETH for many wallets via AdsPower profiles."""

    if not accounts:
        raise FaucetError("No accounts loaded")
    try:
        from .secrets import validate_adspower_api_key

        key = validate_adspower_api_key(str(api_key or ""), allow_empty=False)
    except ValueError as exc:
        raise FaucetError(str(exc)) from exc
    try:
        _assert_local_http_base(config.api_base, label="AdsPower API base")
    except FaucetError:
        key = ""
        raise
    missing = [a.label for a in accounts if not a.adspower_profile_id]
    if missing:
        key = ""
        raise FaucetError(
            "AdsPower profile_id missing for: "
            + ", ".join(missing[:8])
            + ("…" if len(missing) > 8 else "")
            + ". Put profiles 1:1 in input/adspower_profiles.txt and recreate DB"
        )

    workers = max(1, min(int(config.max_profiles_parallel), len(accounts)))
    log(
        f"[•] FAUCET · Arbitrum Sepolia · wallets={len(accounts)} · "
        f"parallel={workers} · target≥{config.target_eth} ETH"
    )
    results: list[FaucetResult] = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_one, account, key, config, log): account
                for account in accounts
            }
            for future in as_completed(futures):
                account = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    err = _safe_error(exc, key, account.private_key_hex or "")
                    results.append(
                        FaucetResult(
                            address=account.address,
                            label=account.label,
                            success=False,
                            start_eth="?",
                            final_eth="?",
                            error=err,
                        )
                    )
                    log(f"[-] {account.label} | faucet crash: {err}")
    finally:
        # Drop the only remaining cleartext handle as soon as workers finish.
        key = ""
    ok = sum(1 for item in results if item.success)
    marker = "✓" if ok == len(results) else ("-" if ok == 0 else "!")
    log(f"[{marker}] FAUCET FINISH · success={ok}/{len(results)}")
    return results


def _run_one(
    account: FarmAccount,
    api_key: str,
    config: AdsPowerFaucetConfig,
    log: LogFn,
) -> FaucetResult:
    profile_id = account.adspower_profile_id
    target = _to_wei(config.target_eth)
    start = _native_balance_wei(config.rpc_url, account.address)
    if target > 0 and start >= target:
        log(
            f"[✓] {account.label} | ETH already enough: {_from_wei(start)} "
            f"(target {config.target_eth})"
        )
        return FaucetResult(
            address=account.address,
            label=account.label,
            success=True,
            start_eth=_from_wei(start),
            final_eth=_from_wei(start),
        )

    session = requests.Session()
    session.trust_env = False
    # Header only — never put the API key in query strings or log bodies.
    session.headers.update({"Authorization": f"Bearer {api_key}"})
    profile: dict[str, str] | None = None
    try:
        profile = _start_profile(config, session, profile_id, log, account.label)
        mid = _open_and_claim(
            config,
            profile["ws_url"],
            account.address,
            log,
            account.label,
            client_rpc=config.rpc_url,
            target_wei=target,
            start_balance=start,
        )
        final = mid
        if target <= 0 or mid < target:
            final = _wait_for_balance(
                config, account.address, target, start, log, account.label
            )
        ok = target <= 0 or final >= target or final > start
        if ok and final >= target:
            log(f"[✓] {account.label} | faucet OK · {_from_wei(final)} ETH")
        elif ok:
            log(
                f"[!] {account.label} | faucet partial · {_from_wei(final)} ETH "
                f"(target {config.target_eth})"
            )
        else:
            log(
                f"[-] {account.label} | faucet timeout · {_from_wei(final)} / "
                f"{config.target_eth} ETH"
            )
        return FaucetResult(
            address=account.address,
            label=account.label,
            success=ok,
            start_eth=_from_wei(start),
            final_eth=_from_wei(final),
            error="" if ok else "target not reached",
        )
    except Exception as exc:
        raise FaucetError(_safe_error(exc, api_key)) from None
    finally:
        try:
            if config.close_profile_after and profile is not None:
                _stop_profile(config, session, profile_id)
        finally:
            session.headers.pop("Authorization", None)
            session.close()


def _start_profile(
    config: AdsPowerFaucetConfig,
    session: requests.Session,
    profile_id: str,
    log: LogFn,
    label: str,
) -> dict[str, str]:
    global _LAST_START_MONO
    url = f"{config.api_base.rstrip('/')}/api/v1/browser/start"
    params = {
        "user_id": profile_id,
        "open_tabs": int(config.open_tabs),
        "ip_tab": int(config.ip_tab),
        "headless": int(config.headless),
    }
    last_error = "unknown"
    for attempt in range(1, _START_MAX_RETRIES + 1):
        with _START_LOCK:
            now = time.monotonic()
            wait = _MIN_START_INTERVAL_SEC - (now - _LAST_START_MONO)
            if wait > 0:
                if wait >= 0.3:
                    log(f"[•] {label} | AdsPower queue {wait:.1f}s")
                time.sleep(wait)
            try:
                response = session.get(
                    url, params=params, timeout=config.request_timeout_seconds
                )
                payload = response.json() if response.content else {}
            except Exception as exc:
                _LAST_START_MONO = time.monotonic()
                # Never stash raw exception text — may echo Authorization headers.
                last_error = _safe_error(exc)
                payload = {"code": -1, "msg": last_error}
                response = None
            else:
                _LAST_START_MONO = time.monotonic()

        code = payload.get("code") if isinstance(payload, dict) else None
        msg = str((payload or {}).get("msg") or "")
        if response is not None and response.ok and code == 0:
            data = payload.get("data") or {}
            ws = data.get("ws") or {}
            ws_url = ws.get("puppeteer")
            if not ws_url:
                raise FaucetError("AdsPower start did not return puppeteer ws")
            safe_ws = _assert_local_cdp_ws(str(ws_url))
            # Log only a short profile tag — not a secret, but avoid full dumps.
            log(f"[✓] {label} | AdsPower profile started · id={profile_id[:6]}…")
            return {"ws_url": safe_ws}

        last_error = f"code={code} msg={msg}"
        msg_l = msg.lower()
        transient = (
            "too many" in msg_l
            or "rate" in msg_l
            or "timeout" in msg_l
            or "connection" in last_error.lower()
        )
        if transient and attempt < _START_MAX_RETRIES:
            backoff = min(8.0, 1.5 * attempt + random.uniform(0.4, 1.2))
            log(
                f"[!] {label} | AdsPower start retry {attempt}/"
                f"{_START_MAX_RETRIES} in {backoff:.1f}s"
            )
            time.sleep(backoff)
            continue
        raise FaucetError(f"AdsPower start failed: {last_error}")
    raise FaucetError(f"AdsPower start failed after retries: {last_error}")


def _stop_profile(
    config: AdsPowerFaucetConfig, session: requests.Session, profile_id: str
) -> None:
    url = f"{config.api_base.rstrip('/')}/api/v1/browser/stop"
    try:
        session.get(
            url,
            params={"user_id": profile_id},
            timeout=config.request_timeout_seconds,
        )
    except Exception:
        return


def _open_and_claim(
    config: AdsPowerFaucetConfig,
    ws_url: str,
    address: str,
    log: LogFn,
    label: str,
    *,
    client_rpc: str,
    target_wei: int,
    start_balance: int,
) -> int:
    last_balance = start_balance
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise FaucetError(
            "playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    timeout_ms = max(1, int(config.page_timeout_seconds)) * 1000
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ws_url, timeout=timeout_ms)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        if context.pages:
            page = context.pages[0]
            page.goto(
                config.faucet_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
        else:
            page = context.new_page()
            page.goto(
                config.faucet_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
        page.bring_to_front()

        filled = False
        last_error = ""
        for selector in config.address_selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=timeout_ms)
                locator.fill(address, timeout=timeout_ms)
                filled = True
                break
            except Exception as exc:
                last_error = str(exc)[:200]
        if not filled:
            raise FaucetError(f"Address field not found on faucet page: {last_error}")
        log(f"[✓] {label} | address pasted into faucet")
        time.sleep(max(0.0, float(config.post_fill_delay_seconds)))

        if config.click_claim_button:
            attempts = max(1, int(config.claim_attempts))
            for attempt in range(1, attempts + 1):
                log(f"[•] {label} | claim attempt {attempt}/{attempts}")
                try:
                    if page.url.rstrip("/") != config.faucet_url.rstrip("/"):
                        page.goto(
                            config.faucet_url,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                except Exception:
                    pass
                for _ in range(2):
                    try:
                        _click_claim(page, config.claim_button_selectors, timeout_ms)
                    except Exception:
                        pass
                    time.sleep(0.3)
                human = max(2.0, float(config.human_captcha_solve_seconds))
                log(f"[•] {label} | captcha window {human:.0f}s (solve in AdsPower)")
                time.sleep(human)
                for _ in range(2):
                    try:
                        _click_claim(page, config.claim_button_selectors, 6000)
                    except Exception:
                        pass
                    time.sleep(0.3)
                try:
                    bal = _native_balance_wei(client_rpc, address)
                    last_balance = bal
                    log(f"[•] {label} | balance after claim {attempt}: {_from_wei(bal)} ETH")
                    if target_wei > 0 and bal >= target_wei:
                        log(f"[✓] {label} | target reached")
                        break
                    if bal > start_balance:
                        log(f"[✓] {label} | ETH credited — stop claim loop")
                        break
                except Exception:
                    pass
                if attempt < attempts and config.reload_between_claims:
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                        time.sleep(0.5)
                        for selector in config.address_selectors:
                            try:
                                locator = page.locator(selector).first
                                locator.wait_for(state="visible", timeout=3000)
                                locator.fill(address, timeout=3000)
                                break
                            except Exception:
                                pass
                        time.sleep(max(1.0, float(config.claim_retry_delay_seconds)))
                    except Exception:
                        time.sleep(max(1.0, float(config.claim_retry_delay_seconds)))
        try:
            page.close()
        except Exception:
            pass
    try:
        last_balance = _native_balance_wei(client_rpc, address)
    except Exception:
        pass
    return last_balance


def _click_claim(page: Any, selectors: tuple[str, ...], timeout_ms: int) -> None:
    last_error = ""
    for selector in selectors:
        for _ in range(2):
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=timeout_ms)
                locator.scroll_into_view_if_needed()
                page.wait_for_timeout(100)
                locator.click(timeout=timeout_ms, force=True)
                return
            except Exception as exc:
                last_error = str(exc)[:200]
                page.wait_for_timeout(200)
    raise FaucetError(f"Claim button not clickable: {last_error}")


def _wait_for_balance(
    config: AdsPowerFaucetConfig,
    address: str,
    target: int,
    start: int,
    log: LogFn,
    label: str,
) -> int:
    deadline = time.monotonic() + max(0, int(config.max_wait_seconds))
    interval = max(1.0, float(config.poll_interval_seconds))
    balance = start
    while time.monotonic() < deadline:
        try:
            balance = _native_balance_wei(config.rpc_url, address)
        except Exception as exc:
            log(f"[!] {label} | balance poll failed: {exc}")
            time.sleep(interval)
            continue
        if target <= 0 or balance >= target or balance > start:
            return balance
        time.sleep(interval)
    try:
        return _native_balance_wei(config.rpc_url, address)
    except Exception:
        return balance
