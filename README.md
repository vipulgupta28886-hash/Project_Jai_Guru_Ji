# Project Jai Guru Ji

A FastAPI-based paper trading agent for F&O signals with Kite Connect integration placeholders.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install fastapi "uvicorn[standard]" kiteconnect python-dotenv pandas`
3. Add your Kite credentials to `.env`.
4. Run the app:
   - `uvicorn main:app --reload`

## Endpoints

- `GET /health`
- `GET /risk`
- `POST /paper/signal`
- `POST /paper/trades/{trade_id}/close`
- `GET /kite/login`
- `GET /kite/callback`
