"""Fairground session inside an AdsPower Chrome profile.

The live frontend (PostHog, canvas, heatmaps) runs in Ads. Trades are signed
by the Hub private key, not by Rabby. The tab stays open so API fetch and
posthog.capture share the same cookies and TLS as the browser.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable
from urllib.parse import urlparse

from .timing import (
    SessionStyle,
    sleep_jitter,
)

SITE = "https://fairground.fi/421614"
CRYPTO_MARKETS = (
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "HYPE-USD",
    "XRP-USD",
    "ZEC-USD",
    "LIT-USD",
    "NEAR-USD",
)
KNOWN_RABBY_IDS = (
    "acmacodkjbdgmoleebolmdjonilkdbch",
    "khpkpbbcccdmmclmpgnpndfdjklcapol",
)
# AdsPower Assistant + other wallets in the same profile. Never open these as Rabby.
SKIP_EXTENSION_PREFIXES = ("gcaimg",)
OTHER_WALLET_IDS = (
    "bfnaelmomeimhpmdgjnjophhpkkoljpa",  # Phantom — seen stealing the unlock
    "nkbihfbeogaeaoehlefnkodbefgpgknn",  # MetaMask
    "fnjhmkhhmkbjkkabndcnnogagogbneec",  # Ronin
)
RABBY_PATHS = ("popup.html", "index.html", "notification.html")
NETWORK_BUTTONS = (
    'button:has-text("Add")',
    'button:has-text("Approve")',
)
UNLOCK_BUTTONS = (
    'button:has-text("Unlock")',
    'button:has-text("Confirm")',
    'button:has-text("Next")',
    'button:has-text("Continue")',
)
SIGN_BUTTONS = (
    'button:has-text("Sign")',
    'button:has-text("Confirm")',
    'button:has-text("Approve")',
    'button:has-text("Connect")',
    'button:has-text("Submit")',
)
CONNECT_BUTTONS = (
    'button:has-text("Connect Wallet")',
    'button:has-text("Connect wallet")',
)
# Recorded success (2026-09-05): first Privy row is "Rabby Wallet" + Last used.
# "Continue with a wallet" opens the 500+ list and is the fallback only.
LAST_USED_RABBY = (
    'button:has-text("Last used")',
    '[role="button"]:has-text("Last used")',
    'button:has-text("Rabby Wallet")',
    '[role="button"]:has-text("Rabby Wallet")',
    'div[role="button"]:has-text("Rabby Wallet")',
)
PRIVY_CONTINUE = (
    'button:has-text("Continue with a wallet")',
    '[role="button"]:has-text("Continue with a wallet")',
    'text=Continue with a wallet',
)
WALLET_CHOICE = (
    'button:has-text("Rabby Wallet")',
    'button:has-text("Rabby")',
    'div[role="button"]:has-text("Rabby")',
    '[role="button"]:has-text("Rabby")',
    'button:has-text("Injected")',
    'button:has-text("Browser")',
)
COOKIE_BUTTONS = (
    'button:has-text("Accept All")',
    'button:has-text("Accept all")',
)
PRIVY_IFRAME = 'iframe[src*="privy"]'
SKIP_CLICK_FRAME_MARKERS = (
    "telegram.org",
    "oauth.telegram",
    "chrome-extension://",
    "walletconnect.com",
    "explorer-api.walletconnect",
)
ALERT_BUTTONS = (
    'button:has-text("Ignore all")',
    'button:has-text("Ignore All")',
    '[role="button"]:has-text("Ignore all")',
    'button:has-text("Proceed anyway")',
    'button:has-text("Ignore")',
)
PERCENT_CHIPS = (10, 25, 50, 75)
LogFn = Callable[[str], None]
CancelCheck = Callable[[], None] | None


class BrowserTradeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def pick_percent(
    style: SessionStyle | None,
    lo: int = 10,
    hi: int = 75,
) -> int:
    rng = style.rng if style is not None else random
    family = style.margin_style if style is not None else "spread"
    if family == "conservative":
        pool = (10, 25, 25)
    elif family == "steps":
        pool = (25, 25, 50, 50, 75)
    else:
        pool = (10, 25, 50, 50, 75)
    clipped = [item for item in pool if lo <= item <= hi]
    if not clipped:
        clipped = [item for item in PERCENT_CHIPS if lo <= item <= hi] or [25]
    return int(rng.choice(clipped))


def pick_market(style: SessionStyle | None, blocked: set[str] | None = None) -> str:
    rng = style.rng if style is not None else random
    blocked = blocked or set()
    pool = [name for name in CRYPTO_MARKETS if name not in blocked]
    if not pool:
        pool = list(CRYPTO_MARKETS)
    return str(rng.choice(pool))


def is_extension_url(url: str) -> bool:
    return "chrome-extension://" in (url or "") or "moz-extension://" in (url or "")


def extension_id_from_url(url: str) -> str:
    if not is_extension_url(url or ""):
        return ""
    return (urlparse(url).hostname or "").lower()


def skip_extension_id(ext_id: str) -> bool:
    host = (ext_id or "").lower()
    if not host:
        return True
    if host in KNOWN_RABBY_IDS:
        return False
    if host in OTHER_WALLET_IDS:
        return True
    return any(host.startswith(prefix) for prefix in SKIP_EXTENSION_PREFIXES)


def click_frame_rank(url: str) -> int:
    """0 = Privy modal, 1 = Fairground page, 90+ = skip (Telegram/oauth/extensions)."""
    blob = (url or "").lower()
    if any(marker in blob for marker in SKIP_CLICK_FRAME_MARKERS):
        return 90
    if "privy.fairground.fi" in blob or "auth.privy.io" in blob or "privy.io" in blob:
        return 0
    if "privy" in blob:
        return 0
    if "fairground.fi" in blob:
        return 1
    return 90


BROWSER_FETCH_HEADERS = frozenset({"content-type", "connect-protocol-version", "accept"})


def browser_fetch_headers(headers: dict[str, str]) -> dict[str, str]:
    """Chrome forbids UA/Origin/Cookie on fetch — sending them breaks the Ads tab."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        if str(key).lower() in BROWSER_FETCH_HEADERS and value:
            out[str(key)] = str(value)
    return out


