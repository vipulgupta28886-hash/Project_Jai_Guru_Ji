from dataclasses import asdict, dataclass
from datetime import datetime, datetime as real_datetime, time, timedelta
from pathlib import Path
import json
import os
import uuid
from typing import Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
DATA_FILE = Path("data/paper_trades.json")
DATA_FILE.parent.mkdir(exist_ok=True)

MAX_POSITIONS = 2
MIN_PREMIUM_OUTLAY = 30000
MAX_PREMIUM_OUTLAY = 35000
MAX_LOSS_PER_TRADE = 2000
MAX_DAILY_LOSS = 4000
DAILY_PROFIT_LOCK = 6000
MAX_TRADES_PER_DAY = 3
MAX_WEEKLY_LOSS = 10000

ENTRY_START = time(10, 0)
ENTRY_END = time(14, 0)
SQUARE_OFF_TIME = time(14, 30)
ALLOWED_WEEKDAYS = (0, 1, 2)  # Monday, Tuesday, Wednesday

app = FastAPI(title="Jai Guru Ji F&O Paper Agent")


@dataclass
class PaperTrade:
    id: str
    instrument: str
    direction: str
    premium_outlay: float
    opened_at: str
    stop_loss: float = -MAX_LOSS_PER_TRADE
    target: float | None = None
    status: str = "OPEN"
    pnl: float = 0.0


def load_trades() -> list[PaperTrade]:
    if not DATA_FILE.exists():
        return []

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return [PaperTrade(**trade) for trade in data]
    except (json.JSONDecodeError, TypeError, KeyError):
        return []


trades: list[PaperTrade] = load_trades()
kite_client: KiteConnect | None = None


class Signal(BaseModel):
    instrument: Literal["NIFTY", "BANKNIFTY"]
    direction: Literal["CE", "PE"]
    premium_outlay: float = Field(
        ge=MIN_PREMIUM_OUTLAY,
        le=MAX_PREMIUM_OUTLAY,
    )
    vwap_confirmed: bool
    ema_trend_confirmed: bool
    opening_range_breakout: bool
    volume_confirmed: bool


class CloseTrade(BaseModel):
    pnl: float = Field(
        ge=-MAX_LOSS_PER_TRADE,
        description="Paper trade loss cannot exceed the configured stop loss.",
    )


def trade_datetime(trade: PaperTrade) -> datetime:
    return datetime.fromisoformat(trade.opened_at)


def is_today(trade: PaperTrade) -> bool:
    return trade_datetime(trade).date() == datetime.now(IST).date()


def is_this_week(trade: PaperTrade) -> bool:
    now = datetime.now(IST)
    start_of_week = now.date().fromordinal(now.date().toordinal() - now.weekday())
    return trade_datetime(trade).date() >= start_of_week


def daily_pnl() -> float:
    return sum(trade.pnl for trade in trades if is_today(trade))


def weekly_pnl() -> float:
    return sum(trade.pnl for trade in trades if is_this_week(trade))


def open_positions() -> int:
    return sum(trade.status == "OPEN" for trade in trades)


def trades_today() -> int:
    return sum(is_today(trade) for trade in trades)


def open_risk() -> int:
    return open_positions() * MAX_LOSS_PER_TRADE


def save_trades() -> None:
    DATA_FILE.write_text(
        json.dumps([asdict(trade) for trade in trades], indent=2),
        encoding="utf-8",
    )


