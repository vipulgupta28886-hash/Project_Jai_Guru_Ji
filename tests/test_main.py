from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import main as main_module
from app import app as app_module
from main import app


client = TestClient(app)


class DummyKiteClient:
    def __init__(self):
        self.access_token = "token"
        self._instrument_token = 12345

    def instruments(self, exchange=None):
        return [
            {
                "tradingsymbol": "NIFTY24SEP",
                "name": "NIFTY",
                "expiry": "2030-09-27",
                "instrument_token": 12345,
                "exchange": "NFO",
                "segment": "NFO-FUT",
                "instrument_type": "FUT",
            },
            {
                "tradingsymbol": "NIFTY24DEC",
                "name": "NIFTY",
                "expiry": "2030-12-27",
                "instrument_token": 54321,
                "exchange": "NFO",
                "segment": "NFO-FUT",
                "instrument_type": "FUT",
            },
        ]

    def historical_data(self, instrument_token, from_date, to_date, interval):
        now = datetime(2026, 8, 6, 10, 30, tzinfo=main_module.IST)
        candles = []
        for idx in range(25):
            minute = 15 + idx
            if minute >= 45:
                minute = 30 + (minute - 45)
            candle_time = now.replace(hour=9, minute=minute, second=0, microsecond=0)
            if minute >= 45:
                candle_time = candle_time.replace(hour=10, minute=minute - 15)
            candles.append(
                {
                    "date": candle_time,
                    "open": 22000.0 + idx,
                    "high": 22100.0 + idx,
                    "low": 21900.0 + idx,
                    "close": 22050.0 + idx,
                    "volume": 100 if idx < 20 else 300,
                }
            )
        return candles


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_app_module_exports_fastapi_app():
    assert app_module is not None


def test_market_indices_requires_login():
    main_module.kite_client = None

    response = client.get("/market/indices")

    assert response.status_code == 401
    assert response.json()["detail"] == "Please log in through /kite/login first."


def test_scan_requires_login():
    main_module.kite_client = None

    response = client.get("/paper/scan/NIFTY")

    assert response.status_code == 401
    assert response.json()["detail"] == "Please log in through /kite/login first."