def rabby_alert_blocks_sign(text: str) -> bool:
    body = (text or "").lower()
    return (
        "process the alert" in body
        or "please process the alert before signing" in body
        or "ignore all" in body
    )


def header_shows_address(text: str, expected_address: str = "") -> bool:
    """Header chip is `0xAb12...Cd34` — both ends, not a 4-hex hit inside a price."""
    body = (text or "").lower()
    addr = (expected_address or "").lower()
    if not addr.startswith("0x") or len(addr) < 10:
        return False
    return addr[2:6] in body and addr[-4:] in body


def page_shows_wallet_connected(text: str, expected_address: str = "") -> bool:
    """True when the header looks logged in. Privy/SIWE screens are not connected."""
    body = (text or "").lower()
    if "continue with a wallet" in body:
        return False
    if "log in or sign up" in body:
        return False
    if "select your wallet" in body:
        return False
    if "sign to verify" in body:
        return False
    if "waiting for rabby" in body:
        return False
    if "last used" in body and "rabby" in body:
        return False
    if "connect your wallet to start trading" in body:
        return False
    if "connect wallet" in body:
        return False
    return header_shows_address(text, expected_address)


def site_ready_for_connect(text: str) -> bool:
    """False while the trade UI is still a skeleton — Connect click is a no-op then."""
    body = (text or "").lower()
    if "loading market data" in body:
        return False
    if "connect wallet" in body:
        return True
    return "place order" in body or "approve funds" in body or "enter amount" in body


def privy_modal_open(text: str) -> bool:
    body = (text or "").lower()
    return any(
        mark in body
        for mark in (
            "log in or sign up",
            "last used",
            "continue with a wallet",
            "sign to verify",
            "waiting for rabby",
            "successfully connected",
        )
    )