def _resolve_futures_contract(instrument: Literal["NIFTY", "BANKNIFTY"]) -> dict:
    if kite_client is None or not getattr(kite_client, "access_token", None):
        raise HTTPException(status_code=401, detail="Please log in through /kite/login first.")

    underlying_name = instrument
    contracts = kite_client.instruments(exchange="NFO") or []
    matching_contracts = []
    for item in contracts:
        if str(item.get("exchange", "")).upper() != "NFO":
            continue
        if str(item.get("segment", "")).upper() != "NFO-FUT":
            continue
        if str(item.get("instrument_type", "")).upper() != "FUT":
            continue
        if str(item.get("name", "")).upper() != underlying_name:
            continue

        expiry = item.get("expiry")
        if not expiry:
            continue
        try:
            expiry_dt = datetime.strptime(str(expiry), "%Y-%m-%d").date()
        except ValueError:
            continue
        if expiry_dt >= datetime.now(IST).date():
            matching_contracts.append((expiry_dt, item))

    if not matching_contracts:
        raise HTTPException(status_code=404, detail=f"No NFO futures contract found for {instrument}.")

    matching_contracts.sort(key=lambda pair: pair[0])
    _, contract = matching_contracts[0]
    return {
        "trading_symbol": contract.get("tradingsymbol"),
        "symbol": contract.get("tradingsymbol"),
        "expiry": contract.get("expiry"),
        "instrument_token": int(contract["instrument_token"]),
        "contract_type": str(contract.get("instrument_type", "")).upper(),
    }


def _calculate_ema(prices: list[float], period: int) -> float | None:
    if not prices:
        return None

    multiplier = 2 / (period + 1)
    ema_value = prices[0]
    for price in prices[1:]:
        ema_value = (price * multiplier) + (ema_value * (1 - multiplier))
    return ema_value


