import dataclasses
import datetime
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo
from loguru import logger

from config.settings import TradingConfig

# Hardcoded NSE holiday list for current year (2026).
EXCHANGE_HOLIDAYS = {
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 3),   # Maha Shivaratri
    date(2026, 3, 23),  # Holi
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),   # Maharashtra Day
    date(2026, 8, 15),  # Independence Day
    date(2026, 9, 15),  # Ganesh Chaturthi
    date(2026, 10, 2),  # Mahatma Gandhi Jayanti
    date(2026, 10, 19), # Dussehra
    date(2026, 11, 8),  # Diwali
    date(2026, 11, 23), # Gurunanak Jayanti
    date(2026, 12, 25), # Christmas
}

@dataclass
class ExpiryConfig:
    is_expiry_day: bool
    
    # Overrides (only meaningful if is_expiry_day=True)
    min_confirmations: int = 3          # all 3 required (vs 2 normal)
    valid_window_end: str = "13:00"     # no trades after 13:00 IST
    sl_buffer_points: int = 20          # wider SL
    t1_booking_pct: float = 0.60        # faster profit taking
    afternoon_block: bool = True        # hard block after valid_window_end
    
    # Gap specific
    gap_detected: bool = False
    gap_type: Optional[str] = None        # "gap_up" or "gap_down"
    gap_adjusted_start: Optional[str] = None   # e.g. "10:15" or "10:30"
    effective_window: Optional[str] = None     # human readable
    no_trading_today: bool = False

class ExpiryManager:
    def __init__(self, trading_config: TradingConfig):
        self._trading_config = trading_config

    def get_config(self) -> ExpiryConfig:
        """
        Determine if today is NIFTY expiry day and return ExpiryConfig.
        
        Logic:
        1. Get current date in IST
        2. If weekday == Tuesday (weekday() == 1): is_expiry_day = True
        3. Check for exchange holidays (see below)
        4. Return ExpiryConfig with appropriate overrides
        """
        expiry_day = self.is_expiry_day()
        if expiry_day:
            logger.info("Today IS expiry day — applying expiry config")
        else:
            logger.info("Today is NOT expiry day — standard config active")
            
        return ExpiryConfig(is_expiry_day=expiry_day)

    def is_expiry_day(self) -> bool:
        """Returns True if today is NIFTY weekly expiry."""
        ist_zone = ZoneInfo("Asia/Kolkata")
        today_ist = datetime.datetime.now(ist_zone).date()
        
        # Tuesday is 1 in Python's weekday()
        is_tuesday = today_ist.weekday() == 1
        
        if is_tuesday:
            if self._is_exchange_holiday(today_ist):
                logger.warning(f"Today ({today_ist}) is a Tuesday but it's an exchange holiday. Expiry shifted.")
                return False
            return True
            
        # If today is a Tuesday but it's a holiday, expiry shifts to Monday.
        # This means if today is Monday and tomorrow is a Tuesday holiday, today is expiry day.
        is_monday = today_ist.weekday() == 0
        if is_monday:
            tomorrow = today_ist + datetime.timedelta(days=1)
            if self._is_exchange_holiday(tomorrow):
                logger.info(f"Tomorrow ({tomorrow}) is a Tuesday holiday. Expiry shifted to today ({today_ist}).")
                return True
                
        return False

    def _is_exchange_holiday(self, check_date: date) -> bool:
        """
        Hardcoded NSE holiday list for current year.
        If today is a Tuesday but it's a holiday, expiry shifts to Monday.
        Maintain EXCHANGE_HOLIDAYS as a set of date objects in this file.
        Log a WARNING if today appears to be a Tuesday holiday.
        """
        return check_date in EXCHANGE_HOLIDAYS

    def apply_to_config(self, trading_config: TradingConfig) -> TradingConfig:
        """
        Returns a COPY of trading_config with expiry overrides applied.
        Never mutates the original CONFIG singleton.
        """
        expiry_config = self.get_config()
        if not expiry_config.is_expiry_day:
            # We return a copy even if not expiry, to ensure it doesn't accidentally mutate
            # the singleton if the caller manipulates the returned object, though returning
            # trading_config is fine too. Let's strictly return a copy as requested.
            return dataclasses.replace(trading_config)

        return dataclasses.replace(
            trading_config,
            min_confirmations=expiry_config.min_confirmations,
            valid_window_end=expiry_config.valid_window_end,
            sl_buffer_points=expiry_config.sl_buffer_points,
            t1_booking_pct=expiry_config.t1_booking_pct,
            afternoon_block=expiry_config.afternoon_block
        )

    def apply_gap_override(
        self,
        expiry_config: ExpiryConfig,
        gap_info: Optional[dict],
    ) -> ExpiryConfig:
        """
        If expiry day AND gap detected, adjust the effective trading window.
        
        gap_pct < 0.5%:  no adjustment (normal expiry rules)
        gap_pct 0.5-1.0%: extend no-trade period to 10:15
                           effective_window = "10:15 – 13:00"
        gap_pct > 1.0%:  extend no-trade period to 10:30
                           effective_window = "10:30 – 13:00"
        gap_pct > 1.5%:  NO TRADING on this expiry day
                           valid_window_end = gap_adjusted_start
                           (window never opens)
                           send Discord: "Expiry + large gap: no trading today"
        
        Returns updated copy of expiry_config.
        Never mutates original.
        """
        if not gap_info or not expiry_config.is_expiry_day:
            return dataclasses.replace(expiry_config)
            
        gap_pct = gap_info.get('gap_pct', 0.0)
        gap_type = gap_info.get('type', 'gap_up')
        
        t1 = self._trading_config.gap_threshold_pct
        t2 = getattr(self._trading_config, 'expiry_gap_tier2_pct', 0.010)
        t3 = getattr(self._trading_config, 'expiry_gap_tier3_pct', 0.015)
        
        updated = dataclasses.replace(expiry_config, gap_detected=True, gap_type=gap_type)
        
        if gap_pct > t3:
            updated.no_trading_today = True
            updated.gap_adjusted_start = "13:00"
            updated.valid_window_end = "13:00"
            updated.effective_window = "No trading"
        elif gap_pct > t2:
            updated.gap_adjusted_start = "10:30"
            updated.effective_window = "10:30 – 13:00"
        elif gap_pct > t1:
            updated.gap_adjusted_start = "10:15"
            updated.effective_window = "10:15 – 13:00"
        else:
            updated.effective_window = f"{self._trading_config.no_trade_until} – 13:00"
            
        return updated
