import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict
import pandas as pd
from threading import Lock

from core.delta import ClassifiedTick

logger = logging.getLogger(__name__)

@dataclass
class FootprintLevel:
    price: float           # price bucket (rounded to 0.5)
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    delta: float = 0.0           # buy - sell at this level
    imbalance_ratio: float = 1.0 # buy/sell or sell/buy - whichever is dominant

@dataclass
class FootprintCandle:
    interval_start: pd.Timestamp
    interval_end: pd.Timestamp
    levels: Dict[float, FootprintLevel] = field(default_factory=dict)   # keyed by price bucket
    is_complete: bool = False
    
    # Computed signals
    bid_absorption: bool = False       # sellers hitting bids, price not falling
    ask_absorption: bool = False       # buyers lifting asks, price not rising  
    stacked_imbalance_buy: bool = False   # 3+ consecutive levels: buy >> sell
    stacked_imbalance_sell: bool = False  # 3+ consecutive levels: sell >> buy
    dominant_side: str = "neutral"        # "buy", "sell", or "neutral"

class FootprintBuilder:
    def __init__(
        self,
        interval_minutes: int = 5,
        bucket_size: float = 0.5,
        imbalance_threshold: float = 3.0,   # ratio to flag imbalance
        stacked_count: int = 3,             # consecutive levels for stacked
    ):
        self.interval_minutes = interval_minutes
        self.bucket_size = bucket_size
        self.imbalance_threshold = imbalance_threshold
        self.stacked_count = stacked_count
        
        self._lock = Lock()
        self.current_candle: Optional[FootprintCandle] = None
        self.completed_candles: List[FootprintCandle] = []

    def record_feed_gap(self, gap_start: pd.Timestamp, gap_end: pd.Timestamp) -> None:
        """Mark current candle as having a feed gap."""
        with self._lock:
            if self.current_candle:
                self.current_candle.has_feed_gap = True

    def _get_bucket(self, price: float) -> float:
        return round(price / self.bucket_size) * self.bucket_size

    def process_tick(self, classified_tick: Optional[ClassifiedTick]) -> Optional[FootprintCandle]:
        """
        Add classified tick to current building footprint candle.
        Bucket tick.ltp to nearest bucket_size.
        
        Returns completed FootprintCandle when interval closes.
        Returns None if still building.
        """
        if classified_tick is None:
            return None
            
        tick = classified_tick.tick
        
        if tick.ltq == 0:
            return None
            
        with self._lock:
            # Match DeltaBuilder exactly
            interval_start = tick.timestamp.floor(f'{self.interval_minutes}min')
            interval_end = interval_start + pd.Timedelta(minutes=self.interval_minutes)
            
            completed_candle = None
            
            if self.current_candle is None:
                self._start_new_candle(interval_start, interval_end)
            elif self.current_candle.interval_start != interval_start:
                # Close the current candle
                self.current_candle.is_complete = True
                completed_candle = self._compute_signals(self.current_candle)
                self.completed_candles.append(completed_candle)
                
                logger.info(f"Footprint completed for {completed_candle.interval_start}: "
                            f"BidAbsorb={completed_candle.bid_absorption}, AskAbsorb={completed_candle.ask_absorption}, "
                            f"StackedBuy={completed_candle.stacked_imbalance_buy}, StackedSell={completed_candle.stacked_imbalance_sell}, "
                            f"Dominant={completed_candle.dominant_side}")
                
                # Start a new one
                self._start_new_candle(interval_start, interval_end)
            
            c = self.current_candle
            b_price = self._get_bucket(tick.ltp)
            
            if b_price not in c.levels:
                c.levels[b_price] = FootprintLevel(price=b_price)
                
            level = c.levels[b_price]
            level.buy_volume += classified_tick.buy_volume
            level.sell_volume += classified_tick.sell_volume
            level.delta = level.buy_volume - level.sell_volume
            
            # Update imbalance ratio safely
            if level.buy_volume == 0 and level.sell_volume == 0:
                level.imbalance_ratio = 1.0
            elif level.buy_volume > level.sell_volume:
                level.imbalance_ratio = level.buy_volume / level.sell_volume if level.sell_volume > 0 else float('inf')
            else:
                level.imbalance_ratio = level.sell_volume / level.buy_volume if level.buy_volume > 0 else float('inf')
                
            # Cap it slightly for printing/logical sanity if inf, or just leave as inf.
            # float('inf') works for > checks.
            
            return completed_candle

    def _start_new_candle(self, start: pd.Timestamp, end: pd.Timestamp):
        self.current_candle = FootprintCandle(
            interval_start=start,
            interval_end=end,
            levels={},
            is_complete=False
        )

    def _compute_signals(self, candle: FootprintCandle) -> FootprintCandle:
        """
        Internal. Called when candle closes.
        """
        if not candle.levels:
            return candle
            
        total_buy = sum(l.buy_volume for l in candle.levels.values())
        total_sell = sum(l.sell_volume for l in candle.levels.values())
        total_vol = total_buy + total_sell
        
        # Dominant side
        if total_vol > 0:
            diff = abs(total_buy - total_sell)
            if diff < 0.10 * total_vol:
                candle.dominant_side = "neutral"
            elif total_buy > total_sell:
                candle.dominant_side = "buy"
            else:
                candle.dominant_side = "sell"
                
        # Stacked Imbalances
        sorted_prices = sorted(candle.levels.keys())
        buy_streak = 0
        sell_streak = 0
        
        for price in sorted_prices:
            lvl = candle.levels[price]
            
            if lvl.buy_volume > lvl.sell_volume and lvl.imbalance_ratio >= self.imbalance_threshold:
                buy_streak += 1
                sell_streak = 0
            elif lvl.sell_volume > lvl.buy_volume and lvl.imbalance_ratio >= self.imbalance_threshold:
                sell_streak += 1
                buy_streak = 0
            else:
                buy_streak = 0
                sell_streak = 0
                
            if buy_streak >= self.stacked_count:
                candle.stacked_imbalance_buy = True
            if sell_streak >= self.stacked_count:
                candle.stacked_imbalance_sell = True
                
        # Absorption Detection
        if self.completed_candles:
            prior_candle = self.completed_candles[-1]
            if prior_candle.levels:
                prior_high = max(prior_candle.levels.keys())
                prior_low = min(prior_candle.levels.keys())
                
                curr_high = sorted_prices[-1]
                curr_low = sorted_prices[0]
                
                high_lvl = candle.levels[curr_high]
                low_lvl = candle.levels[curr_low]
                
                # Ask Absorption:
                # At the HIGH of the candle: buy_volume > sell_volume
                # BUT candle high did not break above prior candle high
                if high_lvl.buy_volume > high_lvl.sell_volume and curr_high <= prior_high:
                    candle.ask_absorption = True
                    
                # Bid Absorption:
                # At the LOW of the candle: sell_volume > buy_volume
                # BUT candle low did not break below prior candle low
                if low_lvl.sell_volume > low_lvl.buy_volume and curr_low >= prior_low:
                    candle.bid_absorption = True
                    
        return candle

    def get_current_candle(self) -> Optional[FootprintCandle]:
        """Returns currently-building candle."""
        with self._lock:
            return self.current_candle

    def get_completed_candles(self) -> List[FootprintCandle]:
        """All completed candles this session, ascending."""
        with self._lock:
            return list(self.completed_candles)

    def reset_session(self) -> None:
        """Reset all state for new session."""
        with self._lock:
            self.current_candle = None
            self.completed_candles.clear()