def _scan_signal(instrument: Literal["NIFTY", "BANKNIFTY"]) -> dict:
    if kite_client is None or not getattr(kite_client, "access_token", None):
        raise HTTPException(status_code=401, detail="Please log in through /kite/login first.")

    now = datetime.now(IST)
    if now.weekday() not in ALLOWED_WEEKDAYS:
        return {
            "instrument": instrument,
            "signal": "WAIT",
            "mode": "PAPER_TRADING_ONLY",
            "entry_window_open": False,
            "message": "No trade was created. Outside permitted entry days.",
            "selected_contract": None,
            "metrics": {},
            "conditions": {},
        }

    if not ENTRY_START <= now.time() <= ENTRY_END:
        return {
            "instrument": instrument,
            "signal": "WAIT",
            "mode": "PAPER_TRADING_ONLY",
            "entry_window_open": False,
            "message": "No trade was created. Outside the 10:00–14:00 IST entry window.",
            "selected_contract": None,
            "metrics": {},
            "conditions": {},
        }

    contract = _resolve_futures_contract(instrument)
    instrument_token = contract["instrument_token"]
    from_date = now - timedelta(days=3)
    to_date = now

    try:
        candles = kite_client.historical_data(
            instrument_token,
            from_date,
            to_date,
            "5minute",
        )
    except Exception as exc:  # pragma: no cover - defensive path
        raise HTTPException(
            status_code=502,
            detail=f"Kite Connect historical data request failed: {exc}",
        ) from exc

    if not candles:
        raise HTTPException(status_code=502, detail="No 5-minute candles were returned by Kite Connect.")

    recent_candles = [
        c
        for c in candles
        if isinstance(c.get("date"), (datetime, real_datetime))
    ]
    if not recent_candles:
        raise HTTPException(status_code=502, detail="Kite Connect returned candles without usable timestamps.")

    recent_candles = recent_candles[-60:]
    if len(recent_candles) < 21:
        raise HTTPException(status_code=502, detail="Insufficient futures candle history for the scanner.")

    latest = recent_candles[-1]
    latest_volume = float(latest.get("volume", 0) or 0)
    if latest_volume <= 0:
        return {
            "instrument": instrument,
            "signal": "WAIT",
            "mode": "PAPER_TRADING_ONLY",
            "entry_window_open": True,
            "message": "No trade was created. Volume unavailable for the latest completed candle.",
            "selected_contract": contract,
            "metrics": {},
            "conditions": {},
        }

    closes = [float(c["close"]) for c in recent_candles]
    volumes = [float(c.get("volume", 0) or 0) for c in recent_candles]
    typical_prices = [((float(c["high"]) + float(c["low"]) + float(c["close"])) / 3) * float(c.get("volume", 0) or 0) for c in recent_candles]
    total_weighted_price = sum(typical_prices)
    total_volume = sum(volumes)
    vwap = total_weighted_price / total_volume if total_volume else None

    ema_20 = _calculate_ema(closes, 20)
    ema_50 = _calculate_ema(closes, 50)

    opening_range_candles = [
        c
        for c in recent_candles
        if c["date"].date() == now.date()
        and time(9, 15) <= c["date"].time() < time(9, 45)
    ]
    if opening_range_candles:
        opening_range_high = max(float(c["high"]) for c in opening_range_candles)
        opening_range_low = min(float(c["low"]) for c in opening_range_candles)
    else:
        opening_range_high = None
        opening_range_low = None

    previous_volumes = volumes[-21:-1]
    average_previous_volume = sum(previous_volumes) / len(previous_volumes) if previous_volumes else 0
    volume_confirmed = bool(average_previous_volume > 0 and latest_volume >= average_previous_volume * 1.5)

    latest_close = float(latest["close"])
    ce_vwap_confirmed = vwap is not None and latest_close > vwap
    ce_ema_trend_confirmed = ema_20 is not None and ema_50 is not None and ema_20 > ema_50
    ce_opening_range_breakout = opening_range_high is not None and latest_close > opening_range_high
    ce_volume_confirmed = volume_confirmed

    pe_vwap_confirmed = vwap is not None and latest_close < vwap
    pe_ema_trend_confirmed = ema_20 is not None and ema_50 is not None and ema_20 < ema_50
    pe_opening_range_breakout = opening_range_low is not None and latest_close < opening_range_low
    pe_volume_confirmed = volume_confirmed

    signal = "WAIT"
    if all([
        ce_vwap_confirmed,
        ce_ema_trend_confirmed,
        ce_opening_range_breakout,
        ce_volume_confirmed,
    ]):
        signal = "CE_SIGNAL"
    elif all([
        pe_vwap_confirmed,
        pe_ema_trend_confirmed,
        pe_opening_range_breakout,
        pe_volume_confirmed,
    ]):
        signal = "PE_SIGNAL"

    return {
        "instrument": instrument,
        "signal": signal,
        "mode": "PAPER_TRADING_ONLY",
        "entry_window_open": True,
        "message": "No trade was created. Use /paper/signal to create a paper trade.",
        "selected_contract": contract,
        "metrics": {
            "futures_vwap": round(vwap, 2) if vwap is not None else None,
            "futures_ema20": round(ema_20, 2) if ema_20 is not None else None,
            "futures_ema50": round(ema_50, 2) if ema_50 is not None else None,
            "opening_range_high": round(opening_range_high, 2) if opening_range_high is not None else None,
            "opening_range_low": round(opening_range_low, 2) if opening_range_low is not None else None,
            "latest_close": round(latest_close, 2),
            "latest_volume": int(latest_volume),
            "average_previous_volume": round(average_previous_volume, 2),
        },
        "conditions": {
            "ce_vwap_confirmed": ce_vwap_confirmed,
            "ce_ema_trend_confirmed": ce_ema_trend_confirmed,
            "ce_opening_range_breakout": ce_opening_range_breakout,
            "ce_volume_confirmed": ce_volume_confirmed,
            "pe_vwap_confirmed": pe_vwap_confirmed,
            "pe_ema_trend_confirmed": pe_ema_trend_confirmed,
            "pe_opening_range_breakout": pe_opening_range_breakout,
            "pe_volume_confirmed": pe_volume_confirmed,
        },
    }


def trading_allowed() -> tuple[bool, str]:
    now = datetime.now(IST)
    current_time = now.time()

    if now.weekday() not in ALLOWED_WEEKDAYS:
        return False, "Paper entries are allowed only on Monday, Tuesday, and Wednesday."

    if not ENTRY_START <= current_time <= ENTRY_END:
        return False, "New paper entries are allowed only from 10:00 AM to 2:00 PM IST."

    if open_positions() >= MAX_POSITIONS:
        return False, "Maximum of two simultaneous paper trades reached."

    if open_risk() >= MAX_DAILY_LOSS:
        return False, "Combined open risk has reached INR 4,000."

    if trades_today() >= MAX_TRADES_PER_DAY:
        return False, "Maximum of three paper trades for today has been reached."

    if daily_pnl() <= -MAX_DAILY_LOSS:
        return False, "Daily loss limit of INR 4,000 reached."

    if daily_pnl() >= DAILY_PROFIT_LOCK:
        return False, "Daily profit lock of INR 6,000 reached."

    if weekly_pnl() <= -MAX_WEEKLY_LOSS:
        return False, "Weekly loss limit of INR 10,000 reached."

    return True, "Paper trade permitted."


