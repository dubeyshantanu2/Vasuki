-- db/schema.sql
-- Supabase Schema for Vasuki Order Flow Analysis

-- 1. market_structure_snapshots
CREATE TABLE IF NOT EXISTS market_structure_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    captured_at timestamptz NOT NULL,
    symbol text NOT NULL,
    bias text NOT NULL,           -- 'bullish', 'bearish', 'neutral'
    last_event text,              -- 'bos_bullish', 'choch_bearish', etc
    is_clear boolean NOT NULL,
    last_swing_high float8,
    last_swing_low float8,
    adaptive_lookback_used int,   -- NEW COLUMN
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_market_structure_symbol_time ON market_structure_snapshots (symbol, captured_at);


-- 2. volume_profile_snapshots
CREATE TABLE IF NOT EXISTS volume_profile_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    captured_at timestamptz NOT NULL,
    symbol text NOT NULL,
    session_type text NOT NULL,    -- 'intraday' or 'prior_day'
    poc float8 NOT NULL,
    vah float8 NOT NULL,
    val float8 NOT NULL,
    total_volume float8 NOT NULL,
    poc_concentration_pct float8,  -- NEW COLUMN
    created_at timestamptz DEFAULT now()
);


-- 3. delta_candles
CREATE TABLE IF NOT EXISTS delta_candles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol text NOT NULL,
    interval_start timestamptz NOT NULL,
    interval_minutes int NOT NULL,
    buy_volume float8,
    sell_volume float8,
    delta float8,
    cumulative_delta float8,
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_delta_candles_symbol_time ON delta_candles (symbol, interval_start);


-- 4. big_trades
CREATE TABLE IF NOT EXISTS big_trades (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol text NOT NULL,
    traded_at timestamptz NOT NULL,
    price float8 NOT NULL,
    quantity_lots int NOT NULL,
    direction text NOT NULL,      -- 'buy' or 'sell'
    significance text NOT NULL,   -- 'large' or 'block'
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_big_trades_symbol_time ON big_trades (symbol, traded_at);


-- 5. signals
CREATE TABLE IF NOT EXISTS signals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol text NOT NULL,
    triggered_at timestamptz NOT NULL,
    direction text NOT NULL,      -- 'long' or 'short'
    bias text NOT NULL,
    zone_type text NOT NULL,      -- 'POC', 'VAH', 'VAL'
    zone_price float8 NOT NULL,
    entry_price float8,
    sl_price float8,
    t1_price float8,
    t2_price float8,
    t3_price float8,
    confirmations jsonb,          -- {"delta": true, "footprint": true, "big_trade": false}
    confluence jsonb,             -- {"strength": "single", "all_zones": [], "sources": []}
    is_expiry_day boolean DEFAULT false,
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals (symbol, triggered_at);

-- 6. spike_events
CREATE TABLE IF NOT EXISTS spike_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol text NOT NULL,
    detected_at timestamptz NOT NULL,
    candle_range float8 NOT NULL,
    avg_range float8 NOT NULL,
    suppression_end timestamptz,
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_spike_events_symbol_time ON spike_events (symbol, detected_at);

-- Migrations (Run these if updating an existing database)
ALTER TABLE volume_profile_snapshots ADD COLUMN IF NOT EXISTS poc_concentration_pct float8;
ALTER TABLE market_structure_snapshots ADD COLUMN IF NOT EXISTS adaptive_lookback_used int;
