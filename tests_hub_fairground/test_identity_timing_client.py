from __future__ import annotations

import gzip
import json
import unittest
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugin.fairground_bot.client import FairgroundClient, decode_http_body
from plugin.fairground_bot.browser_trade import (
    browser_fetch_headers,
    click_frame_rank,
    header_shows_address,
    is_extension_url,
    page_shows_wallet_connected,
    pick_percent,
    privy_modal_open,
    rabby_alert_blocks_sign,
    site_ready_for_connect,
    skip_extension_id,
)
from plugin.fairground_bot.farm import (
    FarmRuntimePlan,
    VolumeFarm,
    _is_recoverable_round_error,
)
from plugin.fairground_bot.onchain import (
    HARD_MAX_GAS_LIMIT,
    SimulationError,
    resolve_gas_limit,
)
from plugin.fairground_bot.workflow import api_lot_agrees_with_chain
from plugin.fairground_bot.waitlist import admission_from_balances
from plugin.fairground_bot.farm import AccountFarmResult, _looks_like_not_waitlisted, _tally_live_finish
from plugin.fairground_bot.identity import identity_from_profile, resolve_identity
from plugin.fairground_bot.timing import hold_seconds, roll_session


class IdentityTests(unittest.TestCase):
    def test_ads_profile_becomes_browser_headers(self) -> None:
        ident = identity_from_profile(
            {
                "fingerprint_config": {
                    "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
                    "language": ["de-DE", "de", "en"],
                    "timezone": "Europe/Berlin",
                    "screen_resolution": "1920_1080",
                    "do_not_track": "true",
                }
            }
        )
        headers = ident.headers()
        self.assertEqual(headers["Origin"], "https://fairground.fi")
        self.assertIn("Chrome/141", headers["User-Agent"])
        self.assertTrue(headers["Accept-Language"].startswith("de-DE"))
        self.assertIn("Google Chrome", headers["sec-ch-ua"])
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
        self.assertNotIn("DNT", headers)
        self.assertNotIn("Device-Memory", headers)
        self.assertNotIn("fairground-guard", headers["User-Agent"])
        self.assertNotIn("sec-ch-ua-full-version-list", headers)
        self.assertTrue(headers["Referer"].endswith("/421614"))
        self.assertEqual(headers["Accept-Encoding"], "gzip, deflate")
        self.assertEqual(
            headers["sec-ch-ua"],
            '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
        )
        keys = list(headers)
        self.assertLess(keys.index("sec-ch-ua"), keys.index("Accept"))
        self.assertEqual(headers["Sec-Fetch-Site"], "same-site")

    def test_missing_ua_falls_back_to_profile_bank(self) -> None:
        first = resolve_identity("0xabc", {"fingerprint_config": {}})
        second = resolve_identity("0xabc", None)
        self.assertEqual(first.user_agent, second.user_agent)
        self.assertEqual(first.source, "bank")
        self.assertIn("Chrome/", first.user_agent)
        third = resolve_identity("0xdef", None)
        self.assertNotEqual(first.user_agent + first.timezone, third.user_agent + third.timezone)

    def test_chrome_143_grease_matches_chromium(self) -> None:
        ident = identity_from_profile(
            {
                "fingerprint_config": {
                    "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
                    "language": ["en-US", "en"],
                }
            }
        )
        self.assertEqual(
            ident.headers()["sec-ch-ua"],
            '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        )
        self.assertEqual(ident.headers()["sec-ch-ua-platform"], '"macOS"')

    def test_bank_identities_are_unique(self) -> None:
        from plugin.fairground_bot.identity import bank_identities

        seeds = [f"0x{index:040x}" for index in range(19)]
        rows = bank_identities(seeds)
        fingerprints = {
            (item.user_agent, item.accept_language, item.timezone, item.screen)
            for item in rows.values()
        }
        self.assertEqual(len(fingerprints), 19)
        mac_ua = [item.user_agent for item in rows.values() if item.platform == "macOS"]
        self.assertTrue(all("Mac OS X 10_15_7" in ua for ua in mac_ua))


class ClientTests(unittest.TestCase):
    def test_requests_carry_chain_id_and_identity(self) -> None:
        captured: dict = {}
        ident = identity_from_profile(
            {
                "fingerprint_config": {
                    "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
                    "language": ["en-US", "en"],
                }
            }
        )

        def transport(request, timeout):
            captured["url"] = request.full_url
            captured["raw"] = list(request.header_items())
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["body"] = json.loads(request.data)
            return 200, b'{"portfolio":{"owner":"0x0000000000000000000000000000000000000001","totalVolume":1}}'

        client = FairgroundClient(
            "https://api.fairground.fi",
            transport=transport,
            identity=ident,
        )
        client.get_portfolio("0x0000000000000000000000000000000000000001")
        self.assertEqual(captured["body"]["chainId"], "421614")
        self.assertEqual(captured["body"]["owner"], "0x0000000000000000000000000000000000000001")
        self.assertIn("chrome/151", captured["headers"]["user-agent"].lower())
        self.assertEqual(captured["headers"]["origin"], "https://fairground.fi")
        self.assertEqual(captured["headers"]["connect-protocol-version"], "1")
        self.assertNotIn("fairground-guard", captured["headers"]["user-agent"])
        self.assertEqual(captured["headers"]["accept-encoding"], "gzip, deflate")
        self.assertNotIn("sec-ch-ua-full-version-list", captured["headers"])
        wire = dict(captured["raw"])
        self.assertIn("User-Agent", wire)
        self.assertIn("sec-ch-ua", wire)
        self.assertIn("Content-Type", wire)
        self.assertIn("Connect-Protocol-Version", wire)
        self.assertNotIn("User-agent", wire)
        self.assertNotIn("Sec-ch-ua", wire)
        self.assertNotIn("Connect-protocol-version", wire)

    def test_wire_keeps_chrome_header_names(self) -> None:
        import socket
        import threading

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        captured: dict[str, bytes] = {}

        def serve() -> None:
            conn, _ = listener.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            captured["head"] = data.split(b"\r\n\r\n", 1)[0]
            body = b'{"portfolio":{"owner":"0x0000000000000000000000000000000000000001"}}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ident = identity_from_profile(
            {
                "fingerprint_config": {
                    "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
                    "language": ["en-US", "en"],
                }
            }
        )
        FairgroundClient(f"http://127.0.0.1:{port}", identity=ident).get_portfolio(
            "0x0000000000000000000000000000000000000001"
        )
        thread.join(2)
        listener.close()
        names = [
            line.split(":", 1)[0]
            for line in captured["head"].decode("latin1").split("\r\n")[1:]
            if ":" in line
        ]
        self.assertIn("User-Agent", names)
        self.assertIn("sec-ch-ua", names)
        self.assertIn("sec-ch-ua-platform", names)
        self.assertIn("Sec-Fetch-Site", names)
        self.assertIn("Connect-Protocol-Version", names)
        self.assertNotIn("Sec-Ch-Ua", names)
        self.assertNotIn("User-agent", names)

    def test_gzip_body_is_decoded(self) -> None:
        payload = gzip.compress(b'{"ok":true}')
        self.assertEqual(decode_http_body(payload, "gzip"), b'{"ok":true}')
        self.assertEqual(decode_http_body(b'{"ok":true}', ""), b'{"ok":true}')
        self.assertEqual(decode_http_body(b'{"ok":true}', "gzip"), b'{"ok":true}')

    def test_browser_fetch_transport(self) -> None:
        def browser_fetch(url, headers, body):
            self.assertIn("/GetPortfolio", url)
            self.assertEqual(headers["Origin"], "https://fairground.fi")
            payload = json.loads(body.decode())
            self.assertEqual(payload["chainId"], "421614")
            return 200, b'{"portfolio":{"owner":"0x0000000000000000000000000000000000000001"}}'

        ident = identity_from_profile(
            {
                "fingerprint_config": {
                    "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
                    "language": ["en-US", "en"],
                }
            }
        )
        client = FairgroundClient(
            "https://api.fairground.fi",
            identity=ident,
            cookie_header="ph_phc_test=abc",
            browser_fetch=browser_fetch,
        )
        result = client.get_portfolio("0x0000000000000000000000000000000000000001")
        self.assertEqual(result["portfolio"]["owner"], "0x0000000000000000000000000000000000000001")

    def test_browser_fetch_strips_forbidden_headers(self) -> None:
        stripped = browser_fetch_headers(
            {
                "User-Agent": "Mozilla/5.0",
                "Origin": "https://fairground.fi",
                "Cookie": "secret=1",
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            }
        )
        self.assertEqual(stripped["Content-Type"], "application/json")
        self.assertEqual(stripped["Connect-Protocol-Version"], "1")
        self.assertNotIn("User-Agent", stripped)
        self.assertNotIn("Origin", stripped)
        self.assertNotIn("Cookie", stripped)

    def test_browser_fetch_error_falls_back_to_urllib(self) -> None:
        captured: dict = {}

        def boom(url, headers, body):
            raise RuntimeError("page gone")

        def transport(request, timeout):
            captured["ok"] = True
            return 200, b'{"portfolio":{"owner":"0x0000000000000000000000000000000000000001"}}'

        client = FairgroundClient(
            "https://api.fairground.fi",
            transport=transport,
            browser_fetch=boom,
        )
        result = client.get_portfolio("0x0000000000000000000000000000000000000001")
        self.assertTrue(captured.get("ok"))
        self.assertEqual(result["portfolio"]["owner"], "0x0000000000000000000000000000000000000001")


