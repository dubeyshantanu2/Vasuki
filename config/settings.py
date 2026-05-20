from dataclasses import dataclass, field
from typing import Optional
import os
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

@dataclass
class DhanConfig:
    client_id: str = os.getenv("DHAN_CLIENT_ID", "")
    access_token: str = os.getenv("DHAN_ACCESS_TOKEN", "")
    base_url: str = "https://api.dhan.co"

@dataclass
class SupabaseConfig:
    url: str = os.getenv("SUPABASE_URL", "")
    key: str = os.getenv("SUPABASE_KEY", "")
    fallback_dir: str = os.getenv("SUPABASE_FALLBACK_DIR", "/var/log/order-flow-nifty/fallback/")

@dataclass
class DiscordConfig:
    webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    alert_webhook_url: str = os.getenv("DISCORD_ALERT_WEBHOOK_URL", "")

@dataclass
class TradingConfig:
    # Instrument
    symbol: str = "NIFTY"
    security_id: str = "13"           # NIFTY index security ID on Dhan
    exchange_segment: str = "IDX_I"
    
    # Timeframes
    htf_interval: str = "60"          # 1H in minutes — for market structure
    mtf_interval: str = "15"          # 15m — for volume profile
    ltf_interval: str = "5"           # 5m — for order flow confirmation

    # Volume Profile
    bucket_size: float = 0.5          # NIFTY points per bucket
    value_area_pct: float = 0.70      # 70% value area
    flat_profile_threshold_pct: float = 0.15 # top 3 buckets < 15% = flat
    poc_migration_threshold_points: int = 30 # points before POC migration invalidates signal
    confluence_threshold_pts: float = 10.0 # NIFTY points

    # Market Structure
    swing_lookback: int = 3           # candles each side for swing detection
    gap_threshold_pct: float = 0.005  # 0.5% threshold for gap detection
    expiry_gap_tier2_pct: float = 0.010 # 1.0%
    expiry_gap_tier3_pct: float = 0.015 # 1.5%
    atr_multiplier_medium: float = 1.5   # lookback → 5
    atr_multiplier_high: float = 2.0     # lookback → 7
    atr_lookback_candles: int = 20

    # Order Flow
    big_trade_threshold: int = 500    # lots — big trade filter
    outlier_cap_multiplier: float = 3.0 # multiplier for delta tick capping

    # System Scheduling
    vp_refresh_offset_mins: int = 2
    structure_refresh_offset_mins: int = 5
    no_data_alert_minutes: int = 10
    heartbeat_tick_minimum: int = 50
    circuit_breaker_silence_threshold_seconds: int = 120

    # Execution
    spike_threshold_multiplier: float = 3.0
    spike_suppression_candles: int = 3
    sl_buffer_points: int = 15        # NIFTY points beyond structural level
    t1_booking_pct: float = 0.50
    t2_booking_pct: float = 0.30
    t3_booking_pct: float = 0.20

    # Session windows (IST, 24hr)
    session_start: str = "09:15"
    no_trade_until: str = "09:45"
    prime_end: str = "11:30"
    secondary_end: str = "13:00"
    lunch_end: str = "14:00"
    final_end: str = "15:15"
    session_close: str = "15:30"
    valid_window_end: str = "15:15"   # dynamic based on expiry
    afternoon_block: bool = False     # dynamic based on expiry

    # Confirmation
    min_confirmations: int = 2        # of 3 required on normal days

@dataclass
class AppConfig:
    dhan: DhanConfig = field(default_factory=DhanConfig)
    supabase: SupabaseConfig = field(default_factory=SupabaseConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)

CONFIG = AppConfig()