class BrowserTrader:
    def __init__(
        self,
        *,
        ws_url: str,
        expected_address: str,
        log: LogFn,
        password: str = "",
        cancel_check: CancelCheck = None,
        page_timeout_ms: int = 25000,
    ) -> None:
        self.ws_url = ws_url
        self.expected_address = (expected_address or "").lower()
        self.password = password
        self.log = log
        self.cancel_check = cancel_check
        self.page_timeout_ms = page_timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.connected = False
        self.opened = 0
        self.closed = 0

    def _cancel(self) -> None:
        if self.cancel_check:
            self.cancel_check()

    def _sleep(self, lo: float, hi: float) -> float:
        return sleep_jitter(lo, hi, cancel_check=self.cancel_check)

    def __enter__(self) -> "BrowserTrader":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserTradeError(
                "playwright_missing",
                "Playwright не установлен в runtime Hub",
            ) from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(
            self.ws_url, timeout=self.page_timeout_ms
        )
        if self._browser.contexts:
            self._context = self._browser.contexts[0]
        else:
            self._context = self._browser.new_context()
        self._page = self._pick_working_page()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._page = None
        self._context = None
        self._browser = None
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None

    def warm(self, style: SessionStyle | None = None) -> None:
        """Unlock Rabby, hard-reload Fairground, Connect if needed. Hub key signs trades."""
        self._cancel()
        page = self._pick_working_page()
        self._page = page
        try:
            self._context = page.context
        except Exception:
            pass
        self.unlock_rabby()
        self.open_site()
        self.dismiss_cookies()
        try:
            self.connect_wallet()
        except BrowserTradeError as exc:
            self.log(str(exc))
        self.identify_wallet()
        self.wander(style)

    def wander(self, style: SessionStyle | None = None, clicks: int = 3) -> None:
        blocked: set[str] = set()
        visits = max(1, clicks)
        for _ in range(visits):
            self._cancel()
            market = pick_market(style, blocked)
            try:
                if not self.select_market(market):
                    blocked.add(market)
                    continue
                page = self._need_page()
                side_sel = ('button:has-text("Short")',) if (
                    style is not None and style.rng.random() >= style.long_bias
                ) else ('button:has-text("Long")',)
                self._click_first(page, side_sel, timeout_ms=2500)
                self._sleep(0.25, 0.7)
                chip = f"{pick_percent(style)}%"
                self._click_first(page, (f'button:has-text("{chip}")',), timeout_ms=2500)
                self._sleep(0.4, 1.2)
            except Exception:
                blocked.add(market)
                continue

    def unlock_rabby(self) -> None:
        if not self.password:
            self.log("Пароль Rabby пустой — пробую как есть")
            return
        if self._wait_unlock(8):
            self.log("Rabby разблокирован")
            return
        opened = self._open_rabby_popup()
        try:
            if self._wait_unlock(12):
                self.log("Rabby разблокирован")
                return
        finally:
            if opened is not None:
                try:
                    if self._is_dead_extension_page(opened):
                        opened.close()
                except Exception:
                    pass
        self.log("Окно пароля Rabby пока нет — всплывёт на Connect")

    def identify_wallet(self) -> None:
        page = self._site_page()
        address = self.expected_address
        if not address:
            return
        try:
            page.wait_for_function(
                "() => window.posthog && typeof window.posthog.identify === 'function'",
                timeout=20000,
            )
        except Exception:
            pass
        status = "missing"
        for _ in range(10):
            self._cancel()
            try:
                status = page.evaluate(
                    """(address) => {
                        const ph = window.posthog;
                        if (!ph) return 'missing';
                        if (typeof ph.identify === 'function') ph.identify(address);
                        return 'ok';
                    }""",
                    address,
                )
            except Exception:
                status = "missing"
            if status == "ok":
                break
            self._sleep(0.8, 1.4)
        self.log(f"PostHog identify {status}")

    def open_site(self) -> None:
        page = self._pick_working_page()
        self._page = page
        try:
            self._context = page.context
        except Exception:
            pass
        page.bring_to_front()
        current = ""
        try:
            current = page.url or ""
        except Exception:
            current = ""
        if "fairground.fi" not in current:
            page.goto(SITE, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
        self._hard_reload(page)
        self._sleep(0.8, 1.4)
        if not self._wait_site_ready(page, 20):
            self.log("Маркет ещё грузится — Connect подождёт в цикле")

    def _hard_reload(self, page: Any) -> None:
        """AdsPower restores a frozen tab. Same-URL goto is a no-op; ⌘R boots the live app."""
        try:
            session = page.context.new_cdp_session(page)
            try:
                session.send("Network.setCacheDisabled", {"cacheDisabled": True})
            except Exception:
                pass
            session.send("Page.reload", {"ignoreCache": True})
            try:
                page.wait_for_load_state("load", timeout=self.page_timeout_ms)
            except Exception:
                page.wait_for_load_state("domcontentloaded", timeout=self.page_timeout_ms)
            self.log("Перезагрузил Fairground без кэша")
            return
        except Exception:
            pass
        try:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        page.goto(SITE, wait_until="load", timeout=self.page_timeout_ms)
        self.log("Перезагрузил Fairground")

    def dismiss_cookies(self) -> None:
        page = self._site_page()
        if self._click_first(page, COOKIE_BUTTONS, timeout_ms=2500):
            self.log("Cookie-баннер закрыт")
            self._sleep(0.2, 0.5)

    def connect_wallet(self) -> None:
        page = self._site_page()
        page.bring_to_front()
        if self._looks_connected():
            self.connected = True
            self.log("Кошелёк уже подключён")
            return
        self._wait_site_ready(page, 20)
        logged_open = False
        clicked_rabby = False
        logged_sign = False
        logged_lag = False
        last_connect_at = 0.0
        connect_clicks = 0
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self._cancel()
            page = self._site_page()
            try:
                page.bring_to_front()
            except Exception:
                pass
            if self._looks_connected():
                self._accept_terms(page)
                self.connected = True
                self.log("Подключил кошелёк")
                return
            if not self._privy_modal_open(page):
                now = time.monotonic()
                if now - last_connect_at >= 1.8:
                    use_js = connect_clicks >= 1
                    clicked = False
                    if use_js:
                        clicked = self._js_click_connect(page)
                        if clicked and not logged_lag:
                            self.log("Connect: повторный клик (кнопка лагает)")
                            logged_lag = True
                    if not clicked:
                        clicked = self._click_visible_connect(page)
                    if clicked:
                        connect_clicks += 1
                        last_connect_at = now
                        if not logged_open:
                            self.log("Открыл Connect Wallet")
                            logged_open = True
                self._sleep(0.45, 0.85)
                continue
            if not clicked_rabby:
                clicked_rabby = self._pick_privy_wallet(page)
            self._fill_unlock_anywhere()
            signed = self.confirm_rabby(timeout=1.8)
            if signed and not logged_sign:
                self.log("Rabby: Sign")
                logged_sign = True
            self._accept_terms(page)
            self._sleep(0.25, 0.5)
        self._log_connect_debug(page)
        raise BrowserTradeError("wallet_connect", "Не удалось подключить Rabby к Fairground")

    def _pick_privy_wallet(self, page: Any) -> bool:
        """Click Last used Rabby first (recorded path). Continue-with-a-wallet is fallback."""
        privy = self._privy_text(page).lower()
        last_used = "last used" in privy or "rabby wallet" in privy
        if last_used or not privy:
            if self._click_privy(page, LAST_USED_RABBY) or self._click_first(
                page, LAST_USED_RABBY, timeout_ms=400
            ):
                self.log("Privy: Rabby Wallet (Last used)")
                self._sleep(0.35, 0.7)
                return True
            if last_used:
                return False
        if self._click_privy(page, PRIVY_CONTINUE) or self._click_first(
            page, PRIVY_CONTINUE, timeout_ms=400
        ):
            self.log("Privy: Continue with a wallet")
            self._sleep(0.35, 0.7)
        if self._click_privy(page, WALLET_CHOICE) or self._click_first(
            page, WALLET_CHOICE, timeout_ms=400
        ):
            self.log("Выбрал Rabby в списке")
            self._sleep(0.35, 0.7)
            return True
        return False

    def select_market(self, market: str) -> bool:
        page = self._need_page()
        symbol = market.split("-", 1)[0]
        if self._click_first(
            page,
            (
                f'button:has-text("{market}")',
                f'button:has-text("{symbol}-USD")',
                f'button:has-text("{symbol}")',
            ),
            timeout_ms=2500,
        ):
            self._sleep(0.3, 0.8)
        search = None
        try:
            search = page.get_by_placeholder("Search")
            if search.count() == 0:
                search = None
        except Exception:
            search = None
        if search is None:
            # Dropdown already open from the pair button; try the row.
            pass
        else:
            try:
                search.fill(symbol, timeout=3000)
                self._sleep(0.25, 0.6)
            except Exception:
                pass
        row = page.locator(f'text=/^{symbol}-USD$/').first
        try:
            if row.count() == 0:
                row = page.get_by_text(f"{symbol}-USD", exact=True).first
            row.wait_for(state="visible", timeout=4000)
            parent = row.locator("xpath=ancestor::*[self::button or self::tr or self::div][1]")
            body = ""
            try:
                body = (parent.inner_text(timeout=1500) or "").lower()
            except Exception:
                body = (row.inner_text(timeout=1500) or "").lower()
            if "market closed" in body or "closed" in body and "usd" in body:
                page.keyboard.press("Escape")
                return False
            row.click(timeout=3000)
            self._sleep(0.6, 1.3)
            return True
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return symbol.lower() in (self._body().lower())

    def confirm_rabby(self, timeout: float = 8) -> int:
        clicks = 0
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            self._cancel()
            clicks += self._confirm_once()
            if clicks:
                self._sleep(0.35, 0.8)
            else:
                self._sleep(0.25, 0.5)
        return clicks

    def _confirm_once(self) -> int:
        hits = 0
        for popup in self._all_pages():
            try:
                url = popup.url or ""
            except Exception:
                continue
            if not is_extension_url(url) and "rabby" not in url.lower():
                continue
            if skip_extension_id(extension_id_from_url(url)):
                continue
            if self._is_other_wallet_page(popup):
                continue
            if self._unlock_page(popup):
                hits += 1
                continue
            body = ""
            try:
                body = (popup.inner_text("body", timeout=800) or "").lower()
            except Exception:
                body = ""
            if rabby_alert_blocks_sign(body) or self._alert_button_visible(popup):
                if self._click_first(popup, ALERT_BUTTONS, timeout_ms=1600):
                    self.log("Rabby: Ignore all")
                    hits += 1
                    self._sleep(0.25, 0.5)
            labels = SIGN_BUTTONS
            if "custom network" in body:
                labels = NETWORK_BUTTONS + SIGN_BUTTONS
            for selector in labels:
                try:
                    btn = popup.locator(selector).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=1600)
                        hits += 1
                        break
                except Exception:
                    continue
        return hits

    def _unlock_page(self, page: Any) -> bool:
        if not self.password:
            return False
        try:
            field = page.locator('input[type="password"]').first
            if field.count() == 0 or not field.is_visible():
                try:
                    field = page.get_by_placeholder("Enter the Password to Unlock")
                    if field.count() == 0:
                        return False
                except Exception:
                    return False
            field.fill(self.password, timeout=2500)
            for selector in UNLOCK_BUTTONS:
                try:
                    btn = page.locator(selector).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=2000)
                        return True
                except Exception:
                    continue
            try:
                field.press("Enter")
                return True
            except Exception:
                return True
        except Exception:
            return False

    def _fill_unlock_anywhere(self) -> bool:
        for page in self._all_pages():
            try:
                url = page.url or ""
            except Exception:
                url = ""
            if skip_extension_id(extension_id_from_url(url)):
                continue
            if self._is_other_wallet_page(page):
                continue
            if self._unlock_page(page):
                return True
        return False

    def _password_visible(self) -> bool:
        for page in self._all_pages():
            try:
                field = page.locator('input[type="password"]').first
                if field.count() > 0 and field.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _wait_unlock(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(1.0, seconds)
        filled = False
        while time.monotonic() < deadline:
            self._cancel()
            if self._fill_unlock_anywhere():
                filled = True
                self._sleep(0.4, 0.8)
                if not self._password_visible():
                    return True
            self._sleep(0.3, 0.6)
        return filled and not self._password_visible()

    def _open_rabby_popup(self) -> Any | None:
        context = self._context
        if context is None:
            return None
        for ext_id in KNOWN_RABBY_IDS:
            for path in RABBY_PATHS:
                opened = None
                try:
                    opened = context.new_page()
                    opened.goto(
                        f"chrome-extension://{ext_id}/{path}",
                        wait_until="domcontentloaded",
                        timeout=5000,
                    )
                except Exception:
                    if opened is not None:
                        try:
                            opened.close()
                        except Exception:
                            pass
                    continue
                if self._is_dead_extension_page(opened):
                    try:
                        opened.close()
                    except Exception:
                        pass
                    continue
                return opened
        return None

    def _is_dead_extension_page(self, page: Any) -> bool:
        try:
            body = (page.inner_text("body", timeout=1500) or "").lower()
        except Exception:
            body = ""
        return ("couldn" in body and "accessed" in body) or "err_file_not_found" in body

    def _looks_connected(self) -> bool:
        page = self._site_page()
        text = self._body()
        if "connect your wallet to start trading" in text.lower():
            return False
        if self._connect_button_visible(page):
            return False
        return header_shows_address(text, self.expected_address)

    def _is_other_wallet_page(self, page: Any) -> bool:
        try:
            host = extension_id_from_url(page.url or "")
        except Exception:
            host = ""
        if host in OTHER_WALLET_IDS:
            return True
        try:
            blob = f"{page.title() or ''} {page.inner_text('body', timeout=600) or ''}".lower()
        except Exception:
            blob = ""
        return "phantom" in blob or "metamask" in blob

    def _accept_terms(self, page: Any) -> bool:
        if self._click_privy(page, ('button:has-text("Accept")',)):
            self.log("Privy: Accept terms")
            self._sleep(0.3, 0.6)
            return True
        for scope in self._iter_click_scopes(page):
            try:
                btn = scope.get_by_role("button", name="Accept", exact=True)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=1200)
                    self.log("Privy: Accept terms")
                    self._sleep(0.3, 0.6)
                    return True
            except Exception:
                continue
        return False

    def _alert_button_visible(self, page: Any) -> bool:
        scopes: list[Any] = [page]
        try:
            scopes.extend(list(page.frames))
        except Exception:
            pass
        for scope in scopes:
            for selector in ALERT_BUTTONS[:3]:
                try:
                    loc = scope.locator(selector).first
                    if loc.count() > 0 and loc.is_visible():
                        return True
                except Exception:
                    continue
        return False

    def _connect_button_visible(self, page: Any) -> bool:
        for selector in CONNECT_BUTTONS:
            try:
                loc = page.locator(selector)
                count = loc.count()
            except Exception:
                continue
            for index in range(count):
                try:
                    if loc.nth(index).is_visible():
                        return True
                except Exception:
                    continue
        return False

    def _all_pages(self) -> list[Any]:
        pages: list[Any] = []
        browser = self._browser
        contexts = []
        if browser is not None:
            try:
                contexts = list(browser.contexts)
            except Exception:
                contexts = []
        if not contexts and self._context is not None:
            contexts = [self._context]
        for ctx in contexts:
            try:
                pages.extend(ctx.pages)
            except Exception:
                continue
        return pages

    def _pick_working_page(self) -> Any:
        pages = self._all_pages()
        for page in pages:
            try:
                url = page.url or ""
            except Exception:
                continue
            if "fairground.fi" in url:
                return page
        for page in pages:
            try:
                url = page.url or ""
            except Exception:
                continue
            if url.startswith("http") and not is_extension_url(url):
                return page
        if pages:
            first = pages[0]
            if not is_extension_url(getattr(first, "url", "") or ""):
                return first
        if self._context is None:
            raise BrowserTradeError("adspower_unavailable", "Нет вкладки AdsPower")
        return self._context.new_page()

    def _site_page(self) -> Any:
        for page in self._all_pages():
            try:
                url = page.url or ""
            except Exception:
                continue
            if "fairground.fi" in url:
                self._page = page
                try:
                    self._context = page.context
                except Exception:
                    pass
                return page
        return self._need_page()

    def _body(self) -> str:
        try:
            page = self._site_page()
        except BrowserTradeError:
            page = self._page
        if page is None:
            return ""
        try:
            return page.inner_text("body", timeout=2000) or ""
        except Exception:
            return ""

    def _privy_text(self, page: Any) -> str:
        try:
            iframe = page.locator(PRIVY_IFRAME).first
            if iframe.count() == 0:
                return ""
            return (
                page.frame_locator(PRIVY_IFRAME).locator("body").inner_text(timeout=700)
                or ""
            )
        except Exception:
            return ""

    def _privy_modal_open(self, page: Any) -> bool:
        return privy_modal_open(self._privy_text(page))

    def _wait_site_ready(self, page: Any, seconds: float) -> bool:
        deadline = time.monotonic() + max(1.0, seconds)
        while time.monotonic() < deadline:
            self._cancel()
            text = self._body()
            if site_ready_for_connect(text) or header_shows_address(text, self.expected_address):
                return True
            self._sleep(0.3, 0.55)
        return False

    def _click_visible_connect(self, page: Any) -> bool:
        """Click a real Connect control, not the tiny header chevron stub."""
        visible: list[Any] = []
        for selector in CONNECT_BUTTONS:
            try:
                loc = page.locator(selector)
                count = loc.count()
            except Exception:
                continue
            for index in range(count):
                try:
                    item = loc.nth(index)
                    if not item.is_visible():
                        continue
                    box = item.bounding_box()
                    if not box or box.get("width", 0) < 64 or box.get("height", 0) < 20:
                        continue
                    visible.append(item)
                except Exception:
                    continue
        for item in reversed(visible):
            try:
                item.click(timeout=1600)
                return True
            except Exception:
                continue
        return self._click_first(page, CONNECT_BUTTONS, timeout_ms=1500)

    def _js_click_connect(self, page: Any) -> bool:
        """React often ignores the first Playwright click while the trade UI hydrates."""
        try:
            return bool(
                page.evaluate(
                    """() => {
                        const buttons = Array.from(document.querySelectorAll('button'));
                        const hits = buttons.filter((btn) => {
                            const label = (btn.innerText || '').replace(/\\s+/g, ' ').trim();
                            if (!/connect wallet/i.test(label)) return false;
                            const rect = btn.getBoundingClientRect();
                            return rect.width >= 64 && rect.height >= 20;
                        });
                        if (!hits.length) return false;
                        hits[hits.length - 1].click();
                        return true;
                    }"""
                )
            )
        except Exception:
            return False

    def _wait_privy_modal(self, page: Any, seconds: float) -> bool:
        deadline = time.monotonic() + max(1.0, seconds)
        while time.monotonic() < deadline:
            self._cancel()
            if self._privy_modal_open(page):
                return True
            self._sleep(0.25, 0.4)
        return False

    def _click_privy(self, page: Any, selectors: tuple[str, ...], timeout_ms: int = 500) -> bool:
        try:
            iframe = page.locator(PRIVY_IFRAME).first
            if iframe.count() == 0:
                return False
            frame = page.frame_locator(PRIVY_IFRAME)
        except Exception:
            return False
        per = min(max(int(timeout_ms), 200), 800)
        for selector in selectors:
            self._cancel()
            try:
                loc = frame.locator(selector).first
                loc.click(timeout=per)
                return True
            except Exception:
                continue
        labels = []
        joined = " ".join(selectors).lower()
        if "last used" in joined:
            labels.append("Last used")
        if "rabby" in joined:
            labels.append("Rabby Wallet")
        for label in labels:
            self._cancel()
            try:
                loc = frame.get_by_text(label, exact=False).first
                loc.click(timeout=per)
                return True
            except Exception:
                continue
        return False

    def _iter_click_scopes(self, page: Any) -> list[Any]:
        ranked: list[tuple[int, Any]] = []
        try:
            frames = list(page.frames)
        except Exception:
            frames = [page]
        for frame in frames:
            try:
                url = frame.url or ""
            except Exception:
                url = ""
            rank = click_frame_rank(url)
            if rank >= 90:
                continue
            ranked.append((rank, frame))
        ranked.sort(key=lambda item: item[0])
        scopes = [item[1] for item in ranked]
        if not scopes:
            scopes = [page]
        return scopes

    def _log_connect_debug(self, page: Any) -> None:
        urls: list[str] = []
        try:
            for frame in page.frames:
                urls.append((frame.url or "")[:140])
        except Exception:
            pass
        preview = " | ".join(urls[:10]) or "нет"
        privy = self._privy_text(page).replace("\n", " ")[:180]
        self.log(f"Connect timeout · frames: {preview}")
        if privy:
            self.log(f"Connect timeout · privy: {privy}")

    def _click_first(self, page: Any, selectors: tuple[str, ...], timeout_ms: int) -> bool:
        if page is None:
            return False
        per = min(max(int(timeout_ms), 200), 900)
        for scope in self._iter_click_scopes(page):
            for selector in selectors:
                self._cancel()
                try:
                    loc = scope.locator(selector).first
                    if loc.count() == 0:
                        continue
                    if not loc.is_visible():
                        continue
                    loc.click(timeout=per)
                    return True
                except Exception:
                    continue
        return False

    def _need_page(self) -> Any:
        if self._page is None:
            raise BrowserTradeError("adspower_unavailable", "Нет вкладки AdsPower")
        return self._page

    def cookie_header(self) -> str:
        cookies: list[Any] = []
        seen: set[tuple[str, str]] = set()
        contexts = []
        if self._browser is not None:
            try:
                contexts = list(self._browser.contexts)
            except Exception:
                contexts = []
        if not contexts and self._context is not None:
            contexts = [self._context]
        for context in contexts:
            try:
                cookies.extend(context.cookies())
            except Exception:
                continue
        parts: list[str] = []
        for item in cookies:
            domain = str(item.get("domain") or "")
            if "fairground" not in domain and "privy" not in domain:
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            key = (name, value)
            if name and value and key not in seen:
                seen.add(key)
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    def live_identity_payload(self) -> dict[str, Any]:
        page = self._site_page()
        payload = page.evaluate(
            """() => ({
                ua: navigator.userAgent || '',
                language: Array.from(navigator.languages || []),
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                screen_resolution: (screen.width || 0) + '_' + (screen.height || 0),
                do_not_track: String(navigator.doNotTrack || ''),
            })"""
        )
        return payload if isinstance(payload, dict) else {}

    def fetch_from_page(
        self, url: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes]:
        page = self._site_page()
        safe = browser_fetch_headers(headers)
        result = page.evaluate(
            """async ({url, headers, body}) => {
                const res = await fetch(url, {
                    method: 'POST',
                    headers,
                    body,
                    credentials: 'include',
                    mode: 'cors',
                });
                return {status: res.status, text: await res.text()};
            }""",
            {"url": url, "headers": safe, "body": body.decode("utf-8")},
        )
        if not isinstance(result, dict):
            raise BrowserTradeError("api", "fetch из вкладки не вернул ответ")
        status = int(result.get("status") or 0)
        text = str(result.get("text") or "")
        return status, text.encode("utf-8")

    def emit_trade(self, payload: dict[str, Any]) -> str:
        page = self._site_page()
        market = str(payload.get("market") or "ETH-USD")
        symbol = market.split("-", 1)[0]
        side = str(payload.get("side") or "long").lower()

        def _num(value: Any) -> float:
            try:
                return float(str(value).strip() or "0")
            except Exception:
                return 0.0

        event = {
            "distinct_id": payload.get("address") or "",
            "market_id": market,
            "market_symbol": symbol,
            "direction": "up" if side == "long" else "down",
            "leverage": _num(payload.get("leverage")),
            "amount": _num(payload.get("margin")),
            "price": _num(payload.get("price")),
            "base_symbol": symbol,
            "quote_symbol": "USD",
            "transaction_hash": payload.get("tx") or "",
        }
        status = page.evaluate(
            """(event) => {
                const ph = window.posthog;
                if (!ph || typeof ph.capture !== 'function') return 'missing';
                ph.capture('order_configured', event);
                ph.capture('trade_submitted', event);
                return 'ok';
            }""",
            event,
        )
        try:
            self.select_market(market)
        except Exception:
            pass
        return str(status or "")
