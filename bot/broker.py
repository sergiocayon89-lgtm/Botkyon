import os
from typing import List
from dataclasses import dataclass


@dataclass
class Bar:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


class PaperBroker:
    def __init__(self):
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
        if not key or not secret:
            raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_API_SECRET environment variables.")
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        self._trading = TradingClient(key, secret, paper=True)
        self._data = StockHistoricalDataClient(key, secret)

    def account_value(self):
        acct = self._trading.get_account()
        return float(acct.equity)

    def open_position_qty(self, symbol):
        try:
            pos = self._trading.get_open_position(symbol)
            return int(float(pos.qty))
        except Exception:
            return 0

    def recent_bars(self, symbol, timeframe_minutes, limit=60):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from datetime import datetime, timedelta, timezone
        tf = TimeFrame(timeframe_minutes, TimeFrameUnit.Minute)
        start = datetime.now(timezone.utc) - timedelta(minutes=timeframe_minutes * (limit + 5))
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start)
        bars = self._data.get_stock_bars(req)
        out = []
        for b in bars[symbol]:
            out.append(Bar(b.timestamp.timestamp(), b.open, b.high, b.low, b.close, b.volume))
        return out[-limit:]

    def submit_bracket(self, symbol, qty, side, take_profit, stop_loss):
        from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
        order = MarketOrderRequest(
            symbol=symbol, qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)))
        resp = self._trading.submit_order(order)
        return str(resp.id)

    def close_all(self):
        self._trading.close_all_positions(cancel_orders=True)
        