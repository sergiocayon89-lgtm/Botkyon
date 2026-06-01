"""
runner_webhook.py - PAPER TRADING COMPLETO Y AUTOMATICO
Balance inicial $100,000, 6 micros MNQ ($2/punto), TP1+12.5/TP2+25/SL-12.5.
"""
import os, json, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE = os.environ.get("BOT_STATE_FILE", "state_webhook.json")
PORT = int(os.environ.get("PORT", "8000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "cambia-esto")
DOLLARS_PER_POINT = float(os.environ.get("DOLLARS_PER_POINT", "2"))
CONTRACTS = int(os.environ.get("CONTRACTS", "6"))
TP1_PTS = float(os.environ.get("TP1_PTS", "12.5"))
TP2_PTS = float(os.environ.get("TP2_PTS", "25"))
SL_PTS = float(os.environ.get("SL_PTS", "12.5"))
START_BALANCE = float(os.environ.get("START_BALANCE", "100000"))

_lock = threading.Lock()
STATE = {
    "running": True, "symbol": os.environ.get("BOT_SYMBOL", "MNQ1!"),
    "dollars_per_point": DOLLARS_PER_POINT, "contracts": CONTRACTS,
    "tp1_pts": TP1_PTS, "tp2_pts": TP2_PTS, "sl_pts": SL_PTS,
    "start_balance": START_BALANCE, "balance": START_BALANCE, "equity": START_BALANCE,
    "total_points": 0.0, "total_dollars": 0.0, "wins": 0, "losses": 0,
    "open_trade": None, "trades": [], "events": [],
    "last_signal": None, "last_price": None, "last_error": None,
    "last_vwap": None, "last_ema9": None, "last_ema21": None,
}

def _load():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f: STATE.update(json.load(f))
    except Exception as e: STATE["last_error"] = f"load failed: {e}"

def _save():
    try:
        with open(STATE_FILE, "w") as f: json.dump(STATE, f)
    except Exception as e: STATE["last_error"] = f"save failed: {e}"

def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _log_event(msg):
    STATE["events"].insert(0, {"time": _now(), "msg": msg})
    STATE["events"] = STATE["events"][:100]

def open_trade(action, price):
    side = "long" if action == "OPEN_LONG" else "short"
    if side == "long":
        tp1, tp2, sl = price+TP1_PTS, price+TP2_PTS, price-SL_PTS
    else:
        tp1, tp2, sl = price-TP1_PTS, price-TP2_PTS, price+SL_PTS
    return {"id": len(STATE["trades"])+1, "side": side, "entry": round(price,2),
            "tp1": round(tp1,2), "tp2": round(tp2,2), "sl": round(sl,2),
            "contracts": CONTRACTS, "tp1_hit": False, "remaining": CONTRACTS,
            "opened": _now(), "status": "open", "points": 0.0, "dollars": 0.0}

def _floating_dollars(t, price):
    if not t: return 0.0
    pts = (price-t["entry"]) if t["side"]=="long" else (t["entry"]-price)
    return pts * DOLLARS_PER_POINT * t["remaining"]

def update_trade(price):
    t = STATE["open_trade"]
    if not t: return
    side = t["side"]; half = CONTRACTS // 2
    def close_part(qty, ep, label):
        pts = (ep-t["entry"]) if side=="long" else (t["entry"]-ep)
        d = pts * DOLLARS_PER_POINT * qty
        t["points"] += pts*qty; t["dollars"] += d; t["remaining"] -= qty
        STATE["balance"] += d
        _log_event(f"{label}: {qty} a {round(ep,2)} ({'+' if d>=0 else ''}${round(d,0)}) | bal ${round(STATE['balance'],0)}")
    if side == "long":
        sl_lvl = t["entry"] if t["tp1_hit"] else t["sl"]
        if price <= sl_lvl:
            close_part(t["remaining"], sl_lvl, "BREAKEVEN" if t["tp1_hit"] else "STOP"); _finish_trade(); return
        if not t["tp1_hit"] and price >= t["tp1"]:
            close_part(half, t["tp1"], "TP1"); t["tp1_hit"] = True
        if price >= t["tp2"]:
            close_part(t["remaining"], t["tp2"], "TP2"); _finish_trade(); return
    else:
        sl_lvl = t["entry"] if t["tp1_hit"] else t["sl"]
        if price >= sl_lvl:
            close_part(t["remaining"], sl_lvl, "BREAKEVEN" if t["tp1_hit"] else "STOP"); _finish_trade(); return
        if not t["tp1_hit"] and price <= t["tp1"]:
            close_part(half, t["tp1"], "TP1"); t["tp1_hit"] = True
        if price <= t["tp2"]:
            close_part(t["remaining"], t["tp2"], "TP2"); _finish_trade(); return

def _finish_trade():
    t = STATE["open_trade"]
    t["status"]="closed"; t["closed"]=_now()
    STATE["total_points"] += t["points"]; STATE["total_dollars"] += t["dollars"]
    if t["dollars"]>=0: STATE["wins"] += 1
    else: STATE["losses"] += 1
    STATE["trades"].insert(0, t); STATE["trades"]=STATE["trades"][:200]
    STATE["open_trade"]=None; STATE["equity"]=STATE["balance"]; _save()

def handle_signal(body):
    if body.get("secret") != WEBHOOK_SECRET:
        STATE["last_error"]="secret invalido"; return {"ok": False, "error": "invalid secret"}
    action = body.get("action","").upper()
    try: price = float(body.get("price"))
    except (TypeError, ValueError): return {"ok": False, "error": "price invalido"}
    STATE["last_price"]=round(price,2)
    # indicadores reales si TradingView los manda en el webhook (vwap, ema9, ema21)
    for _k in ("vwap","ema9","ema21"):
        _v = body.get(_k)
        if _v is not None:
            try: STATE["last_"+_k] = round(float(_v),2)
            except (TypeError, ValueError): pass
    STATE["last_signal"]={"time": _now(), "action": action, "price": round(price,2),
                          "vwap": STATE.get("last_vwap"), "ema9": STATE.get("last_ema9"), "ema21": STATE.get("last_ema21")}
    if action in ("TICK","PRICE"):
        if STATE["open_trade"]:
            update_trade(price)
            STATE["equity"]=STATE["balance"]+_floating_dollars(STATE["open_trade"], price)
        _save(); return {"ok": True, "handled": "tick"}
    if action in ("OPEN_LONG","OPEN_SHORT"):
        if STATE["open_trade"]:
            _log_event(f"{action} ignorada: ya hay trade abierto."); return {"ok": True, "handled": "ignored"}
        STATE["open_trade"]=open_trade(action, price)
        ot=STATE["open_trade"]
        _log_event(f"NUEVA {action} a {round(price,2)} -> {CONTRACTS} micros (TP1 {ot['tp1']}, TP2 {ot['tp2']}, SL {ot['sl']})")
        _save(); return {"ok": True, "handled": "opened"}
    if action in ("CLOSE","EXIT") and STATE["open_trade"]:
        update_trade(price); _save(); return {"ok": True, "handled": "close"}
    return {"ok": True, "handled": "noop"}

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
        self.wfile.write(body.encode())
    def do_GET(self):
        if self.path.startswith("/state"):
            with _lock: self._send(200, json.dumps(STATE))
        else: self._send(404, json.dumps({"error":"not found"}))
    def do_POST(self):
        n=int(self.headers.get("Content-Length",0))
        raw=self.rfile.read(n).decode() if n else "{}"
        try: body=json.loads(raw) if raw else {}
        except Exception: body={}
        if self.path.startswith("/webhook"):
            with _lock:
                if not STATE["running"]: self._send(200, json.dumps({"ok":True,"paused":True})); return
                r=handle_signal(body)
            self._send(200, json.dumps(r))
        elif self.path.startswith("/toggle"):
            with _lock: STATE["running"]=not STATE["running"]; r=STATE["running"]
            _save(); self._send(200, json.dumps({"running": r}))
        elif self.path.startswith("/reset"):
            with _lock:
                STATE["balance"]=START_BALANCE; STATE["equity"]=START_BALANCE
                STATE["total_points"]=0.0; STATE["total_dollars"]=0.0
                STATE["wins"]=0; STATE["losses"]=0
                STATE["open_trade"]=None; STATE["trades"]=[]; STATE["events"]=[]
            _save(); self._send(200, json.dumps({"reset": True}))
        else: self._send(404, json.dumps({"error":"not found"}))
    def log_message(self,*a): pass

def main():
    _load()
    s=ThreadingHTTPServer(("0.0.0.0",PORT), Handler)
    print(f"Paper trading bot en puerto {PORT}, balance ${START_BALANCE}, {CONTRACTS} micros")
    s.serve_forever()

if __name__=="__main__": main()
