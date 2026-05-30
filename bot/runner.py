import os
import json
import time
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from strategy import (Candle, evaluate, Params, position_size,
                      session_vwap, ema, session_levels, near_level)

STATE_FILE = os.environ.get("BOT_STATE_FILE", "state.json")
SYMBOL = os.environ.get("BOT_SYMBOL", "AAPL")
POLL_SECONDS = int(os.environ.get("BOT_POLL_SECONDS", "60"))
TRADE_24H = os.environ.get("BOT_24H", "1") == "1"

_lock = threading.Lock()
STATE = {
    "running": False,
    "mode": os.environ.get("BOT_MODE", "signal"),
    "symbol": SYMBOL,
    "params": Params().__dict__,
    "account_start": None,
    "account_value": None,
    "day_pnl": 0.0,
    "killed_for_day": False,
    "open_trade": None,
    "wins": 0,
    "losses": 0,
    "trades": [],
    "last_eval": None,
    "last_error": None,
    "candles": [],        # recent 1m candles for the chart: [t,o,h,l,c]
    "ema9_series": [],     # [t, value] aligned to candles
    "vwap_series": [],     # [t, value]
    "markers": [],         # buy/sell arrows: {time, price, side}
    "session_high": None,
    "session_low": None,
}


def _save():
    with _lock:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(STATE, f)
        except Exception as e:
            STATE["last_error"] = f"save failed: {e}"


def _log_trade(action, reason, details, extra=None):
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "action": action, "reason": reason, "details": details}
    if extra:
        entry.update(extra)
    with _lock:
        STATE["trades"].insert(0, entry)
        STATE["trades"] = STATE["trades"][:200]
    _save()


def in_no_trade_window(now_utc):
    # 24h mode: never blocked. Otherwise restrict to US regular session.
    if TRADE_24H:
        return False
    minutes = now_utc.hour * 60 + now_utc.minute
    open_m, close_m = 13 * 60 + 30, 20 * 60
    if minutes < open_m or minutes >= close_m:
        return True
    if minutes < open_m + 5:
        return True
    if minutes >= close_m - 30:
        return True
    return False


def _params():
    return Params(**STATE["params"])


