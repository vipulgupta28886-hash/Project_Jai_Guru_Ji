from dataclasses import asdict, dataclass
from datetime import date, datetime, datetime as real_datetime, time, timedelta
from pathlib import Path
import csv
import json
import os
import threading
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
SCAN_LOG_FILE = Path("data/paper_scans.jsonl")
REPORTS_DIRECTORY = Path("data/reports")
DATA_FILE.parent.mkdir(exist_ok=True)
REPORTS_DIRECTORY.mkdir(parents=True, exist_ok=True)

MAX_POSITIONS = 3
MIN_PREMIUM_OUTLAY = 30000
MAX_PREMIUM_OUTLAY = 35000
MAX_LOSS_PER_TRADE = 2000
PROFIT_LADDER = (
    (2500, 2500),
    (2000, 2000),
    (1500, 1500),
    (1000, 1000),
    (500, 500),
)
MAX_PROFIT_PER_TRADE = 3000
MAX_DAILY_LOSS = 4000
DAILY_PROFIT_LOCK = 6000
MAX_TRADES_PER_DAY = 10
MAX_WEEKLY_LOSS = 20000

SCAN_START = time(9, 30)
SCAN_END = time(14, 55)
ENTRY_START = time(9, 30)
ENTRY_END = time(14, 30)
SQUARE_OFF_TIME = time(14, 55)
ALLOWED_WEEKDAYS = (0, 1, 2, 3, 4)  # Monday through Friday
TRIAL_START_DATE = date(2026, 8, 6)
TRIAL_END_DATE = date(2026, 8, 21)
ACTIVE_VOLUME_MULTIPLIER = 1.0
SHADOW_VOLUME_MULTIPLIERS = (1.0, 1.2, 1.5)
POSITION_MONITOR_INTERVAL_SECONDS = 2
SIGNAL_SCAN_INTERVAL_MINUTES = 3

app = FastAPI(title="Jai Guru Ji F&O Paper Agent")
scan_log_lock = threading.Lock()
scanner_started = False


@dataclass
class PaperTrade:
    id: str
    instrument: str
    direction: str
    premium_outlay: float
    opened_at: str
    stop_loss: float = -MAX_LOSS_PER_TRADE
    target: float | None = MAX_PROFIT_PER_TRADE
    status: str = "OPEN"
    pnl: float = 0.0
    option_symbol: str | None = None
    option_token: int | None = None
    quantity: int = 0
    entry_price: float | None = None
    current_price: float | None = None
    exit_price: float | None = None
    entry_underlying: float | None = None
    closed_at: str | None = None
    exit_reason: str | None = None
    profit_lock_armed: bool = False
    peak_pnl: float = 0.0
    worst_pnl: float = 0.0
    loss_recovery_750_armed: bool = False
    loss_recovery_1350_armed: bool = False


def load_trades() -> list[PaperTrade]:
    if not DATA_FILE.exists():
        return []

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        for trade in data:
            # Migrate paper positions created under the former INR 6,000
            # per-trade target. INR 6,000 is the aggregate daily cap only.
            if trade.get("target") is None or trade.get("target", 0) > MAX_PROFIT_PER_TRADE:
                trade["target"] = MAX_PROFIT_PER_TRADE
            trade.setdefault("profit_lock_armed", False)
            trade.setdefault("peak_pnl", max(0.0, float(trade.get("pnl", 0) or 0)))
            trade.setdefault("worst_pnl", min(0.0, float(trade.get("pnl", 0) or 0)))
            trade.setdefault("loss_recovery_750_armed", False)
            trade.setdefault("loss_recovery_1350_armed", False)
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




def trial_date_allowed(now: datetime) -> bool:
    return (
        TRIAL_START_DATE <= now.date() <= TRIAL_END_DATE
        and now.weekday() in ALLOWED_WEEKDAYS
    )


def _scan_report_path(scan_date: str) -> Path:
    return REPORTS_DIRECTORY / f"paper_scan_experiment_{scan_date}.csv"


