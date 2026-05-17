from dataclasses import dataclass
from typing import Optional, List
import pandas as pd
import logging
from core import Bias, StructureEvent

logger = logging.getLogger(__name__)

@dataclass
class SwingPoint:
    index: int
    timestamp: pd.Timestamp
    price: float
    type: str      # "high" or "low"

@dataclass
class StructureState:
    bias: Bias
    last_event: StructureEvent
    last_swing_high: Optional[SwingPoint]
    last_swing_low: Optional[SwingPoint]
    prev_swing_high: Optional[SwingPoint]
    prev_swing_low: Optional[SwingPoint]
    is_clear: bool     # False if CHoCH just occurred or range-bound
    gap_info: Optional[dict] = None
    confidence: str = "normal"   # "normal", "high", "low"
    adaptive_lookback_used: int = 3

class MarketStructureEngine:
    """
    Analyzes market structure to detect trends (HH/HL, LH/LL) and key events (BOS, CHoCH).
    
    HH/HL Classification Logic:
    - Higher High (HH): Current swing high > Previous swing high
    - Higher Low (HL): Current swing low > Previous swing low
    - Lower High (LH): Current swing high < Previous swing high
    - Lower Low (LL): Current swing low < Previous swing low
    
    Bias Classification:
    - BULLISH: Established HH and HL
    - BEARISH: Established LH and LL
    - NEUTRAL: No clear trend or opposing signals (e.g., HH and LL)
    """

    GAP_THRESHOLD_PCT: float = 0.005   # 0.5% — configurable via CONFIG

    def __init__(self, lookback: int = 3, gap_threshold_pct: float = 0.005, 
                 atr_multiplier_medium: float = 1.5, atr_multiplier_high: float = 2.0, 
                 atr_lookback_candles: int = 20):
        """lookback: number of candles each side for swing detection"""
        self.base_lookback = lookback
        self.GAP_THRESHOLD_PCT = gap_threshold_pct
        self.atr_multiplier_medium = atr_multiplier_medium
        self.atr_multiplier_high = atr_multiplier_high
        self.atr_lookback_candles = atr_lookback_candles
        self._baseline_atr: Optional[float] = None

    def _compute_adaptive_lookback(self, df: pd.DataFrame) -> int:
        """
        Computes ATR over last N candles.
        Compares to baseline ATR.
        
        If current_atr > 1.5x avg_atr: lookback = 5
        If current_atr > 2.0x avg_atr: lookback = 7
        Else: lookback = self.base_lookback
        """
        if self._baseline_atr is None or len(df) < self.atr_lookback_candles:
            return self.base_lookback
            
        recent_df = df.iloc[-self.atr_lookback_candles:]
        current_atr = (recent_df['high'] - recent_df['low']).mean()
        
        if current_atr > self.atr_multiplier_high * self._baseline_atr:
            adaptive_lookback = 7
        elif current_atr > self.atr_multiplier_medium * self._baseline_atr:
            adaptive_lookback = 5
        else:
            adaptive_lookback = self.base_lookback
            
        if adaptive_lookback != self.base_lookback:
            logger.info(f"Adaptive lookback: {self.base_lookback} → {adaptive_lookback} "
                        f"(ATR {current_atr:.1f} vs avg {self._baseline_atr:.1f})")
                        
        return max(self.base_lookback, adaptive_lookback)

    def _get_event_for_close(self, close_price: float, state: StructureState) -> StructureEvent:
        if not state.last_swing_high or not state.last_swing_low:
            return StructureEvent.NONE
            
        if state.bias == Bias.BULLISH:
            if close_price > state.last_swing_high.price:
                return StructureEvent.BOS_BULLISH
            elif close_price < state.last_swing_low.price:
                return StructureEvent.CHOCH_BEARISH
        elif state.bias == Bias.BEARISH:
            if close_price < state.last_swing_low.price:
                return StructureEvent.BOS_BEARISH
            elif close_price > state.last_swing_high.price:
                return StructureEvent.CHOCH_BULLISH
        elif state.bias == Bias.NEUTRAL:
            if close_price > state.last_swing_high.price:
                return StructureEvent.BOS_BULLISH
            elif close_price < state.last_swing_low.price:
                return StructureEvent.BOS_BEARISH
        return StructureEvent.NONE

    def _detect_failed_choch(
        self,
        events: list[StructureEvent],
        current_event: StructureEvent,
    ) -> Optional[StructureEvent]:
        """
        If the last event was CHoCH and the current event is BOS
        in the opposite direction of the CHoCH:
        
        CHoCH_BEARISH followed by BOS_BULLISH:
            → FAILED_CHOCH_BULLISH
            → bias = BULLISH, is_clear = True, confidence = "high"
        
        CHoCH_BULLISH followed by BOS_BEARISH:
            → FAILED_CHOCH_BEARISH
            → bias = BEARISH, is_clear = True, confidence = "high"
        
        Returns the failed CHoCH event or None.
        """
        last_sig = None
        for e in reversed(events):
            if e != StructureEvent.NONE:
                last_sig = e
                break

        if last_sig == StructureEvent.CHOCH_BEARISH and current_event == StructureEvent.BOS_BULLISH:
            return StructureEvent.FAILED_CHOCH_BULLISH
        elif last_sig == StructureEvent.CHOCH_BULLISH and current_event == StructureEvent.BOS_BEARISH:
            return StructureEvent.FAILED_CHOCH_BEARISH
            
        return None

    def detect_gap_open(self, df: pd.DataFrame) -> Optional[dict]:
        """
        Compares today's first candle open to yesterday's last candle close.
        
        Returns dict if gap detected:
        {
            "type": "gap_up" or "gap_down",
            "gap_points": float,
            "gap_pct": float,
            "prev_close": float,
            "today_open": float,
        }
        Returns None if no significant gap.
        """
        if df.empty or 'timestamp' not in df.columns:
            return None
            
        dates = df['timestamp'].dt.date.unique()
        if len(dates) < 2:
            return None
            
        today = dates[-1]
        yesterday = dates[-2]
        
        today_df = df[df['timestamp'].dt.date == today]
        yesterday_df = df[df['timestamp'].dt.date == yesterday]
        
        if today_df.empty or yesterday_df.empty:
            return None
            
        today_open = float(today_df.iloc[0]['open'])
        prev_close = float(yesterday_df.iloc[-1]['close'])
        
        if prev_close == 0:
            return None
            
        gap_pct = abs(today_open - prev_close) / prev_close
        if gap_pct >= self.GAP_THRESHOLD_PCT:
            gap_points = abs(today_open - prev_close)
            gap_type = "gap_up" if today_open > prev_close else "gap_down"
            return {
                "type": gap_type,
                "gap_points": float(gap_points),
                "gap_pct": float(gap_pct),
                "prev_close": float(prev_close),
                "today_open": float(today_open),
            }
        return None

    def is_gap_settled(self, df: pd.DataFrame, gap_info: dict) -> bool:
        """
        Returns True when gap is considered settled and structure is reliable.
        
        Condition: At least 2 complete 1H candles have formed since gap open
        AND those candles show consistent direction (both bullish or both bearish
        body direction — not mixed).
        
        Until settled: SignalEngine must not evaluate gates.
        """
        if df.empty or 'timestamp' not in df.columns:
            return False
            
        dates = df['timestamp'].dt.date.unique()
        if len(dates) == 0:
            return False
            
        today = dates[-1]
        today_df = df[df['timestamp'].dt.date == today]
        
        req_candles = 3 if gap_info.get("gap_pct", 0) >= 0.01 else 2
        
        if len(today_df) < req_candles:
            return False
            
        directions = []
        for i in range(req_candles):
            c_open = today_df.iloc[i]['open']
            c_close = today_df.iloc[i]['close']
            if c_close > c_open:
                directions.append('bullish')
            elif c_close < c_open:
                directions.append('bearish')
            else:
                directions.append('doji')
                
        first_dir = directions[0]
        if first_dir == 'doji':
            return False
            
        for d in directions:
            if d != first_dir:
                return False
                
        return True

    def detect_swings(self, df: pd.DataFrame) -> List[SwingPoint]:
        """
        Input: DataFrame with columns [timestamp, open, high, low, close, volume]
        Returns list of SwingPoints sorted by index ascending.
        
        Swing High: df.high[i] > df.high[i-n] AND df.high[i] > df.high[i+n]
                    for all n in range(1, lookback+1)
        Swing Low:  df.low[i]  < df.low[i-n]  AND df.low[i]  < df.low[i+n]
                    for all n in range(1, lookback+1)
        
        Do NOT include the last `lookback` candles (incomplete swing detection).
        """
        lookback = self._compute_adaptive_lookback(df)
        return self._detect_swings_static(df, lookback)

    @staticmethod
    def _detect_swings_static(df: pd.DataFrame, lookback: int) -> List[SwingPoint]:
        """Pure function for swing detection."""
        swings = []
        n = lookback
        
        if len(df) < 2 * n + 1:
            return []

        highs = df['high'].values
        lows = df['low'].values
        
        if 'timestamp' in df.columns:
            timestamps = df['timestamp']
        else:
            timestamps = df.index

        for i in range(n, len(df) - n):
            curr_high = highs[i]
            curr_low = lows[i]
            
            is_high = True
            is_low = True
            
            for j in range(1, n + 1):
                if not (curr_high > highs[i - j] and curr_high > highs[i + j]):
                    is_high = False
                if not (curr_low < lows[i - j] and curr_low < lows[i + j]):
                    is_low = False
                if not is_high and not is_low:
                    break
            
            if is_high:
                swings.append(SwingPoint(
                    index=i,
                    timestamp=pd.Timestamp(timestamps[i]),
                    price=float(curr_high),
                    type="high"
                ))
            if is_low:
                swings.append(SwingPoint(
                    index=i,
                    timestamp=pd.Timestamp(timestamps[i]),
                    price=float(curr_low),
                    type="low"
                ))
                
        swings.sort(key=lambda x: x.index)
        return swings

    def analyze(self, df: pd.DataFrame) -> StructureState:
        """
        Full analysis on a candle DataFrame.
        Returns current StructureState.
        
        Logic:
        1. Detect all swings
        2. Walk swings in order to classify HH/HL/LH/LL
        3. Detect BOS: current close breaks last swing high/low
        4. Detect CHoCH: opposing break in established trend
        5. Set is_clear=False if:
           - Last event was CHoCH
           - Fewer than 2 confirmed swings of each type
           - Price is between swings with no clear direction
        """
        swings = self.detect_swings(df)
        lookback = self._compute_adaptive_lookback(df)
        
        state = StructureState(
            bias=Bias.NEUTRAL,
            last_event=StructureEvent.NONE,
            last_swing_high=None,
            last_swing_low=None,
            prev_swing_high=None,
            prev_swing_low=None,
            is_clear=False,
            adaptive_lookback_used=lookback
        )
        
        if len(swings) < 4:
            return state

        # Walk swings to classify HH/HL/LH/LL and determine bias
        highs = []
        lows = []
        current_bias = Bias.NEUTRAL
        
        for s in swings:
            if s.type == "high":
                highs.append(s)
            else:
                lows.append(s)
                
            if len(highs) >= 2 and len(lows) >= 2:
                last_h = highs[-1]
                prev_h = highs[-2]
                last_l = lows[-1]
                prev_l = lows[-2]
                
                is_hh = last_h.price > prev_h.price
                is_hl = last_l.price > prev_l.price
                is_ll = last_l.price < prev_l.price
                is_lh = last_h.price < prev_h.price
                
                if is_hh and is_hl:
                    current_bias = Bias.BULLISH
                elif is_ll and is_lh:
                    current_bias = Bias.BEARISH
                else:
                    current_bias = Bias.NEUTRAL
        
        state.bias = current_bias
        state.last_swing_high = highs[-1]
        state.prev_swing_high = highs[-2]
        state.last_swing_low = lows[-1]
        state.prev_swing_low = lows[-2]

        # Get events for the last 4 candles to support failed CHoCH detection
        events_window = []
        lookback_candles = min(4, len(df))
        for i in range(-lookback_candles, 0):
            events_window.append(self._get_event_for_close(df.close.iloc[i], state))
            
        current_event = events_window[-1]
        past_events = events_window[:-1]
        
        state.last_event = current_event
        
        # is_clear logic
        state.is_clear = True
        
        # - Last event was CHoCH
        if state.last_event in [StructureEvent.CHOCH_BULLISH, StructureEvent.CHOCH_BEARISH]:
            state.is_clear = False
            
        # - Price is between swings with no clear direction
        # If bias is Neutral, it's not "clear"
        curr_close = df.close.iloc[-1]
        if state.bias == Bias.NEUTRAL:
            state.is_clear = False
            
        # If price is range-bound between the last high and low in a Neutral structure
        if state.bias == Bias.NEUTRAL and curr_close < state.last_swing_high.price and curr_close > state.last_swing_low.price:
            state.is_clear = False

        # Failed CHoCH detection
        failed_choch = self._detect_failed_choch(past_events, current_event)
        if failed_choch:
            state.last_event = failed_choch
            state.is_clear = True
            state.confidence = "high"
            if failed_choch == StructureEvent.FAILED_CHOCH_BULLISH:
                state.bias = Bias.BULLISH
                logger.info("Failed CHoCH detected — strong BULLISH confirmation")
            else:
                state.bias = Bias.BEARISH
                logger.info("Failed CHoCH detected — strong BEARISH confirmation")

        gap_info = self.detect_gap_open(df)
        if gap_info:
            state.gap_info = gap_info
            if not self.is_gap_settled(df, gap_info):
                state.is_clear = False
                state.last_event = StructureEvent.GAP_OPEN
                logger.warning(
                    f"Gap open detected: {gap_info['type']} {gap_info['gap_points']:.1f} pts "
                    f"({gap_info['gap_pct']:.2%})"
                )
                logger.warning("Structure marked UNCLEAR until 2 confirmed post-gap 1H candles")

        return state

    def get_current_bias(self, df: pd.DataFrame) -> Bias:
        """Convenience method. Returns Bias only."""
        return self.analyze(df).bias
