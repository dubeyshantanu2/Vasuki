import logging
from dataclasses import dataclass
from typing import Optional, List
import pandas as pd
from threading import Lock

from data.dhan_ws import Tick

logger = logging.getLogger(__name__)

@dataclass
class ClassifiedTick:
    tick: Tick                    # original tick from WebSocket
    direction: str                # "buy" or "sell"
    buy_volume: int
    sell_volume: int

@dataclass
class DeltaCandle:
    interval_start: pd.Timestamp  # candle open time (IST)
    interval_end: pd.Timestamp    # candle close time (IST)
    buy_volume: float
    sell_volume: float
    delta: float                  # buy_volume - sell_volume
    cumulative_delta: float       # running delta from session start
    high_delta: float             # max delta reached within candle
    low_delta: float              # min delta reached within candle
    is_complete: bool             # True when candle interval has closed

class DeltaBuilder:
    def __init__(self, interval_minutes: int = 5):
        self.interval_minutes = interval_minutes
        self._lock = Lock()
        
        self.current_candle: Optional[DeltaCandle] = None
        self.completed_candles: List[DeltaCandle] = []
        self.cumulative_delta: float = 0.0

    def classify_tick(self, tick: Tick) -> ClassifiedTick:
        """
        Uptick Rule:
          if tick.ltp >= tick.prev_ltp -> BUY  (aggressive buyer)
          if tick.ltp <  tick.prev_ltp -> SELL (aggressive seller)
        """
        if tick.ltp >= tick.prev_ltp:
            return ClassifiedTick(
                tick=tick,
                direction="buy",
                buy_volume=tick.ltq,
                sell_volume=0
            )
        else:
            return ClassifiedTick(
                tick=tick,
                direction="sell",
                buy_volume=0,
                sell_volume=tick.ltq
            )

    def process_tick(self, tick: Tick) -> Optional[DeltaCandle]:
        """
        Classify the tick and add to the current building candle.
        
        Returns a completed DeltaCandle when the candle interval closes.
        Returns None if the candle is still building.
        """
        with self._lock:
            classified = self.classify_tick(tick)
            
            # Use floor to align to intervals natively
            interval_start = tick.timestamp.floor(f'{self.interval_minutes}min')
            interval_end = interval_start + pd.Timedelta(minutes=self.interval_minutes)
            
            completed_candle = None
            
            # Session reset logic check: if interval_start is a new day?
            # Or perhaps explicit session resets are preferred via reset_session.
            # We'll just rely on the user to call reset_session(), but we can also
            # check if the date changed.
            
            # If we don't have a current candle, start one
            if self.current_candle is None:
                self._start_new_candle(interval_start, interval_end)
            elif self.current_candle.interval_start != interval_start:
                # The interval has changed. Close the current one.
                self.current_candle.is_complete = True
                completed_candle = self.current_candle
                self.completed_candles.append(completed_candle)
                logger.debug(f"Completed DeltaCandle: {completed_candle.interval_start} | Delta: {completed_candle.delta} | CumDelta: {completed_candle.cumulative_delta}")
                
                # Start a new candle for the current tick
                self._start_new_candle(interval_start, interval_end)
                
            # Update current candle
            c = self.current_candle
            c.buy_volume += classified.buy_volume
            c.sell_volume += classified.sell_volume
            tick_delta = classified.buy_volume - classified.sell_volume
            c.delta += tick_delta
            self.cumulative_delta += tick_delta
            c.cumulative_delta = self.cumulative_delta
            
            if c.delta > c.high_delta:
                c.high_delta = c.delta
            if c.delta < c.low_delta:
                c.low_delta = c.delta
                
            return completed_candle

    def _start_new_candle(self, start: pd.Timestamp, end: pd.Timestamp):
        """Helper to initialize a new candle."""
        self.current_candle = DeltaCandle(
            interval_start=start,
            interval_end=end,
            buy_volume=0.0,
            sell_volume=0.0,
            delta=0.0,
            cumulative_delta=self.cumulative_delta, # Will be immediately updated
            high_delta=0.0,
            low_delta=0.0,
            is_complete=False
        )

    def get_current_candle(self) -> Optional[DeltaCandle]:
        """Returns the currently-building incomplete candle (is_complete=False)."""
        with self._lock:
            # Return a copy to prevent mutation? Not requested, but safe.
            return self.current_candle

    def get_completed_candles(self) -> List[DeltaCandle]:
        """Returns all completed DeltaCandles this session, ascending."""
        with self._lock:
            return list(self.completed_candles)

    def detect_divergence(
        self,
        price_series: List[float],
        delta_series: List[float],
        lookback: int = 3,
    ) -> Optional[str]:
        """
        Detects delta divergence over last `lookback` candles.
        
        Bullish divergence (absorption at lows):
          price makes lower low BUT delta does NOT -> return "bullish"
        
        Bearish divergence (distribution at highs):
          price makes higher high BUT delta does NOT -> return "bearish"
        """
        if len(price_series) < lookback + 1 or len(delta_series) < lookback + 1:
            return None
            
        current_price = price_series[-1]
        current_delta = delta_series[-1]
        
        # Compare with the candle `lookback` steps ago
        # For a lookback of 3, we look at index -(lookback+1)
        ref_price = price_series[-(lookback + 1)]
        ref_delta = delta_series[-(lookback + 1)]
        
        # Bullish divergence: price makes lower low (current_price <= ref_price)
        # BUT delta does NOT (current_delta >= ref_delta)
        if current_price <= ref_price and current_delta >= ref_delta:
            return "bullish"
            
        # Bearish divergence: price makes higher high (current_price >= ref_price)
        # BUT delta does NOT (current_delta <= ref_delta)
        if current_price >= ref_price and current_delta <= ref_delta:
            return "bearish"
            
        return None

    def reset_session(self) -> None:
        """Call at start of each new trading session. Clears all state."""
        with self._lock:
            self.current_candle = None
            self.completed_candles.clear()
            self.cumulative_delta = 0.0
