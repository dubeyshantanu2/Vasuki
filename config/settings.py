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

@dataclass
class DiscordConfig:
    webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    alert_webhook_url: str = os.getenv("DISCORD_ALERT_WEBHOOK_URL", "")

@dataclass
class TradingConfig:
    # Instrument
    symbol: str = "NIFTY"
    security_id: str = "13"           # NIFTY index security ID on Dhan
    exchange_segment: str = "NSE_EQ"
    
    # Timeframes
    htf_interval: str = "60"          # 1H in minutes — for market structure
    mtf_interval: str = "15"          # 15m — for volume profile
    ltf_interval: str = "5"           # 5m — for order flow confirmation

    # Volume Profile
    bucket_size: float = 0.5          # NIFTY points per bucket
    value_area_pct: float = 0.70      # 70% value area

    # Market Structure
    swing_lookback: int = 3           # candles each side for swing detection

    # Order Flow
    big_trade_threshold: int = 500    # lots — big trade filter

    # Execution
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