def _flatten_scan_for_report(scan: dict, source: str) -> dict:
    """Create one spreadsheet-friendly record. This function never creates trades."""
    metrics = scan.get("metrics") or {}
    conditions = scan.get("conditions") or {}
    contract = scan.get("selected_contract") or {}
    timestamp = datetime.now(IST).isoformat()
    expiry = contract.get("expiry")
    if isinstance(expiry, (datetime, real_datetime)):
        expiry = expiry.date().isoformat()
    elif hasattr(expiry, "isoformat"):
        expiry = expiry.isoformat()
    return {
        "scan_timestamp_ist": timestamp,
        "scan_date_ist": timestamp[:10],
        "source": source,
        "instrument": scan.get("instrument"),
        "signal": scan.get("signal", "WAIT"),
        "entry_window_open": scan.get("entry_window_open", False),
        "message": scan.get("message"),
        "contract": contract.get("trading_symbol"),
        "contract_expiry": expiry,
        "latest_close": metrics.get("latest_close"),
        "futures_vwap": metrics.get("futures_vwap"),
        "futures_ema20": metrics.get("futures_ema20"),
        "futures_ema50": metrics.get("futures_ema50"),
        "opening_range_high": metrics.get("opening_range_high"),
        "opening_range_low": metrics.get("opening_range_low"),
        "latest_volume": metrics.get("latest_volume"),
        "average_previous_volume": metrics.get("average_previous_volume"),
        "volume_ratio": metrics.get("volume_ratio"),
        "completed_candle_at": metrics.get("completed_candle_at"),
        "shadow_signal_1_0x": (scan.get("shadow_signals") or {}).get("1.0x"),
        "shadow_signal_1_2x": (scan.get("shadow_signals") or {}).get("1.2x"),
        "shadow_signal_1_5x": (scan.get("shadow_signals") or {}).get("1.5x"),
        "ce_vwap_confirmed": conditions.get("ce_vwap_confirmed"),
        "ce_ema_trend_confirmed": conditions.get("ce_ema_trend_confirmed"),
        "ce_opening_range_breakout": conditions.get("ce_opening_range_breakout"),
        "ce_volume_confirmed": conditions.get("ce_volume_confirmed"),
        "pe_vwap_confirmed": conditions.get("pe_vwap_confirmed"),
        "pe_ema_trend_confirmed": conditions.get("pe_ema_trend_confirmed"),
        "pe_opening_range_breakout": conditions.get("pe_opening_range_breakout"),
        "pe_volume_confirmed": conditions.get("pe_volume_confirmed"),
    }


def record_scan(scan: dict, source: str) -> None:
    """Persist scan outcomes for later review; it deliberately has no order logic."""
    record = _flatten_scan_for_report(scan, source)
    fieldnames = list(record)
    report_path = _scan_report_path(record["scan_date_ist"])
    with scan_log_lock:
        with SCAN_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, default=str) + "\n")
        needs_header = not report_path.exists() or report_path.stat().st_size == 0
        with report_path.open("a", newline="", encoding="utf-8") as report_file:
            writer = csv.DictWriter(report_file, fieldnames=fieldnames)
            if needs_header:
                writer.writeheader()
            writer.writerow(record)