@app.get("/health")
def health():
    return {"status": "ok", "mode": "PAPER_TRADING_ONLY"}


@app.get("/risk")
def risk():
    return {
        "daily_pnl": daily_pnl(),
        "weekly_pnl": weekly_pnl(),
        "open_positions": open_positions(),
        "open_risk": open_risk(),
        "trades_today": trades_today(),
        "limits": {
            "max_positions": MAX_POSITIONS,
            "premium_outlay_range": [
                MIN_PREMIUM_OUTLAY,
                MAX_PREMIUM_OUTLAY,
            ],
            "max_loss_per_trade": MAX_LOSS_PER_TRADE,
            "max_daily_loss": MAX_DAILY_LOSS,
            "daily_profit_lock": DAILY_PROFIT_LOCK,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "max_weekly_loss": MAX_WEEKLY_LOSS,
            "new_entry_window_ist": "10:00-14:00",
            "square_off_by_ist": "14:30",
        },
    }


@app.get("/paper/scan/{instrument}")
def scan_paper_signal(instrument: Literal["NIFTY", "BANKNIFTY"]):
    return _scan_signal(instrument)


@app.post("/paper/signal")
def create_paper_trade(signal: Signal):
    allowed, reason = trading_allowed()
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)

    confirmed = all(
        [
            signal.vwap_confirmed,
            signal.ema_trend_confirmed,
            signal.opening_range_breakout,
            signal.volume_confirmed,
        ]
    )
    if not confirmed:
        raise HTTPException(
            status_code=400,
            detail="Positive signal is not fully confirmed.",
        )

    trade = PaperTrade(
        id=str(uuid.uuid4())[:8],
        instrument=signal.instrument,
        direction=signal.direction,
        premium_outlay=signal.premium_outlay,
        opened_at=datetime.now(IST).isoformat(),
    )
    trades.append(trade)
    save_trades()

    return {
        "message": "Paper trade created. No Zerodha order was sent.",
        "trade": asdict(trade),
    }


@app.post("/paper/trades/{trade_id}/close")
def close_paper_trade(trade_id: str, payload: CloseTrade):
    for trade in trades:
        if trade.id == trade_id and trade.status == "OPEN":
            trade.status = "CLOSED"
            trade.pnl = payload.pnl
            save_trades()
            return {"message": "Paper trade closed.", "trade": asdict(trade)}

    raise HTTPException(status_code=404, detail="Open trade not found.")


@app.get("/market/indices")
def market_indices():
    if kite_client is None:
        raise HTTPException(
            status_code=401,
            detail="Please log in through /kite/login first.",
        )

    return kite_client.ohlc([
        "NSE:NIFTY 50",
        "NSE:NIFTY BANK",
    ])


@app.get("/kite/login")
def kite_login():
    api_key = os.getenv("KITE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Missing KITE_API_KEY in .env")

    global kite_client
    kite_client = KiteConnect(api_key=api_key)
    return RedirectResponse(kite_client.login_url())


@app.get("/kite/callback")
def kite_callback(request_token: str):
    global kite_client

    api_key = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")

    if not api_key or not api_secret:
        raise HTTPException(
            status_code=500,
            detail="Missing Kite credentials in .env",
        )

    kite_client = KiteConnect(api_key=api_key)
    session = kite_client.generate_session(
        request_token,
        api_secret=api_secret,
    )
    kite_client.set_access_token(session["access_token"])

    return {
        "message": "Zerodha login successful. Paper-trading data session is active.",
        "user_id": session["user_id"],
        "note": "No live orders are enabled.",
    }