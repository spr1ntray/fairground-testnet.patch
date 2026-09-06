"""Per-account session cadence. Not a Hub option — rolled automatically."""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass
from typing import Callable

CancelCheck = Callable[[], None] | None


@dataclass(frozen=True)
class SessionStyle:
    family: str
    start_lo: float
    start_hi: float
    configure_lo: float
    configure_hi: float
    action_lo: float
    action_hi: float
    hold_lo: float
    hold_hi: float
    think_p: float
    think_extra_lo: float
    think_extra_hi: float
    http_lo: float
    http_hi: float
    between_lo: float
    between_hi: float
    after_lo: float
    after_hi: float
    long_bias: float
    margin_style: str
    margin_low_weight: float
    skip_last_p: float
    offset_bps_lo: int
    offset_bps_hi: int
    rng: random.Random

    def public(self) -> dict[str, object]:
        return {
            "family": self.family,
            "hold": f"{self.hold_lo:.0f}-{self.hold_hi:.0f}s",
            "margin": self.margin_style,
            "long_bias": round(self.long_bias, 2),
        }


def _rng(*parts: str) -> random.Random:
    blob = "|".join(parts).encode("utf-8")
    return random.Random(int(hashlib.sha256(blob).hexdigest()[:16], 16))


def roll_session(address: str = "", run_id: str = "") -> SessionStyle:
    """Stable family per wallet, fresh jitter every run."""

    stable = _rng("fairground-s3", (address or "anon").lower())
    jitter = _rng("fairground-s3", (address or "anon").lower(), run_id or str(time.time_ns()))
    family = stable.choice(("snappy", "steady", "leisure", "bursty", "night", "cautious"))

    if family == "snappy":
        action, start, configure, hold, between, think_p = (
            (2.4, 8.5), (0.2, 2.4), (4.0, 14.0), (6.0, 18.0), (1.5, 6.0), 0.07,
        )
        margin_style = "steps"
        long_bias = stable.uniform(0.42, 0.62)
    elif family == "leisure":
        action, start, configure, hold, between, think_p = (
            (11.0, 34.0), (2.5, 12.0), (18.0, 52.0), (28.0, 95.0), (8.0, 28.0), 0.26,
        )
        margin_style = "conservative"
        long_bias = stable.uniform(0.35, 0.58)
    elif family == "bursty":
        action, start, configure, hold, between, think_p = (
            (1.6, 26.0), (0.3, 8.0), (3.0, 38.0), (4.0, 55.0), (1.0, 22.0), 0.18,
        )
        margin_style = "spread"
        long_bias = stable.uniform(0.28, 0.72)
    elif family == "night":
        action, start, configure, hold, between, think_p = (
            (8.0, 24.0), (4.0, 16.0), (12.0, 40.0), (18.0, 70.0), (6.0, 24.0), 0.22,
        )
        margin_style = "steps"
        long_bias = stable.uniform(0.40, 0.60)
    elif family == "cautious":
        action, start, configure, hold, between, think_p = (
            (9.0, 22.0), (1.5, 7.0), (14.0, 44.0), (16.0, 48.0), (5.0, 18.0), 0.16,
        )
        margin_style = "conservative"
        long_bias = stable.uniform(0.48, 0.70)
    else:
        action, start, configure, hold, between, think_p = (
            (5.5, 16.0), (0.6, 5.5), (8.0, 28.0), (12.0, 36.0), (3.0, 12.0), 0.12,
        )
        margin_style = "spread"
        long_bias = stable.uniform(0.38, 0.62)

    def spread(lo: float, hi: float, *, floor: float = 0.15) -> tuple[float, float]:
        a = max(floor, lo * jitter.uniform(0.55, 1.28))
        b = max(a + 0.4, hi * jitter.uniform(0.78, 1.45))
        return a, b

    slo, shi = spread(*start, floor=0.05)
    clo, chi = spread(*configure, floor=1.2)
    alo, ahi = spread(*action)
    hlo, hhi = spread(*hold, floor=3.0)
    blo, bhi = spread(*between, floor=0.6)
    after_lo, after_hi = spread(2.0, 14.0, floor=0.8)
    http_lo, http_hi = spread(0.12, 2.6, floor=0.05)

    # Occasional long hold for rebate-style sitting.
    if jitter.random() < 0.16:
        hhi = max(hhi, jitter.uniform(70.0, 160.0))
    # Occasional short poke.
    if jitter.random() < 0.12:
        hlo = max(3.0, hlo * jitter.uniform(0.35, 0.7))

    offset_lo = int(jitter.choice((120, 150, 160, 180)))
    offset_hi = int(max(offset_lo + 20, jitter.choice((200, 220, 240, 250, 280))))

    return SessionStyle(
        family=family,
        start_lo=slo,
        start_hi=shi,
        configure_lo=clo,
        configure_hi=chi,
        action_lo=alo,
        action_hi=ahi,
        hold_lo=hlo,
        hold_hi=hhi,
        think_p=min(0.42, think_p * jitter.uniform(0.45, 1.7)),
        think_extra_lo=jitter.uniform(2.5, 8.0),
        think_extra_hi=jitter.uniform(9.0, 24.0),
        http_lo=http_lo,
        http_hi=http_hi,
        between_lo=blo,
        between_hi=bhi,
        after_lo=after_lo,
        after_hi=after_hi,
        long_bias=long_bias,
        margin_style=margin_style,
        margin_low_weight=jitter.uniform(0.15, 0.72),
        skip_last_p=0.11 if jitter.random() < 0.35 else 0.0,
        offset_bps_lo=offset_lo,
        offset_bps_hi=offset_hi,
        rng=jitter,
    )