def _run_automatic_scans() -> None:
    """Monitor positions every two seconds and scan signals every three minutes."""
    last_slot: tuple[str, int, int] | None = None
    while True:
        now = datetime.now(IST)
        slot = (now.date().isoformat(), now.hour, now.minute // SIGNAL_SCAN_INTERVAL_MINUTES)
        scan_window_open = trial_date_allowed(now) and SCAN_START <= now.time() <= SCAN_END
        try:
            _monitor_open_paper_trades()
        except Exception:
            # A temporary quote failure must not kill the monitoring thread.
            pass
        if scan_window_open and slot != last_slot:
            last_slot = slot
            for instrument in ("NIFTY", "BANKNIFTY"):
                try:
                    scan = _scan_signal(instrument)
                    scan["paper_action"] = _automatic_paper_entry(scan)
                    record_scan(scan, source="AUTOMATIC_3_MINUTE")
                except HTTPException as exc:
                    record_scan({"instrument": instrument, "signal": "WAIT", "entry_window_open": False, "message": f"Automatic scan unavailable: {exc.detail}"}, source="AUTOMATIC_3_MINUTE")
                except Exception as exc:  # pragma: no cover - defensive background path
                    record_scan({"instrument": instrument, "signal": "WAIT", "entry_window_open": False, "message": f"Automatic scan failed: {exc}"}, source="AUTOMATIC_3_MINUTE")
        elif not scan_window_open:
            last_slot = None
        threading.Event().wait(POSITION_MONITOR_INTERVAL_SECONDS)


@app.on_event("startup")
def start_automatic_scan_logger() -> None:
    global scanner_started
    if not scanner_started:
        scanner_started = True
        threading.Thread(target=_run_automatic_scans, daemon=True, name="paper-scan-logger").start()
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
    if not trial_date_allowed(now):
        return {
            "instrument": instrument,
            "signal": "WAIT",
            "mode": "PAPER_TRADING_ONLY",
            "entry_window_open": False,
            "message": "No trade was created. Outside the 06-21 August weekday paper trial.",
            "selected_contract": None,
            "metrics": {},
            "conditions": {},
        }

    if not SCAN_START <= now.time() <= SCAN_END:
        return {
            "instrument": instrument,
            "signal": "WAIT",
            "mode": "PAPER_TRADING_ONLY",
            "entry_window_open": False,
            "message": "No trade was created. Outside the 09:30–14:55 IST scan window.",
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

    usable_candles = [
        c
        for c in candles
        if isinstance(c.get("date"), (datetime, real_datetime))
    ]
    if not usable_candles:
        raise HTTPException(status_code=502, detail="Kite Connect returned candles without usable timestamps.")

    completed_cutoff = now - timedelta(minutes=5)
    recent_candles = [c for c in usable_candles if c["date"] <= completed_cutoff][-60:]
    if len(recent_candles) < 21:
        raise HTTPException(status_code=502, detail="Insufficient completed futures candle history for the scanner.")

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
    volume_ratio = latest_volume / average_previous_volume if average_previous_volume > 0 else 0
    volume_confirmed = bool(volume_ratio >= ACTIVE_VOLUME_MULTIPLIER)

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

    shadow_signals = {}
    for multiplier in SHADOW_VOLUME_MULTIPLIERS:
        shadow_volume_confirmed = volume_ratio >= multiplier
        shadow_signal = "WAIT"
        if all([ce_vwap_confirmed, ce_ema_trend_confirmed, ce_opening_range_breakout, shadow_volume_confirmed]):
            shadow_signal = "CE_SIGNAL"
        elif all([pe_vwap_confirmed, pe_ema_trend_confirmed, pe_opening_range_breakout, shadow_volume_confirmed]):
            shadow_signal = "PE_SIGNAL"
        shadow_signals[f"{multiplier:.1f}x"] = shadow_signal

    entry_window_open = ENTRY_START <= now.time() <= ENTRY_END

    return {
        "instrument": instrument,
        "signal": signal,
        "mode": "PAPER_TRADING_ONLY",
        "entry_window_open": entry_window_open,
        "message": "No trade was created. Use /paper/signal to create a paper trade.",
        "selected_contract": contract,
        "shadow_signals": shadow_signals,
        "metrics": {
            "futures_vwap": round(vwap, 2) if vwap is not None else None,
            "futures_ema20": round(ema_20, 2) if ema_20 is not None else None,
            "futures_ema50": round(ema_50, 2) if ema_50 is not None else None,
            "opening_range_high": round(opening_range_high, 2) if opening_range_high is not None else None,
            "opening_range_low": round(opening_range_low, 2) if opening_range_low is not None else None,
            "latest_close": round(latest_close, 2),
            "latest_volume": int(latest_volume),
            "average_previous_volume": round(average_previous_volume, 2),
            "volume_ratio": round(volume_ratio, 3),
            "completed_candle_at": latest["date"].isoformat(),
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



def _parse_expiry(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _select_paper_option(scan: dict) -> dict:
    """Select a quoted NFO option for simulation. This function never sends an order."""
    if kite_client is None or not getattr(kite_client, "access_token", None):
        raise HTTPException(status_code=401, detail="Please log in through /kite/login first.")

    option_type = "CE" if scan["signal"] == "CE_SIGNAL" else "PE"
    underlying = float((scan.get("metrics") or {}).get("latest_close") or 0)
    today = datetime.now(IST).date()
    candidates = []
    for item in kite_client.instruments(exchange="NFO") or []:
        expiry = _parse_expiry(item.get("expiry"))
        if (
            str(item.get("exchange", "")).upper() == "NFO"
            and str(item.get("segment", "")).upper() == "NFO-OPT"
            and str(item.get("instrument_type", "")).upper() == option_type
            and str(item.get("name", "")).upper() == scan["instrument"]
            and expiry is not None
            and expiry >= today
        ):
            candidates.append((expiry, item))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"No {scan['instrument']} {option_type} contract is available.")

    nearest_expiry = min(expiry for expiry, _ in candidates)
    nearest = [item for expiry, item in candidates if expiry == nearest_expiry]
    nearest.sort(key=lambda item: abs(float(item.get("strike", 0) or 0) - underlying))
    nearest = nearest[:24]
    keys = [f"NFO:{item['tradingsymbol']}" for item in nearest]
    quotes = kite_client.ltp(keys) or {}
    choices = []
    midpoint = (MIN_PREMIUM_OUTLAY + MAX_PREMIUM_OUTLAY) / 2
    for item in nearest:
        key = f"NFO:{item['tradingsymbol']}"
        price = float((quotes.get(key) or {}).get("last_price") or 0)
        lot_size = int(item.get("lot_size") or 0)
        if price <= 0 or lot_size <= 0:
            continue
        max_lots = int(MAX_PREMIUM_OUTLAY // (price * lot_size))
        for lots in range(1, max_lots + 1):
            quantity = lot_size * lots
            outlay = price * quantity
            if MIN_PREMIUM_OUTLAY <= outlay <= MAX_PREMIUM_OUTLAY:
                score = (abs(float(item.get("strike", 0) or 0) - underlying), abs(outlay - midpoint))
                choices.append((score, item, price, quantity, outlay))
    if not choices:
        raise HTTPException(status_code=422, detail="No quoted option fits the INR 30,000-35,000 paper outlay range.")

    _, item, price, quantity, outlay = min(choices, key=lambda choice: choice[0])
    return {
        "option_symbol": str(item["tradingsymbol"]),
        "option_token": int(item["instrument_token"]),
        "quantity": quantity,
        "entry_price": round(price, 2),
        "premium_outlay": round(outlay, 2),
    }


def _write_trade_event(event: str, trade: PaperTrade) -> None:
    path = REPORTS_DIRECTORY / f"paper_trade_journal_{trade.opened_at[:10]}.csv"
    record = {"event_timestamp_ist": datetime.now(IST).isoformat(), "event": event, **asdict(trade)}
    with scan_log_lock:
        needs_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=list(record))
            if needs_header:
                writer.writeheader()
            writer.writerow(record)


def _automatic_paper_entry(scan: dict) -> dict:
    if scan.get("signal") not in ("CE_SIGNAL", "PE_SIGNAL") or not scan.get("entry_window_open"):
        return {"status": "NO_ENTRY", "reason": "No qualifying entry signal."}
    allowed, reason = trading_allowed()
    if not allowed:
        return {"status": "NO_ENTRY", "reason": reason}

    option = _select_paper_option(scan)
    now = datetime.now(IST).isoformat()
    trade = PaperTrade(
        id=str(uuid.uuid4())[:8],
        instrument=scan["instrument"],
        direction="CE" if scan["signal"] == "CE_SIGNAL" else "PE",
        premium_outlay=option["premium_outlay"],
        opened_at=now,
        option_symbol=option["option_symbol"],
        option_token=option["option_token"],
        quantity=option["quantity"],
        entry_price=option["entry_price"],
        current_price=option["entry_price"],
        entry_underlying=float((scan.get("metrics") or {}).get("latest_close") or 0),
    )
    trades.append(trade)
    save_trades()
    _write_trade_event("OPEN", trade)
    return {"status": "PAPER_TRADE_OPENED", "trade_id": trade.id, "option_symbol": trade.option_symbol}


def _monitor_open_paper_trades() -> None:
    """Mark simulated positions to market and close them. No order API is called."""
    open_trades = [trade for trade in trades if trade.status == "OPEN" and trade.option_symbol]
    if not open_trades or kite_client is None or not getattr(kite_client, "access_token", None):
        return
    keys = [f"NFO:{trade.option_symbol}" for trade in open_trades]
    quotes = kite_client.ltp(keys) or {}
    now = datetime.now(IST)
    changed = False
    quoted_trades = []
    for trade in open_trades:
        price = float((quotes.get(f"NFO:{trade.option_symbol}") or {}).get("last_price") or 0)
        if price <= 0 or trade.entry_price is None or trade.quantity <= 0:
            continue
        trade.current_price = round(price, 2)
        trade.pnl = round((price - trade.entry_price) * trade.quantity, 2)
        trade.peak_pnl = max(trade.peak_pnl, trade.pnl)
        trade.worst_pnl = min(trade.worst_pnl, trade.pnl)

        # Arm a tighter exit after a moderate loss recovers meaningfully.
        if trade.worst_pnl <= -1000 and trade.pnl >= -700:
            trade.loss_recovery_750_armed = True

        # Arm a deeper recovery exit after a severe loss recovers.
        if trade.worst_pnl <= -1500 and trade.pnl >= -1300:
            trade.loss_recovery_1350_armed = True

        quoted_trades.append(trade)
        changed = True

    daily_cap_reached = daily_pnl() >= DAILY_PROFIT_LOCK
    daily_loss_reached = daily_pnl() <= -MAX_DAILY_LOSS
    for trade in quoted_trades:
        reason = None
        if trade.pnl <= trade.stop_loss:
            reason = "STOP_LOSS"
        elif daily_cap_reached:
            reason = "DAILY_PROFIT_LOCK"
        elif daily_loss_reached:
            reason = "DAILY_LOSS_LIMIT"
        elif trade.pnl >= MAX_PROFIT_PER_TRADE:
            reason = "MAX_PROFIT_TARGET"
        elif trade.loss_recovery_750_armed and trade.pnl <= -750:
            reason = "LOSS_RECOVERY_750"
        elif trade.loss_recovery_1350_armed and trade.pnl <= -1350:
            reason = "LOSS_RECOVERY_1350"
        else:
            for trigger, floor in PROFIT_LADDER:
                if trade.peak_pnl > trigger and trade.pnl <= floor:
                    reason = f"PROFIT_LADDER_{trigger}_FLOOR"
                    break
        if reason is None and now.time() >= SQUARE_OFF_TIME:
            reason = "TIME_SQUARE_OFF"
        if reason:
            trade.status = "CLOSED"
            trade.exit_price = trade.current_price
            trade.closed_at = now.isoformat()
            trade.exit_reason = reason
            _write_trade_event("CLOSE", trade)
    if changed:
        save_trades()

def trading_allowed() -> tuple[bool, str]:
    now = datetime.now(IST)
    current_time = now.time()

    if not trial_date_allowed(now):
        return False, "Paper entries are allowed only on weekdays from 06 through 21 August 2026."

    if current_time >= SQUARE_OFF_TIME:
        return False, "Mandatory square-off is due by 2:55 PM IST."

    if not ENTRY_START <= current_time <= ENTRY_END:
        return False, "New paper entries are allowed only from 09:30 AM to 02:30 PM IST."

    if open_positions() >= MAX_POSITIONS:
        return False, "Maximum of three simultaneous paper trades reached. A new trade can open after one closes."

    if trades_today() >= MAX_TRADES_PER_DAY:
        return False, "Maximum of ten paper trades for today has been reached."

    if daily_pnl() <= -MAX_DAILY_LOSS:
        return False, "Daily loss limit of INR 4,000 reached."

    if daily_pnl() >= DAILY_PROFIT_LOCK:
        return False, "Daily profit lock of INR 6,000 reached."

    if weekly_pnl() <= -MAX_WEEKLY_LOSS:
        return False, f"Weekly loss limit of INR {MAX_WEEKLY_LOSS:,} reached."

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
            "profit_ladder": [
                {"peak_must_exceed": trigger, "protected_floor": floor}
                for trigger, floor in reversed(PROFIT_LADDER)
            ],
            "max_profit_per_trade": MAX_PROFIT_PER_TRADE,
            "max_daily_loss": MAX_DAILY_LOSS,
            "daily_profit_lock": DAILY_PROFIT_LOCK,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "max_weekly_loss": MAX_WEEKLY_LOSS,
            "active_volume_multiplier": ACTIVE_VOLUME_MULTIPLIER,
            "position_monitor_interval_seconds": POSITION_MONITOR_INTERVAL_SECONDS,
            "signal_scan_interval_minutes": SIGNAL_SCAN_INTERVAL_MINUTES,
            "shadow_volume_multipliers": list(SHADOW_VOLUME_MULTIPLIERS),
            "trial_dates": "2026-08-06 to 2026-08-21",
            "scan_window_ist": "09:30-14:55",
            "new_entry_window_ist": "09:30-14:30",
            "square_off_by_ist": "14:55",
        },
    }


@app.get("/paper/trades")
def list_paper_trades():
    _monitor_open_paper_trades()
    return {"mode": "PAPER_TRADING_ONLY", "trades": [asdict(trade) for trade in trades]}

@app.get("/paper/scan/{instrument}")
def scan_paper_signal(instrument: Literal["NIFTY", "BANKNIFTY"]):
    scan = _scan_signal(instrument)
    record_scan(scan, source="MANUAL_API")
    return scan


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
            trade.closed_at = datetime.now(IST).isoformat()
            trade.exit_reason = "MANUAL"
            save_trades()
            _write_trade_event("CLOSE", trade)
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
