import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple, Callable
import pandas as pd
from threading import Lock

from core.delta import ClassifiedTick

logger = logging.getLogger(__name__)

@dataclass
class BigTrade:
    timestamp: pd.Timestamp
    price: float
    quantity: int           # in lots
    direction: str          # "buy" or "sell"
    significance: str       # "large" (500-999) or "block" (1000+)

class BigTradeFilter:
    def __init__(
        self,
        threshold_lots: int = 500,          # minimum to flag as big trade
        block_threshold_lots: int = 1000,   # block trade threshold
        on_big_trade: Optional[Callable[[BigTrade], None]] = None # Callback for Supabase DB insertion
    ):
        self.threshold_lots = threshold_lots
        self.block_threshold_lots = block_threshold_lots
        self.on_big_trade = on_big_trade
        
        self._lock = Lock()
        self.big_trades: List[BigTrade] = []

    def process_tick(self, classified_tick: ClassifiedTick) -> Optional[BigTrade]:
        """
        If classified_tick.tick.ltq >= threshold_lots:
          -> Create and return BigTrade
        Else:
          -> Return None
        
        Direction inherited from classified_tick.direction.
        """
        tick = classified_tick.tick
        
        if tick.ltq >= self.threshold_lots:
            significance = "block" if tick.ltq >= self.block_threshold_lots else "large"
            
            big_trade = BigTrade(
                timestamp=tick.timestamp,
                price=tick.ltp,
                quantity=tick.ltq,
                direction=classified_tick.direction,
                significance=significance
            )
            
            with self._lock:
                self.big_trades.append(big_trade)
                
            logger.info(
                f"BigTrade: time={big_trade.timestamp}, price={big_trade.price}, "
                f"qty={big_trade.quantity}, dir={big_trade.direction}, sig={big_trade.significance}"
            )
            
            if self.on_big_trade:
                try:
                    self.on_big_trade(big_trade)
                except Exception as e:
                    logger.error(f"Error in on_big_trade callback: {e}")
                    
            return big_trade
            
        return None

    def get_recent_big_trades(
        self,
        within_seconds: int = 60,
        price_range: Optional[Tuple[float, float]] = None,
    ) -> List[BigTrade]:
        """
        Returns big trades in the last `within_seconds` seconds.
        Optionally filter by price range (low, high).
        """
        now = pd.Timestamp.now(tz="Asia/Kolkata")
        cutoff_time = now - pd.Timedelta(seconds=within_seconds)
        
        recent_trades = []
        with self._lock:
            # Iterate backwards for efficiency since list is chronologically ordered
            for trade in reversed(self.big_trades):
                if trade.timestamp < cutoff_time:
                    break
                    
                if price_range:
                    low, high = price_range
                    if not (low <= trade.price <= high):
                        continue
                        
                recent_trades.append(trade)
                
        # Return in chronological order
        return list(reversed(recent_trades))

    def get_dominant_side(
        self,
        within_seconds: int = 60,
        price_range: Optional[Tuple[float, float]] = None,
    ) -> Optional[str]:
        """
        Among recent big trades at a price zone:
          If buy lots > sell lots by 20%+: return "buy"
          If sell lots > buy lots by 20%+: return "sell"
          Else: return None (inconclusive)
        """
        recent_trades = self.get_recent_big_trades(within_seconds, price_range)
        
        if not recent_trades:
            return None
            
        buy_lots = sum(t.quantity for t in recent_trades if t.direction == "buy")
        sell_lots = sum(t.quantity for t in recent_trades if t.direction == "sell")
        
        # Checking if one side is 20%+ larger than the other
        if buy_lots >= sell_lots * 1.20 and buy_lots > 0:
            return "buy"
        elif sell_lots >= buy_lots * 1.20 and sell_lots > 0:
            return "sell"
            
        return None

    def reset_session(self) -> None:
        """Clear all stored big trades for new session."""
        with self._lock:
            self.big_trades.clear()
