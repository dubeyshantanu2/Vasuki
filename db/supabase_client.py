import logging
import asyncio
from typing import Optional, List, Dict
import pandas as pd
from supabase import create_client, Client
from pydantic import BaseModel

from config.settings import SupabaseConfig
from core.market_structure import StructureState
from core.volume_profile import VolumeProfile
from core.delta import DeltaCandle
from core.big_trades import BigTrade

logger = logging.getLogger(__name__)

# Temporary mock of Signal until signal_engine is fully built
class Signal(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    symbol: str
    triggered_at: pd.Timestamp
    direction: str
    bias: str
    zone_type: str
    zone_price: float
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    t1_price: Optional[float] = None
    t2_price: Optional[float] = None
    t3_price: Optional[float] = None
    confirmations: Dict[str, bool] = {}
    is_expiry_day: bool = False

class SupabaseClient:
    def __init__(self, config: SupabaseConfig):
        self.url = config.url
        self.key = config.key
        
        try:
            self.client: Client = create_client(self.url, self.key)
            logger.info("Initialized Supabase client")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}")
            self.client = None

    def _fire_and_forget(self, func, *args, **kwargs):
        """Helper to run a synchronous Supabase call in an executor asynchronously."""
        if not self.client:
            logger.error("Supabase client not initialized. Dropping write.")
            return
            
        loop = asyncio.get_event_loop()
        
        # We wrap the actual call in a try/except so exceptions are caught
        def safe_run():
            try:
                func(*args, **kwargs)
                logger.debug(f"Successfully executed {func.__name__} in Supabase")
            except Exception as e:
                logger.error(f"Failed executing {func.__name__} in Supabase: {e}")
                
        # Fire and forget: we don't await the future
        loop.run_in_executor(None, safe_run)

    def _safe_float(self, val) -> Optional[float]:
        """Convert objects (like numpy/pandas types) to standard float safely, handling NaN."""
        if val is None or pd.isna(val):
            return None
        return float(val)

    def save_market_structure(self, symbol: str, state: StructureState) -> None:
        """Saves current market structure state asynchronously."""
        data = {
            "captured_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "symbol": symbol,
            "bias": state.bias.value if hasattr(state.bias, 'value') else str(state.bias),
            "last_event": state.last_event.value if hasattr(state.last_event, 'value') else str(state.last_event),
            "is_clear": state.is_clear,
            "last_swing_high": self._safe_float(state.last_swing_high.price) if state.last_swing_high else None,
            "last_swing_low": self._safe_float(state.last_swing_low.price) if state.last_swing_low else None,
        }
        
        def do_insert():
            self.client.table("market_structure_snapshots").insert(data).execute()
            
        self._fire_and_forget(do_insert)

    def save_volume_profile(self, symbol: str, profile: VolumeProfile, session_type: str) -> None:
        """Saves volume profile asynchronously."""
        data = {
            "captured_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "symbol": symbol,
            "session_type": session_type,
            "poc": self._safe_float(profile.poc),
            "vah": self._safe_float(profile.vah),
            "val": self._safe_float(profile.val),
            "total_volume": self._safe_float(profile.total_volume),
        }
        
        def do_insert():
            self.client.table("volume_profile_snapshots").insert(data).execute()
            
        self._fire_and_forget(do_insert)

    def save_delta_candle(self, symbol: str, candle: DeltaCandle) -> None:
        """Saves completed delta candle asynchronously."""
        # Note: DeltaCandle doesn't inherently store the interval_minutes, we calculate it
        interval_mins = int((candle.interval_end - candle.interval_start).total_seconds() / 60)
        
        data = {
            "symbol": symbol,
            "interval_start": candle.interval_start.isoformat(),
            "interval_minutes": interval_mins,
            "buy_volume": self._safe_float(candle.buy_volume),
            "sell_volume": self._safe_float(candle.sell_volume),
            "delta": self._safe_float(candle.delta),
            "cumulative_delta": self._safe_float(candle.cumulative_delta),
        }
        
        def do_insert():
            self.client.table("delta_candles").insert(data).execute()
            
        self._fire_and_forget(do_insert)

    def save_big_trade(self, symbol: str, trade: BigTrade) -> None:
        """Saves a big trade asynchronously."""
        data = {
            "symbol": symbol,
            "traded_at": trade.timestamp.isoformat(),
            "price": self._safe_float(trade.price),
            "quantity_lots": int(trade.quantity),
            "direction": trade.direction,
            "significance": trade.significance,
        }
        
        def do_insert():
            self.client.table("big_trades").insert(data).execute()
            
        self._fire_and_forget(do_insert)

    def save_signal(self, signal: Signal) -> str:
        """
        Saves a signal and returns the UUID. 
        Note: The prompt asks this to return a str (UUID), meaning this *might* 
        need to be blocking/synchronous for the return value, OR we can generate 
        the UUID client-side and fire-and-forget the insert.
        Let's generate the UUID client-side so we can fire-and-forget.
        """
        import uuid
        signal_id = str(uuid.uuid4())
        
        data = {
            "id": signal_id,
            "symbol": signal.symbol,
            "triggered_at": signal.triggered_at.isoformat(),
            "direction": signal.direction,
            "bias": signal.bias,
            "zone_type": signal.zone_type,
            "zone_price": self._safe_float(signal.zone_price),
            "entry_price": self._safe_float(signal.entry_price),
            "sl_price": self._safe_float(signal.sl_price),
            "t1_price": self._safe_float(signal.t1_price),
            "t2_price": self._safe_float(signal.t2_price),
            "t3_price": self._safe_float(signal.t3_price),
            "confirmations": signal.confirmations,
            "is_expiry_day": signal.is_expiry_day,
        }
        
        def do_insert():
            self.client.table("signals").insert(data).execute()
            
        self._fire_and_forget(do_insert)
        return signal_id

    def get_today_signals(self, symbol: str) -> List[Dict]:
        """
        Returns all signals triggered today, descending by triggered_at.
        This is a READ operation, so it should be synchronous and block, 
        as the caller expects the result immediately.
        """
        if not self.client:
            logger.error("Supabase client not initialized. Cannot fetch signals.")
            return []
            
        try:
            today_start = pd.Timestamp.now(tz="Asia/Kolkata").replace(hour=0, minute=0, second=0, microsecond=0)
            
            response = (
                self.client.table("signals")
                .select("*")
                .eq("symbol", symbol)
                .gte("triggered_at", today_start.isoformat())
                .order("triggered_at", desc=True)
                .execute()
            )
            return response.data
        except Exception as e:
            logger.error(f"Failed to fetch today's signals: {e}")
            return []
