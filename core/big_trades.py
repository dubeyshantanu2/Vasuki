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
    is_suspected_rollover: bool = False
    is_outlier: bool = False        # single tick far above session average
    outlier_ratio: float = 1.0      # trade_size / session_avg_big_trade_size

class BigTradeFilter:
    ROLLOVER_SIZE_MULTIPLIER: float = 5.0    # 5x avg big trade = suspect rollover
    OUTLIER_SIZE_MULTIPLIER: float = 10.0   # 10x avg = definite outlier

    def __init__(
        self,
        threshold_lots: int = 500,          # minimum to flag as big trade
        block_threshold_lots: int = 1000,   # block trade threshold
        on_big_trade: Optional[Callable[[BigTrade], None]] = None # Callback for Supabase DB insertion
    ):
        self.threshold_lots = threshold_lots
        self.block_threshold_lots = block_threshold_lots
        self.on_big_trade = on_big_trade
        
        self._rollover_multiplier = self.ROLLOVER_SIZE_MULTIPLIER
        self._session_big_trade_sizes: List[int] = []
        
        self._lock = Lock()
        self.big_trades: List[BigTrade] = []

    def set_expiry_mode(self, is_expiry: bool) -> None:
        self._rollover_multiplier = 3.0 if is_expiry else self.ROLLOVER_SIZE_MULTIPLIER

    def _classify_trade(self, trade: BigTrade) -> BigTrade:
        """
        After creating the BigTrade, classify it further.
        
        Compute rolling average of big trade sizes this session.
        """
        with self._lock:
            self._session_big_trade_sizes.append(trade.quantity)
            if len(self._session_big_trade_sizes) > 20:
                self._session_big_trade_sizes.pop(0)
            
            # If fewer than 5 big trades seen: no rollover classification (not enough baseline)
            if len(self._session_big_trade_sizes) < 5:
                return trade
                
            avg = sum(self._session_big_trade_sizes) / len(self._session_big_trade_sizes)

        if trade.quantity > avg * self.OUTLIER_SIZE_MULTIPLIER:
            trade.is_outlier = True
            trade.is_suspected_rollover = True   # assume rollover for safety
        elif trade.quantity > avg * self._rollover_multiplier:
            trade.is_suspected_rollover = True
            # Could be real — flag but don't exclude unless we treat all suspected rollovers carefully
        
        trade.outlier_ratio = trade.quantity / avg if avg > 0 else 1.0
        return trade

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
            
            big_trade = self._classify_trade(big_trade)
            
            with self._lock:
                self.big_trades.append(big_trade)
                
            if big_trade.is_outlier:
                logger.warning(f"Outlier trade detected: {big_trade.quantity} lots at {big_trade.price} ({big_trade.outlier_ratio:.1f}x avg) — flagged as rollover")
            else:
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
        
        # Exclude suspected rollover trades from dominant side calculation
        clean_trades = [t for t in recent_trades if not t.is_suspected_rollover]
        
        if not clean_trades:
            return None
            
        buy_lots = sum(t.quantity for t in clean_trades if t.direction == "buy")
        sell_lots = sum(t.quantity for t in clean_trades if t.direction == "sell")
        
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
            self._session_big_trade_sizes.clear()
