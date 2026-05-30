"""
runner_webhook.py - Bot operador que recibe senales de TradingView
por webhook y las registra como paper trades internos (MNQ, $2/punto).
NO toca dinero real.
"""
import os
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE   = os.environ.get("BOT_STATE_FILE", "state_webhook.json")
PORT         = int(os.environ.get("PORT", "8000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "cambia-esto")

DOLLARS_PER_POINT = float(os.environ.get("DOLLARS_PER_POINT", "2"))
CONTRACTS   = int(os.environ.get("CONTRACTS", "6"))
TP1_PTS     = float(os.environ.get("TP1_PTS", "12.5"))
TP2_PTS     = float(os.environ.get("TP2_PTS", "25"))
SL_PTS      = float(os.environ.get("SL_PTS", "12.5"))

_lock = threading.Lock()
STATE = {
    "running": True,
    "symbol": os.environ.get("BOT_SYMBOL", "MNQ1!"),
    "dollars_per_point": DOLLARS_PER_POINT,
    "contracts": CONTRACTS,
    "tp1_pts": TP1_PTS, "tp2_pts": TP2_PTS, "sl_pts": SL_PTS,
    "total_points": 0.0,
    "total_dollars": 0.0,
    "wins": 0, "losses": 0,
    "open_trade": None,
    "trades": [],
    "last_signal": None,
    "last_error": None,
}


def _load():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                STATE.update(json.load(f))
    except Exception as e:
        STATE["last_error"] = f"load failed: {e}"


def _save():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f)
    except Exception as e:
        STATE["last_error"] = f"save failed: {e}"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_trade(action, price):
    side = "long" if action == "OPEN_LONG" else "short"
    if side == "long":
        tp1 = price + TP1_PTS; tp2 = price + TP2_PTS; sl = price - SL_PTS
    else:
        tp1 = price - TP1_PTS; tp2 = price - TP2_PTS; sl = price + SL_PTS
    return {
        "id": len(STATE["trades"]) + 1, "side": side,
        "entry": round(price, 2),
        "tp1": round(tp1, 2), "tp2": round(tp2, 2), "sl": round(sl, 2),
        "contracts": CONTRACTS, "tp1_hit": False, "remaining": CONTRACTS,
        "opened": _now(), "status": "open", "points": 0.0, "dollars": 0.0,
    }


def update_trade(price):
    t = STATE["open_trade"]
    if not t:
        return
    side = t["side"]; half = CONTRACTS // 2

    def close_part(qty, exit_price, label):
        pts = (exit_price - t["entry"]) if side == "long" else (t["entry"] - exit_price)
        dollars = pts * DOLLARS_PER_POINT * qty
        t["points"] += pts * qty
        t["dollars"] += dollars
        t["remaining"] -= qty
        _log_event(f"{label}: cerro {qty} a {round(exit_price,2)} "
                   f"({'+' if pts>=0 else ''}{round(pts,1)} pts, "
                   f"{'+' if dollars>=0 else ''}${round(dollars,0)})")

    if side == "long":
        stop_level = t["entry"] if t["tp1_hit"] else t["sl"]
        if price <= stop_level:
            close_part(t["remaining"], stop_level, "BREAKEVEN" if t["tp1_hit"] else "STOP")
            _finish_trade(); return
        if not t["tp1_hit"] and price >= t["tp1"]:
            close_part(half, t["tp1"], "TP1"); t["tp1_hit"] = True
        if price >= t["tp2"]:
            close_part(t["remaining"], t["tp2"], "TP2"); _finish_trade(); return
    else:
        stop_level = t["entry"] if t["tp1_hit"] else t["sl"]
        if price >= stop_level:
            close_part(t["remaining"], stop_level, "BREAKEVEN" if t["tp1_hit"] else "STOP")
            _finish_trade(); return
        if not t["tp1_hit"] and price <= t["tp1"]:
            close_part(half, t["tp1"], "TP1"); t["tp1_hit"] = True
        if price <= t["tp2"]:
            close_part(t["remaining"], t["tp2"], "TP2"); _finish_trade(); return


def _finish_trade():
    t = STATE["open_trade"]
    t["status"] = "closed"; t["closed"] = _now()
    STATE["total_points"]  += t["points"]
    STATE["total_dollars"] += t["dollars"]
    if t["dollars"] >= 0: STATE["wins"] += 1
    else: STATE["losses"] += 1
    STATE["trades"].insert(0, t)
    STATE["trades"] = STATE["trades"][:200]
    STATE["open_trade"] = None
    _save()


def _log_event(msg):
    STATE.setdefault("events", [])
    STATE["events"].insert(0, {"time": _now(), "msg": msg})
    STATE["events"] = STATE["events"][:100]


def handle_signal(body):
    if body.get("secret") != WEBHOOK_SECRET:
        STATE["last_error"] = "webhook con secret invalido (ignorado)"
        return {"ok": False, "error": "invalid secret"}
    action = body.get("action", "").upper()
    price = body.get("price")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return {"ok": False, "error": "price invalido"}
    STATE["last_signal"] = {"time": _now(), "action": action, "price": price}
    if action in ("TICK", "PRICE") and STATE["open_trade"]:
        update_trade(price); _save(); return {"ok": True, "handled": "tick"}
    if action in ("OPEN_LONG", "OPEN_SHORT"):
        if STATE["open_trade"]:
            _log_event(f"Senal {action} ignorada: ya hay trade abierto.")
            return {"ok": True, "handled": "ignored_open_exists"}
        STATE["open_trade"] = open_trade(action, price)
        _log_event(f"NUEVA senal {action} a {round(price,2)} -> abierto paper de {CONTRACTS} micros")
        _save(); return {"ok": True, "handled": "opened"}
    if action in ("CLOSE", "EXIT") and STATE["open_trade"]:
        update_trade(price); _save(); return {"ok": True, "handled": "close"}
    return {"ok": True, "handled": "noop"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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
        if self.path.startswith("/webhook"):
            with _lock:
                if not STATE["running"]:
                    self._send(200, json.dumps({"ok": True, "paused": True})); return
                result = handle_signal(body)
            self._send(200, json.dumps(result))
        elif self.path.startswith("/toggle"):
            with _lock:
                STATE["running"] = not STATE["running"]; r = STATE["running"]
            _save(); self._send(200, json.dumps({"running": r}))
        elif self.path.startswith("/reset"):
            with _lock:
                STATE["total_points"] = 0.0; STATE["total_dollars"] = 0.0
                STATE["wins"] = 0; STATE["losses"] = 0
                STATE["open_trade"] = None; STATE["trades"] = []; STATE["events"] = []
            _save(); self._send(200, json.dumps({"reset": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass


def main():
    _load()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Bot operador escuchando webhooks en puerto {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()