class GasLimitTests(unittest.TestCase):
    def test_oracle_pad_clamps_to_ceiling(self) -> None:
        self.assertEqual(resolve_gas_limit(4_200_000, uses_oracle=True), HARD_MAX_GAS_LIMIT)
        self.assertEqual(resolve_gas_limit(800_000, uses_oracle=True), 2_000_000)
        self.assertEqual(resolve_gas_limit(800_000, uses_oracle=False), 1_000_000)

    def test_estimate_over_ceiling_clamps(self) -> None:
        self.assertEqual(
            resolve_gas_limit(HARD_MAX_GAS_LIMIT + 1, uses_oracle=True),
            HARD_MAX_GAS_LIMIT,
        )

    def test_gas_ceiling_is_recoverable(self) -> None:
        self.assertTrue(
            _is_recoverable_round_error(
                SimulationError("Estimated gas exceeds the compiled transaction ceiling")
            )
        )
        self.assertTrue(
            _is_recoverable_round_error("Fairground transaction simulation reverted")
        )


class WaitlistTests(unittest.TestCase):
    def test_zero_usdc_without_activity_is_not_admitted(self) -> None:
        closed = admission_from_balances(usdc_units=0, positions=0, orders=0, fills=0)
        self.assertFalse(closed.admitted)
        self.assertEqual(closed.reason, "не в waitlist")

    def test_usdc_or_volume_means_waitlisted(self) -> None:
        self.assertTrue(admission_from_balances(usdc_units=1_000_000).admitted)
        self.assertTrue(admission_from_balances(usdc_units=0, volume_usdc="12.5").admitted)
        self.assertTrue(admission_from_balances(usdc_units=0, positions=1).admitted)
        self.assertTrue(admission_from_balances(usdc_units=0, fills=3).admitted)

    def test_zero_usdc_farm_error_is_waitlist(self) -> None:
        self.assertTrue(_looks_like_not_waitlisted(RuntimeError("PERCENT margin: USDC balance is zero")))
        self.assertFalse(_looks_like_not_waitlisted(RuntimeError("Fairground API is unavailable")))

    def test_live_finish_separates_waitlist_skips(self) -> None:
        rows = [
            AccountFarmResult("0x1", "a", 8, 2, True, waitlisted=True),
            AccountFarmResult("0x2", "b", 8, 0, True, waitlisted=False),
            AccountFarmResult("0x3", "c", 8, 0, False, waitlisted=None),
        ]
        ok, skipped, failed = _tally_live_finish(rows)
        self.assertEqual((ok, skipped, failed), (1, 1, 1))