def test_scan_returns_signal_details(monkeypatch):
    fixed_now = datetime(2026, 8, 6, 10, 30, tzinfo=main_module.IST)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(main_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(main_module, "record_scan", lambda scan, source: None)
    main_module.kite_client = DummyKiteClient()
    client = TestClient(main_module.app)

    response = client.get("/paper/scan/NIFTY")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["signal"] == "WAIT"
    assert payload["mode"] == "PAPER_TRADING_ONLY"
    assert payload["selected_contract"]["trading_symbol"] == "NIFTY24SEP"
    assert payload["selected_contract"]["instrument_token"] == 12345
    assert payload["selected_contract"]["contract_type"] == "FUT"
    assert payload["selected_contract"]["symbol"] == "NIFTY24SEP"
    assert payload["metrics"]["futures_vwap"] is not None
    assert payload["metrics"]["futures_ema20"] is not None
    assert payload["metrics"]["futures_ema50"] is not None
    assert payload["conditions"]["ce_vwap_confirmed"] is True
    assert payload["conditions"]["ce_ema_trend_confirmed"] is True
    assert payload["conditions"]["ce_volume_confirmed"] is True
    assert payload["conditions"]["pe_vwap_confirmed"] is False
    assert payload["conditions"]["pe_ema_trend_confirmed"] is False


def test_scan_ignores_incomplete_five_minute_candle(monkeypatch):
    fixed_now = datetime(2026, 8, 6, 10, 30, tzinfo=main_module.IST)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    class DummyWithIncompleteCandle(DummyKiteClient):
        def historical_data(self, instrument_token, from_date, to_date, interval):
            candles = super().historical_data(instrument_token, from_date, to_date, interval)
            candles.append({
                "date": fixed_now,
                "open": 99999.0,
                "high": 99999.0,
                "low": 99999.0,
                "close": 99999.0,
                "volume": 999999,
            })
            return candles

    monkeypatch.setattr(main_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(main_module, "record_scan", lambda scan, source: None)
    main_module.kite_client = DummyWithIncompleteCandle()

    response = TestClient(main_module.app).get("/paper/scan/NIFTY")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["metrics"]["latest_close"] != 99999.0
    assert payload["metrics"]["latest_volume"] != 999999
    assert set(payload["shadow_signals"]) == {"1.0x", "1.2x", "1.5x"}


def test_scan_window_is_separate_from_entry_window(monkeypatch):
    fixed_now = datetime(2026, 8, 6, 14, 45, tzinfo=main_module.IST)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(main_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(main_module, "record_scan", lambda scan, source: None)
    main_module.kite_client = DummyKiteClient()
    client = TestClient(main_module.app)

    response = client.get("/paper/scan/NIFTY")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["selected_contract"]["trading_symbol"] == "NIFTY24SEP"
    assert payload["entry_window_open"] is False


def test_scan_logging_handles_date_expiry(monkeypatch):
    fixed_now = datetime(2026, 8, 6, 10, 30, tzinfo=main_module.IST)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(main_module, "datetime", FrozenDateTime)
    scan = {
        "instrument": "NIFTY",
        "signal": "CE_SIGNAL",
        "entry_window_open": True,
        "selected_contract": {"trading_symbol": "NIFTY24SEP", "expiry": date(2030, 9, 27)},
        "metrics": {},
        "conditions": {},
    }

    record = main_module._flatten_scan_for_report(scan, "MANUAL_API")
    assert record["contract_expiry"] == "2030-09-27"


def test_trial_covers_entire_next_week_and_then_stops():
    assert main_module.trial_date_allowed(datetime(2026, 8, 10, 10, 0, tzinfo=main_module.IST)) is True
    assert main_module.trial_date_allowed(datetime(2026, 8, 14, 14, 45, tzinfo=main_module.IST)) is True
    assert main_module.trial_date_allowed(datetime(2026, 8, 15, 10, 0, tzinfo=main_module.IST)) is False


def test_scan_never_selects_options(monkeypatch):
    fixed_now = datetime(2026, 8, 6, 10, 30, tzinfo=main_module.IST)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    class DummyKiteClientWithOptions(DummyKiteClient):
        def instruments(self, exchange=None):
            return [
                {
                    "tradingsymbol": "NIFTY2680424750CE",
                    "name": "NIFTY",
                    "expiry": "2030-09-27",
                    "instrument_token": 11111,
                    "exchange": "NFO",
                    "segment": "NFO-OPT",
                    "instrument_type": "CE",
                },
                {
                    "tradingsymbol": "NIFTY2680424750PE",
                    "name": "NIFTY",
                    "expiry": "2030-09-27",
                    "instrument_token": 22222,
                    "exchange": "NFO",
                    "segment": "NFO-OPT",
                    "instrument_type": "PE",
                },
                {
                    "tradingsymbol": "NIFTY24SEP",
                    "name": "NIFTY",
                    "expiry": "2030-09-27",
                    "instrument_token": 33333,
                    "exchange": "NFO",
                    "segment": "NFO-FUT",
                    "instrument_type": "FUT",
                },
            ]

    monkeypatch.setattr(main_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(main_module, "record_scan", lambda scan, source: None)
    main_module.kite_client = DummyKiteClientWithOptions()
    client = TestClient(main_module.app)

    response = client.get("/paper/scan/NIFTY")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["selected_contract"]["contract_type"] == "FUT"
    assert payload["selected_contract"]["trading_symbol"] == "NIFTY24SEP"
    assert payload["selected_contract"]["instrument_token"] == 33333


def test_background_loop_monitors_positions_every_two_seconds(monkeypatch):
    calls = []

    class StopLoop(Exception):
        pass

    class FakeEvent:
        def wait(self, seconds):
            calls.append(("wait", seconds))
            raise StopLoop

    monkeypatch.setattr(main_module, "trial_date_allowed", lambda now: False)
    monkeypatch.setattr(main_module, "_monitor_open_paper_trades", lambda: calls.append(("monitor", None)))
    monkeypatch.setattr(main_module.threading, "Event", lambda: FakeEvent())

    with pytest.raises(StopLoop):
        main_module._run_automatic_scans()

    assert calls == [("monitor", None), ("wait", 2)]


def test_profit_trail_arms_above_two_thousand_and_exits_on_pullback(monkeypatch):
    class QuoteClient:
        access_token = "token"

        def __init__(self):
            self.prices = iter((125.0, 119.0))

        def ltp(self, keys):
            return {keys[0]: {"last_price": next(self.prices)}}

    trade = main_module.PaperTrade(
        id="trail",
        instrument="NIFTY",
        direction="CE",
        premium_outlay=10000,
        opened_at="2026-08-12T10:00:00+05:30",
        option_symbol="TESTCE",
        quantity=100,
        entry_price=100.0,
        current_price=100.0,
    )
    monkeypatch.setattr(main_module, "trades", [trade])
    monkeypatch.setattr(main_module, "kite_client", QuoteClient())
    monkeypatch.setattr(main_module, "save_trades", lambda: None)
    monkeypatch.setattr(main_module, "_write_trade_event", lambda event, paper_trade: None)
    monkeypatch.setattr(main_module, "SQUARE_OFF_TIME", main_module.time(23, 59))

    main_module._monitor_open_paper_trades()
    assert trade.status == "OPEN"
    assert trade.pnl == 2500
    main_module._monitor_open_paper_trades()
    assert trade.status == "CLOSED"
    assert trade.pnl == 1900
    assert trade.exit_reason == "PROFIT_LADDER_2000_FLOOR"


def test_early_profit_trail_exits_after_one_thousand_peak(monkeypatch):
    class QuoteClient:
        access_token = "token"

        def __init__(self):
            self.prices = iter((111.0, 110.0))

        def ltp(self, keys):
            return {keys[0]: {"last_price": next(self.prices)}}

    trade = main_module.PaperTrade(
        id="early-trail",
        instrument="NIFTY",
        direction="CE",
        premium_outlay=10000,
        opened_at="2026-08-12T10:00:00+05:30",
        option_symbol="TESTCE",
        quantity=100,
        entry_price=100.0,
        current_price=100.0,
    )
    monkeypatch.setattr(main_module, "trades", [trade])
    monkeypatch.setattr(main_module, "kite_client", QuoteClient())
    monkeypatch.setattr(main_module, "save_trades", lambda: None)
    monkeypatch.setattr(main_module, "_write_trade_event", lambda event, paper_trade: None)
    monkeypatch.setattr(main_module, "SQUARE_OFF_TIME", main_module.time(23, 59))

    main_module._monitor_open_paper_trades()
    assert trade.status == "OPEN"
    assert trade.peak_pnl == 1100

    main_module._monitor_open_paper_trades()
    assert trade.status == "CLOSED"
    assert trade.pnl == 1000
    assert trade.exit_reason == "PROFIT_LADDER_1000_FLOOR"


@pytest.mark.parametrize(
    ("peak_price", "floor_price", "expected_reason"),
    [
        (106.0, 105.0, "PROFIT_LADDER_500_FLOOR"),
        (111.0, 110.0, "PROFIT_LADDER_1000_FLOOR"),
        (116.0, 115.0, "PROFIT_LADDER_1500_FLOOR"),
        (121.0, 120.0, "PROFIT_LADDER_2000_FLOOR"),
        (126.0, 125.0, "PROFIT_LADDER_2500_FLOOR"),
    ],
)
def test_each_profit_ladder_step(monkeypatch, peak_price, floor_price, expected_reason):
    class QuoteClient:
        access_token = "token"

        def __init__(self):
            self.prices = iter((peak_price, floor_price))

        def ltp(self, keys):
            return {keys[0]: {"last_price": next(self.prices)}}

    trade = main_module.PaperTrade(
        id="ladder",
        instrument="NIFTY",
        direction="CE",
        premium_outlay=10000,
        opened_at="2026-08-13T10:00:00+05:30",
        option_symbol="TESTCE",
        quantity=100,
        entry_price=100.0,
        current_price=100.0,
    )
    monkeypatch.setattr(main_module, "trades", [trade])
    monkeypatch.setattr(main_module, "kite_client", QuoteClient())
    monkeypatch.setattr(main_module, "save_trades", lambda: None)
    monkeypatch.setattr(main_module, "_write_trade_event", lambda event, paper_trade: None)
    monkeypatch.setattr(main_module, "SQUARE_OFF_TIME", main_module.time(23, 59))

    main_module._monitor_open_paper_trades()
    assert trade.status == "OPEN"
    main_module._monitor_open_paper_trades()
    assert trade.status == "CLOSED"
    assert trade.exit_reason == expected_reason


def test_three_simultaneous_positions_and_reopen_after_one_closes(monkeypatch):
    fixed_now = datetime(2026, 8, 13, 12, 0, tzinfo=main_module.IST)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    selected_option = {
        "option_symbol": "NIFTYTESTCE",
        "option_token": 123,
        "quantity": 100,
        "entry_price": 320.0,
        "premium_outlay": 32000.0,
    }
    scan = {
        "instrument": "NIFTY",
        "signal": "CE_SIGNAL",
        "entry_window_open": True,
        "metrics": {"latest_close": 24500.0},
    }
    monkeypatch.setattr(main_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(main_module, "trades", [])
    monkeypatch.setattr(main_module, "_select_paper_option", lambda payload: selected_option)
    monkeypatch.setattr(main_module, "save_trades", lambda: None)
    monkeypatch.setattr(main_module, "_write_trade_event", lambda event, trade: None)

    assert main_module._automatic_paper_entry(scan)["status"] == "PAPER_TRADE_OPENED"
    assert main_module._automatic_paper_entry(scan)["status"] == "PAPER_TRADE_OPENED"
    assert main_module._automatic_paper_entry(scan)["status"] == "PAPER_TRADE_OPENED"
    assert main_module.open_positions() == 3

    blocked = main_module._automatic_paper_entry(scan)
    assert blocked["status"] == "NO_ENTRY"
    assert "Maximum of three" in blocked["reason"]

    main_module.trades[0].status = "CLOSED"
    assert main_module._automatic_paper_entry(scan)["status"] == "PAPER_TRADE_OPENED"
    assert main_module.open_positions() == 3