def cycle(broker):
    now = datetime.now(timezone.utc)
    p = _params()
    acct = broker.account_value()
    with _lock:
        if STATE["account_start"] is None:
            STATE["account_start"] = acct
        STATE["account_value"] = acct
        day_start_val = STATE["account_start"]
        STATE["day_pnl"] = acct - day_start_val
    if STATE["day_pnl"] <= -abs(day_start_val * p.daily_max_loss_pct / 100.0):
        if not STATE["killed_for_day"]:
            broker.close_all()
            with _lock:
                STATE["killed_for_day"] = True
            _log_trade("KILL_SWITCH", f"Daily loss limit ({p.daily_max_loss_pct}%) hit. Halted for the day.", {})
        return
    if in_no_trade_window(now):
        with _lock:
            STATE["last_eval"] = {"time": now.isoformat(timespec="seconds"), "action": "HOLD", "reason": "Outside trading window."}
        _save()
        return

    raw1 = broker.recent_bars(STATE["symbol"], 1, 120)
    c1 = [Candle(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in raw1]
    c5 = [Candle(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in broker.recent_bars(STATE["symbol"], 5, 60)]
    c15 = [Candle(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in broker.recent_bars(STATE["symbol"], 15, 60)]

    # session levels (use the 1m candles we have for this session view)
    levels = session_levels(c1)

    # build chart series: candles + rolling ema9 + rolling vwap
    closes = [c.close for c in c1]
    candle_rows = [[int(c.ts), round(c.open, 2), round(c.high, 2), round(c.low, 2), round(c.close, 2)] for c in c1]
    ema_rows = []
    vwap_rows = []
    for i in range(len(c1)):
        sub = closes[:i + 1]
        e = ema(sub, p.ema_period)
        if e is not None:
            ema_rows.append([int(c1[i].ts), round(e, 2)])
        v = session_vwap(c1[:i + 1])
        if v is not None:
            vwap_rows.append([int(c1[i].ts), round(v, 2)])

    with _lock:
        STATE["candles"] = candle_rows[-120:]
        STATE["ema9_series"] = ema_rows[-120:]
        STATE["vwap_series"] = vwap_rows[-120:]
        STATE["session_high"] = round(levels["high"], 2) if levels["high"] else None
        STATE["session_low"] = round(levels["low"], 2) if levels["low"] else None

    sig = evaluate(c1, c5, c15, p)
    near_hi = near_level(c1[-1].close, levels["high"]) if c1 else False
    near_lo = near_level(c1[-1].close, levels["low"]) if c1 else False
    note = ""
    if near_hi:
        note = "  (price testing session HIGH)"
    elif near_lo:
        note = "  (price testing session LOW)"

    with _lock:
        STATE["last_eval"] = {"time": now.isoformat(timespec="seconds"), "action": sig.action, "reason": sig.reason + note, "details": sig.details}
    _save()
    if sig.action == "HOLD":
        return
    if broker.open_position_qty(STATE["symbol"]) != 0 or STATE["open_trade"]:
        return
    entry = c1[-1].close
    if sig.action == "OPEN_LONG":
        stop = entry * (1 - p.stop_loss_pct / 100)
        tp = entry * (1 + p.take_profit_pct / 100)
        side = "buy"
    else:
        stop = entry * (1 + p.stop_loss_pct / 100)
        tp = entry * (1 - p.take_profit_pct / 100)
        side = "sell"
    qty = position_size(acct, entry, stop, p.risk_per_trade_pct)
    if qty <= 0:
        _log_trade("HOLD", "Position size was 0 (stop too wide for risk %).", sig.details)
        return
    trade_meta = {"entry": round(entry, 2), "stop": round(stop, 2), "take_profit": round(tp, 2), "qty": qty, "side": side}
    marker = {"time": int(c1[-1].ts), "price": round(entry, 2), "side": "buy" if side == "buy" else "sell"}
    if STATE["mode"] == "auto":
        try:
            oid = broker.submit_bracket(STATE["symbol"], qty, side, tp, stop)
            trade_meta["order_id"] = oid
            with _lock:
                STATE["open_trade"] = trade_meta
                STATE["markers"].append(marker)
                STATE["markers"] = STATE["markers"][-100:]
            _log_trade(sig.action, sig.reason + note, sig.details, {**trade_meta, "executed": True})
        except Exception as e:
            with _lock:
                STATE["last_error"] = f"order failed: {e}"
            _log_trade(sig.action, sig.reason + f"  [ORDER FAILED: {e}]", sig.details, {**trade_meta, "executed": False})
    else:
        with _lock:
            STATE["markers"].append(marker)
            STATE["markers"] = STATE["markers"][-100:]
        _log_trade(sig.action, sig.reason + note, sig.details, {**trade_meta, "executed": False, "signal_only": True})


def loop():
    broker = None
    while True:
        if STATE["running"]:
            try:
                if broker is None:
                    from broker import PaperBroker
                    broker = PaperBroker()
                cycle(broker)
                with _lock:
                    STATE["last_error"] = None
            except Exception as e:
                with _lock:
                    STATE["last_error"] = str(e)
                _save()
        time.sleep(POLL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/state"):
            with _lock:
                self._send(200, json.dumps(STATE))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode() if n else "{}"
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        if self.path.startswith("/toggle"):
            with _lock:
                STATE["running"] = not STATE["running"]
                running = STATE["running"]
            _save()
            self._send(200, json.dumps({"running": running}))
        elif self.path.startswith("/mode"):
            m = body.get("mode")
            if m in ("auto", "signal"):
                with _lock:
                    STATE["mode"] = m
                _save()
            self._send(200, json.dumps({"mode": STATE["mode"]}))
        elif self.path.startswith("/params"):
            with _lock:
                for k, v in body.items():
                    if k in STATE["params"]:
                        STATE["params"][k] = v
            _save()
            self._send(200, json.dumps(STATE["params"]))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass


def main():
    threading.Thread(target=loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"Bot API listening on :{port}  (24h={TRADE_24H})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
