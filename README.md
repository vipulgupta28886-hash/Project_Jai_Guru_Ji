# Project Jai Guru Ji

A FastAPI-based paper trading agent for F&O signals with Kite Connect integration placeholders.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and add your Kite credentials.
4. Run the app from the project root:
   - `.venv\Scripts\python.exe -m uvicorn main:app --reload`

## Endpoints

- `GET /health`
- `GET /risk`
- `POST /paper/signal`
- `POST /paper/trades/{trade_id}/close`
- `GET /kite/login`
- `GET /kite/callback`

## Current Paper Trial

- Trial dates: 6 August 2026 through 14 August 2026 inclusive.
- Trading days: Monday through Friday.
- Automatic scan and CSV logging: 09:30 AM to 02:45 PM IST, every five minutes.
- New paper-entry eligibility: 09:30 AM to 02:30 PM IST.
- Mandatory square-off: 02:55 PM IST.
- Active volume threshold: 1.0x average of the previous 20 completed five-minute candles.
- Shadow comparisons: 1.0x, 1.2x, and 1.5x.
- Mode: paper trading only. No live Zerodha order endpoint is enabled.
- Keep the application and laptop running, and complete Kite login each trading day.
