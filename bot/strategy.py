"""
strategy.py
-----------
The brain. Pure, testable strategy logic with NO broker calls and NO network.
This makes it easy to backtest and reason about. The live runner (runner.py)
feeds candles in; this module decides what to do and explains why.

Design principles (grounded in research on why retail bots fail):
  - Few parameters, not many. Complexity overfits.
  - Multi-timeframe confirmation: 1m signal, 5m + 15m must agree.
  - Trade only with VWAP direction.
  - Risk-based position sizing (NOT fixed share counts).
  - Hard daily loss limit (kill switch).
  - Every decision returns a human-readable 'reason'.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict


# ----------------------------- indicators ---------------------------------

def ema(values: List[float], period: int) -> Optional[float]:
    """Exponential moving average of the most recent `period`+ values."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def session_vwap(candles: List["Candle"]) -> Optional[float]:
    """Volume-weighted average price across the candles given (one session)."""
    if not candles:
        return None
    pv = 0.0
    vol = 0.0
    for c in candles:
        typical = (c.high + c.low + c.close) / 3
        pv += typical * c.volume
        vol += c.volume
    if vol == 0:
        return None
    return pv / vol


# ------------------------------- data --------------------------------------

@dataclass
class Candle:
    ts: float          # epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class WickAnalysis:
    body: float
    lower_ratio: float    # lower wick length / body
    upper_ratio: float    # upper wick length / body
    close_pos: float      # where close sits in the candle range (0 bottom .. 1 top)

    @staticmethod
    def of(c: Candle) -> "WickAnalysis":
        body = abs(c.close - c.open) or 1e-9
        lower = min(c.open, c.close) - c.low
        upper = c.high - max(c.open, c.close)
        rng = (c.high - c.low) or 1e-9
        return WickAnalysis(
            body=body,
            lower_ratio=lower / body,
            upper_ratio=upper / body,
            close_pos=(c.close - c.low) / rng,
        )


@dataclass
class Params:
    ema_period: int = 9
    wick_strictness: float = 2.0     # min wick:body ratio to confirm
    close_pos_long: float = 0.60     # close must be in top 40% for a long
    close_pos_short: float = 0.40    # close must be in bottom 40% for a short
    take_profit_pct: float = 2.0     # % move to take profit
    stop_loss_pct: float = 1.0       # % move to stop out
    risk_per_trade_pct: float = 1.0  # % of account risked per trade (sizing)
    daily_max_loss_pct: float = 4.0  # kill switch: stop for the day at this loss
    use_partial_tp: bool = True      # take half at 1:1, move stop to breakeven


@dataclass
class Signal:
    action: str                      # "OPEN_LONG" | "OPEN_SHORT" | "HOLD"
    reason: str
    details: Dict = field(default_factory=dict)


def _direction_bias(tf_close: float, tf_vwap: float) -> str:
    if tf_vwap is None:
        return "NONE"
    return "UP" if tf_close > tf_vwap else "DOWN"


def evaluate(
    candles_1m: List[Candle],
    candles_5m: List[Candle],
    candles_15m: List[Candle],
    params: Params,
) -> Signal:
    """
    Decide whether to OPEN a trade on the 1m trigger, confirmed by 5m & 15m.
    Returns a Signal with a plain-English reason either way.
    """
    if len(candles_1m) < params.ema_period + 1:
        return Signal("HOLD", "Not enough 1m data yet to compute EMA.")

    closes_1m = [c.close for c in candles_1m]
    ema_now = ema(closes_1m, params.ema_period)
    ema_prev = ema(closes_1m[:-1], params.ema_period)
    vwap_1m = session_vwap(candles_1m)
    vwap_5m = session_vwap(candles_5m)
    vwap_15m = session_vwap(candles_15m)

    if None in (ema_now, ema_prev, vwap_1m, vwap_5m, vwap_15m):
        return Signal("HOLD", "Indicators not ready (need full session data).")

    last = candles_1m[-1]
    w = WickAnalysis.of(last)

    crossed_up = ema_prev <= vwap_1m and ema_now > vwap_1m
    crossed_dn = ema_prev >= vwap_1m and ema_now < vwap_1m

    bias_5 = _direction_bias(candles_5m[-1].close, vwap_5m)
    bias_15 = _direction_bias(candles_15m[-1].close, vwap_15m)

    base = {
        "ema9": round(ema_now, 2),
        "vwap_1m": round(vwap_1m, 2),
        "bias_5m": bias_5,
        "bias_15m": bias_15,
        "lower_wick_x": round(w.lower_ratio, 2),
        "upper_wick_x": round(w.upper_ratio, 2),
        "close_pos": round(w.close_pos, 2),
    }

    if crossed_up:
        wick_ok = w.lower_ratio >= params.wick_strictness and w.close_pos >= params.close_pos_long
        tf_ok = bias_5 == "UP" and bias_15 == "UP"
        if wick_ok and tf_ok:
            return Signal(
                "OPEN_LONG",
                (f"1m EMA9 ({base['ema9']}) crossed ABOVE VWAP ({base['vwap_1m']}); "
                 f"lower wick {base['lower_wick_x']}x body, close in top "
                 f"{round((1 - w.close_pos) * 100)}% of range; 5m and 15m both bullish."),
                base,
            )
        why = []
        if not tf_ok:
            why.append(f"5m={bias_5}/15m={bias_15} not both UP")
        if not wick_ok:
            why.append("wick/close didn't confirm bullishness")
        return Signal("HOLD", "1m crossed up but " + "; ".join(why) + ".", base)

    if crossed_dn:
        wick_ok = w.upper_ratio >= params.wick_strictness and w.close_pos <= params.close_pos_short
        tf_ok = bias_5 == "DOWN" and bias_15 == "DOWN"
        if wick_ok and tf_ok:
            return Signal(
                "OPEN_SHORT",
                (f"1m EMA9 ({base['ema9']}) crossed BELOW VWAP ({base['vwap_1m']}); "
                 f"upper wick {base['upper_wick_x']}x body, close in bottom "
                 f"{round(w.close_pos * 100)}% of range; 5m and 15m both bearish."),
                base,
            )
        why = []
        if not tf_ok:
            why.append(f"5m={bias_5}/15m={bias_15} not both DOWN")
        if not wick_ok:
            why.append("wick/close didn't confirm bearishness")
        return Signal("HOLD", "1m crossed down but " + "; ".join(why) + ".", base)

    return Signal("HOLD", "No EMA9/VWAP cross on the 1m this bar.", base)


def position_size(account_value: float, entry: float, stop: float,
                  risk_pct: float) -> int:
    """
    Risk-based sizing: lose exactly risk_pct of the account if stopped out.
    size = (account * risk%) / per-share risk.  Returns whole shares.
    This single rule matters more than any indicator.
    """
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0:
        return 0
    dollars_at_risk = account_value * (risk_pct / 100.0)
    return max(0, int(dollars_at_risk // per_share_risk))


# --------------------- session high/low levels (v2) ------------------------

def session_levels(candles: List["Candle"]) -> Dict:
    """
    Compute the high and low of the candles given (one session's worth).
    These are levels many traders watch. The runner passes the current
    session's candles in, and the bot logs/considers proximity to them.
    """
    if not candles:
        return {"high": None, "low": None}
    return {"high": max(c.high for c in candles),
            "low": min(c.low for c in candles)}


def near_level(price: float, level: float, tol_pct: float = 0.1) -> bool:
    """True if price is within tol_pct% of a level (a 'test' of that level)."""
    if level is None or level == 0:
        return False
    return abs(price - level) / level * 100 <= tol_pct