class LotSizeTests(unittest.TestCase):
    def test_api_zero_lot_matches_chain_unit_lot(self) -> None:
        self.assertTrue(api_lot_agrees_with_chain(0, 1))
        self.assertTrue(api_lot_agrees_with_chain("0", 1))
        self.assertTrue(api_lot_agrees_with_chain(1, 1))
        self.assertFalse(api_lot_agrees_with_chain(2, 1))
        self.assertFalse(api_lot_agrees_with_chain("0.001", 1))


class ManifestTests(unittest.TestCase):
    def test_farm_is_http_only(self) -> None:
        manifest = json.loads(
            (ROOT / "fairground-testnet" / "hub.plugin.json").read_text(encoding="utf-8")
        )
        farm = next(action for action in manifest["actions"] if action["id"] == "farm")
        self.assertEqual(farm["resources"]["account"], ["private_key", "proxy"])
        self.assertEqual(farm["resources"]["settings"], [])
        self.assertNotIn("adspower_profile", farm["resources"]["account"])
        self.assertNotIn("email_password", farm["permissions"]["secrets"])
        self.assertLessEqual(len(manifest["presentation"]["description"]), 80)
        faucet = next(action for action in manifest["actions"] if action["id"] == "faucet")
        self.assertIn("adspower_profile", faucet["resources"]["account"])


