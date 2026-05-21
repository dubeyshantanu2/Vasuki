import logging
import collections
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
class FeedGap:
    started_at: pd.Timestamp
    ended_at: pd.Timestamp
    duration_seconds: float
    affected_candle_start: pd.Timestamp

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
    has_feed_gap: bool = False    # True if feed disconnected during this candle
    outlier_ticks_count: int = 0  # number of capped ticks in this candle

class DeltaBuilder:
    def __init__(self, interval_minutes: int = 5, outlier_cap_multiplier: float = 3.0):
        self.interval_minutes = interval_minutes
        self._lock = Lock()
        
        self.current_candle: Optional[DeltaCandle] = None
        self.completed_candles: List[DeltaCandle] = []
        self.cumulative_delta: float = 0.0

        self._session_tick_volumes: collections.deque = collections.deque(maxlen=50)
        self._outlier_cap_multiplier: float = outlier_cap_multiplier
        self._outlier_ticks: collections.deque = collections.deque(maxlen=100)

    def _get_volume_cap(self) -> Optional[int]:
        """
        Returns the cap for a single tick's contribution to delta.
        Cap = outlier_cap_multiplier * rolling average LTQ (last 50 ticks).
        Returns None if fewer than 20 ticks seen (no cap yet — too early).
        """
        if len(self._session_tick_volumes) < 20:
            return None
        avg_ltq = sum(self._session_tick_volumes) / len(self._session_tick_volumes)
        return max(100, int(avg_ltq * self._outlier_cap_multiplier))

    def classify_tick(self, tick: Tick) -> ClassifiedTick:
        """
        Uptick Rule:
          if tick.ltp >= tick.prev_ltp -> BUY  (aggressive buyer)
          if tick.ltp <  tick.prev_ltp -> SELL (aggressive seller)
        """
        direction = "buy" if tick.ltp >= tick.prev_ltp else "sell"
        self._session_tick_volumes.append(tick.ltq)

        volume_cap = self._get_volume_cap()
        effective_ltq = tick.ltq

        classified_tick = ClassifiedTick(
            tick=tick,
            direction=direction,
            buy_volume=0,
            sell_volume=0
        )

        if volume_cap and tick.ltq > volume_cap:
            effective_ltq = volume_cap
            logger.info(
                f"Outlier tick capped: {tick.ltq} lots → {volume_cap} lots "
                f"at {tick.ltp} ({tick.timestamp})"
            )
            # Store the full tick separately for analysis
            self._outlier_ticks.append(classified_tick)

        # Use effective_ltq (not tick.ltq) for delta aggregation
        classified_tick.buy_volume = effective_ltq if direction == "buy" else 0
        classified_tick.sell_volume = effective_ltq if direction == "sell" else 0

        return classified_tick

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
            
            if tick.ltq > (classified.buy_volume + classified.sell_volume):
                c.outlier_ticks_count += 1
            
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
            is_complete=False,
            outlier_ticks_count=0
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
          
        Skip any candle in the lookback window that has has_feed_gap=True.
        If more than 1 candle in the lookback window is gapped:
            return None (cannot reliably detect divergence)
        Log at WARNING when a gapped candle is skipped.
        """
        with self._lock:
            if len(price_series) < lookback + 1 or len(delta_series) < lookback + 1:
                return None
            
            # Find the relevant completed candles
            relevant_candles = self.completed_candles[-(lookback + 1):]
                
            gapped_count = sum(1 for c in relevant_candles if c.has_feed_gap)
            if gapped_count > 1:
                return None
                
            if relevant_candles and relevant_candles[-1].has_feed_gap:
                logger.warning("Skipping divergence detection: current candle has feed gap")
                return None
                
            if relevant_candles and len(relevant_candles) >= lookback + 1 and relevant_candles[0].has_feed_gap:
                logger.warning("Skipping divergence detection: reference candle has feed gap")
                return None
                
            current_price = price_series[-1]
            current_delta = delta_series[-1]
            
            ref_price = price_series[-(lookback + 1)]
            ref_delta = delta_series[-(lookback + 1)]
            
            if current_price <= ref_price and current_delta >= ref_delta:
                return "bullish"
                
            if current_price >= ref_price and current_delta <= ref_delta:
                return "bearish"
                
            return None

    def reset_session(self) -> None:
        """Call at start of each new trading session. Clears all state."""
        with self._lock:
            if hasattr(self, 'zero_ltq_count') and self.zero_ltq_count > 0:
                logger.info(f"Session ended. Total zero LTQ ticks skipped: {self.zero_ltq_count}")
            self.current_candle = None
            self.completed_candles.clear()
            self.cumulative_delta = 0.0
            self.zero_ltq_count = 0
            self._session_tick_volumes.clear()
            self._outlier_ticks.clear()