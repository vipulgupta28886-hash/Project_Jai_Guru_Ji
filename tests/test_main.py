from datetime import datetime

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
        now = datetime(2026, 8, 4, 10, 30, tzinfo=main_module.IST)
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
    fixed_now = datetime(2026, 8, 4, 10, 30, tzinfo=main_module.IST)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(main_module, "datetime", FrozenDateTime)
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


def test_scan_never_selects_options(monkeypatch):
    fixed_now = datetime(2026, 8, 4, 10, 30, tzinfo=main_module.IST)

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
    main_module.kite_client = DummyKiteClientWithOptions()
    client = TestClient(main_module.app)

    response = client.get("/paper/scan/NIFTY")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["selected_contract"]["contract_type"] == "FUT"
    assert payload["selected_contract"]["trading_symbol"] == "NIFTY24SEP"
    assert payload["selected_contract"]["instrument_token"] == 33333