class BrowserHelperTests(unittest.TestCase):
    def test_extension_url(self) -> None:
        self.assertTrue(is_extension_url("chrome-extension://abc/notification.html"))
        self.assertFalse(is_extension_url("https://fairground.fi/421614"))

    def test_rabby_alert_text(self) -> None:
        self.assertTrue(
            rabby_alert_blocks_sign("Please process the alert before signing Ignore all")
        )
        self.assertFalse(rabby_alert_blocks_sign("Sign Text Confirm Cancel"))

    def test_skips_adspower_assistant_not_rabby(self) -> None:
        self.assertTrue(skip_extension_id("gcaimgaiohinfifkjmcohobkpbbnflaa"))
        self.assertTrue(skip_extension_id("bfnaelmomeimhpmdgjnjophhpkkoljpa"))
        self.assertFalse(skip_extension_id("acmacodkjbdgmoleebolmdjonilkdbch"))

    def test_chain_label_is_not_a_connected_wallet(self) -> None:
        live = (
            "Fairground Arbitrum Sepolia Connect Wallet BTC-USD "
            "Connect your wallet to start trading Funds available 0 USDC 0x"
        )
        self.assertFalse(page_shows_wallet_connected(live, "0xab12cd34ee"))
        privy = "Log in or sign up Google Discord Continue with a wallet"
        self.assertFalse(page_shows_wallet_connected(privy, "0xab12cd34ee"))
        self.assertFalse(
            page_shows_wallet_connected("Sign to verify Connecting Protected by privy", "0xab12cd34ee")
        )
        self.assertFalse(
            page_shows_wallet_connected("Select your wallet Rabby Wallet MetaMask", "0xab12cd34ee")
        )
        self.assertTrue(
            page_shows_wallet_connected("ab12...34ee  One last step Accept", "0xab12cd34ee")
        )
        self.assertFalse(
            page_shows_wallet_connected("ab12...ee90  One last step Accept", "0xab12cd34ee")
        )
        self.assertFalse(
            page_shows_wallet_connected(
                "Log in or sign up Rabby Wallet Last used Continue with a wallet",
                "0xab12cd34ee",
            )
        )
        self.assertFalse(
            page_shows_wallet_connected(
                "Waiting for Rabby Wallet Sign to verify Connecting",
                "0x5f81612c2e710a159ff451178cb261ea5c4acf4d",
            )
        )
        self.assertFalse(
            page_shows_wallet_connected(
                "Successfully connected with Rabby You're good to go",
                "0x5f81612c2e710a159ff451178cb261ea5c4acf4d",
            )
        )
        self.assertTrue(
            page_shows_wallet_connected(
                "Arbitrum Sepolia 0x5f81...cf4d Place order Funds available 9.00K",
                "0x5f81612c2e710a159ff451178cb261ea5c4acf4d",
            )
        )
        self.assertTrue(
            header_shows_address("0xe4ca...9cde Approve funds", "0xe4ca12aa9cde")
        )
        self.assertFalse(
            header_shows_address(
                "Connect your wallet to start trading Connect wallet 79998.82",
                "0x5f81612c2e710a159ff451178cb261ea5c4acf4d",
            )
        )

    def test_click_frames_skip_telegram_and_prefer_privy(self) -> None:
        self.assertEqual(
            click_frame_rank(
                "https://privy.fairground.fi/apps/cmmas5pm501770dl1bnt3bvea/embedded-wallets"
            ),
            0,
        )
        self.assertEqual(click_frame_rank("https://fairground.fi/421614"), 1)
        self.assertGreaterEqual(
            click_frame_rank(
                "https://oauth.telegram.org/embed/@PerpsPrivyProd_bot?origin=https://fairground.fi"
            ),
            90,
        )
        self.assertGreaterEqual(
            click_frame_rank("chrome-extension://acmacodkjbdgmoleebolmdjonilkdbch/notification.html"),
            90,
        )

    def test_connect_waits_out_loading_skeleton(self) -> None:
        loading = (
            "Fairground Perpetuals Arbitrum Sepolia Loading market data... "
            "Connect your wallet to start trading Connect wallet"
        )
        self.assertFalse(site_ready_for_connect(loading))
        ready = (
            "Fairground BTC-USD Long Short Connect wallet "
            "Connect your wallet to start trading Funds available 0 USDC"
        )
        self.assertTrue(site_ready_for_connect(ready))
        self.assertTrue(site_ready_for_connect("Long Short Place order Funds available 10.0K USDC"))
        self.assertFalse(privy_modal_open(ready))
        self.assertTrue(privy_modal_open("Log in or sign up Rabby Wallet Last used"))
        self.assertTrue(privy_modal_open("Sign to verify Connecting Protected by privy"))

    def test_percent_stays_on_ui_chips(self) -> None:
        style = roll_session("0xabc", "run-chip")
        samples = {pick_percent(style, 10, 50) for _ in range(20)}
        self.assertTrue(samples.issubset({10, 25, 50}))