def sleep_jitter(
    lo: float,
    hi: float,
    *,
    cancel_check: CancelCheck = None,
) -> float:
    lo_f = max(0.0, float(lo))
    hi_f = max(lo_f, float(hi))
    if hi_f <= 0:
        return 0.0
    # Log-leaning draw so most waits sit below the midpoint.
    span = hi_f - lo_f
    if span > 0:
        unit = random.random() ** random.uniform(0.55, 1.35)
        delay = lo_f + span * unit
    else:
        delay = lo_f
    deadline = time.monotonic() + delay
    while True:
        if cancel_check is not None:
            cancel_check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.35, remaining))
    return delay


def account_start_delay(
    *,
    cancel_check: CancelCheck = None,
    style: SessionStyle | None = None,
) -> float:
    if style:
        return sleep_jitter(style.start_lo, style.start_hi, cancel_check=cancel_check)
    return sleep_jitter(0.4, 6.5, cancel_check=cancel_check)


def configure_delay(
    *,
    cancel_check: CancelCheck = None,
    style: SessionStyle | None = None,
) -> float:
    if style:
        delay = sleep_jitter(style.configure_lo, style.configure_hi, cancel_check=cancel_check)
        if style.rng.random() < style.think_p:
            delay += sleep_jitter(
                style.think_extra_lo, style.think_extra_hi, cancel_check=cancel_check
            )
        return delay
    return sleep_jitter(6.0, 28.0, cancel_check=cancel_check)


def pre_http_delay(
    *,
    cancel_check: CancelCheck = None,
    style: SessionStyle | None = None,
) -> float:
    if style:
        return sleep_jitter(style.http_lo, style.http_hi, cancel_check=cancel_check)
    return sleep_jitter(0.2, 2.1, cancel_check=cancel_check)


def between_rounds_delay(
    *,
    cancel_check: CancelCheck = None,
    style: SessionStyle | None = None,
    fallback: tuple[int, int] | None = None,
) -> float:
    if style:
        delay = sleep_jitter(style.between_lo, style.between_hi, cancel_check=cancel_check)
        if style.rng.random() < style.think_p:
            delay += sleep_jitter(
                style.think_extra_lo, style.think_extra_hi, cancel_check=cancel_check
            )
        return delay
    lo, hi = fallback or (2, 8)
    return sleep_jitter(float(lo), float(hi), cancel_check=cancel_check)


def after_account_delay(
    *,
    cancel_check: CancelCheck = None,
    style: SessionStyle | None = None,
    fallback: tuple[int, int] | None = None,
) -> float:
    if style:
        return sleep_jitter(style.after_lo, style.after_hi, cancel_check=cancel_check)
    lo, hi = fallback or (2, 8)
    return sleep_jitter(float(lo), float(hi), cancel_check=cancel_check)


def hold_seconds(style: SessionStyle | None, fallback: tuple[int, int]) -> int:
    lo, hi = int(fallback[0]), int(fallback[1])
    if hi < lo:
        lo, hi = hi, lo
    lo = max(3, lo)
    hi = max(lo, hi)
    rng = style.rng if style is not None else random
    if style is not None:
        # Session only shapes the draw inside the Hub range.
        inner_lo = lo + int((hi - lo) * min(0.45, style.hold_lo / max(style.hold_hi, 1.0) * 0.25))
        inner_hi = hi
        if rng.random() < 0.1:
            return int(min(hi, max(lo, inner_hi - rng.randint(0, max(1, (hi - lo) // 6)))))
        unit = rng.random() ** rng.uniform(0.45, 1.45)
        return int(inner_lo + (inner_hi - inner_lo) * unit)
    return int(rng.randint(lo, hi))


def parse_account_gap(*, cancel_check: CancelCheck = None) -> float:
    return sleep_jitter(0.12, 1.8, cancel_check=cancel_check)
