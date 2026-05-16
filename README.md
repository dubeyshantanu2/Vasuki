# Vasuki — Order Flow Trading System

Vasuki is an automated, real-time Order Flow trading system designed for the NIFTY index. It leverages the Dhan API for market data, processes high-frequency WebSocket ticks to build advanced order flow visualizations (Delta, Footprint, Volume Profile), and emits highly confirmed trading signals to Discord and Supabase.

## Architecture

Vasuki is built on a modular architecture processing market data through multiple "Gates" before emitting a signal:

### 1. Data Layer (`data/`)
*   **`dhan_ws.py`**: A robust WebSocket client connecting to Dhan's Live Market Feed. Streams Tick data (LTP, LTQ) with automatic reconnection.
*   **`dhan_rest.py`**: A REST client wrapping the DhanHQ SDK. Fetches historical OHLCV data for Market Structure and Volume Profile building.

### 2. Core Analysis (`core/`)
*   **`delta.py`**: Processes ticks into Delta Candles, calculating buying vs. selling pressure and detecting divergences.
*   **`footprint.py`**: Builds Footprint Charts at configurable price buckets. Detects bid/ask absorption and stacked imbalances.
*   **`volume_profile.py`**: Constructs Volume Profiles from OHLCV data, calculating the Point of Control (POC), Value Area High (VAH), and Value Area Low (VAL).
*   **`market_structure.py`**: Analyzes Higher Timeframe (HTF) data to determine the overarching market bias (Bullish, Bearish, or Neutral) and detect Break of Structure (BOS) / Change of Character (CHoCH).
*   **`big_trades.py`**: Filters for significant block trades to identify institutional activity.
*   **`signal_engine.py`**: The "Gatekeeper." Evaluates the market state through 4 strict gates:
    1.  **Market Structure:** Is the trend clear?
    2.  **Location:** Is price at a key Volume Profile zone (POC, VAH, VAL)?
    3.  **Zone-Bias Alignment:** Does the potential trade align with the HTF bias?
    4.  **Confirmation:** Is there Order Flow confirmation (Delta Divergence, Footprint Absorption, or Block Trades)?

### 3. Orchestration & Outputs (`main.py`, `output/`, `db/`)
*   **`main.py`**: The central `OrderFlowSystem` loop. Coordinates initialization, background refreshing (HTF structure and Volume Profiles), and non-blocking tick processing.
*   **`discord_client.py`**: Formats and sends trade signals and system health alerts to configured Discord webhooks.
*   **`supabase_client.py`**: Asynchronously persists snapshots of market structure, volume profiles, delta candles, big trades, and generated signals for review and backtesting.

### 4. Configuration (`config/`)
*   **`settings.py`**: Central dataclass-based configuration loading from environment variables (`.env`).
*   **`expiry_config.py`**: Dynamically adjusts trading parameters (e.g., stricter confirmations, tighter session windows) on NIFTY weekly expiry days (Tuesdays).

## Setup & Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository_url>
    cd Vasuki
    ```

2.  **Install dependencies:**
    Ensure you have Python 3.10+ installed.
    ```bash
    pip install -r requirements.txt
    ```

3.  **Environment Variables:**
    Copy the `.env.example` file to `.env` and fill in your credentials:
    ```bash
    cp .env.example .env
    ```
    Required keys:
    *   `DHAN_CLIENT_ID`
    *   `DHAN_ACCESS_TOKEN`
    *   `SUPABASE_URL`
    *   `SUPABASE_KEY`
    *   `DISCORD_WEBHOOK_URL` (For Trade Signals)
    *   `DISCORD_ALERT_WEBHOOK_URL` (For System Alerts & Block Trades)

4.  **Database Setup:**
    Execute the SQL definitions in `db/schema.sql` within your Supabase project's SQL Editor to set up the necessary tables.

## Running the System

Run the main orchestrator script:

```bash
python main.py
```

The system will automatically:
1. Wait for the Indian Stock Market to open (09:00 IST).
2. Fetch historical data to build the initial Market Structure and Volume Profiles.
3. Connect to the Dhan WebSocket and begin processing real-time ticks.
4. Refresh configurations periodically and emit signals to Discord when all entry gates are cleared.

## Testing

Vasuki includes a comprehensive test suite. To run the tests:

```bash
pytest tests/
```

## Disclaimer

This software is for educational and informational purposes only. It is not financial advice. Order flow trading carries significant risk. Always paper trade before deploying capital.