class TimingMarginTests(unittest.TestCase):
    def test_sessions_differ_across_runs(self) -> None:
        a = roll_session("0xabc", "run-1")
        b = roll_session("0xabc", "run-2")
        self.assertEqual(a.family, b.family)
        self.assertTrue(
            a.configure_lo != b.configure_lo
            or a.hold_hi != b.hold_hi
            or a.between_hi != b.between_hi
        )

    def test_hold_stays_inside_hub_range(self) -> None:
        style = roll_session("0xdef", "run-hold")
        samples = {hold_seconds(style, (10, 20)) for _ in range(40)}
        self.assertTrue(samples)
        self.assertTrue(all(10 <= value <= 20 for value in samples))
        self.assertGreaterEqual(len(samples), 3)

    def test_margin_rolls_inside_live_window(self) -> None:
        farm = VolumeFarm.__new__(VolumeFarm)
        farm._tls = type("T", (), {})()
        farm._tls.session = roll_session("0xfeed", "run-margin")
        runtime = FarmRuntimePlan(
            market_id="1",
            market_name="ETH-USD",
            tick_decimals=2,
            leverage=Decimal("10"),
            market_min_trade_size=Decimal("10"),
            market_max_trade_size=Decimal("100000"),
            configured_margin=(Decimal("100"), Decimal("400")),
            effective_margin=(Decimal("120"), Decimal("380")),
            leverage_level="MEDIUM",
            margin_mode="PERCENT",
            configured_margin_percent=(Decimal("12"), Decimal("48")),
            collateral_balance_usdc=Decimal("1000"),
        )
        values = {farm._roll_margin(runtime) for _ in range(30)}
        self.assertTrue(values)
        for value in values:
            self.assertGreaterEqual(value, runtime.effective_margin[0])
            self.assertLessEqual(value, runtime.effective_margin[1])
        self.assertGreaterEqual(len(values), 5)


if __name__ == "__main__":
    unittest.main()
