from dataclasses import dataclass
from typing import Optional, List
import pandas as pd
from core import Bias, StructureEvent

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

    def __init__(self, lookback: int = 3):
        """lookback: number of candles each side for swing detection"""
        self.lookback = lookback

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
        return self._detect_swings_static(df, self.lookback)

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
        
        state = StructureState(
            bias=Bias.NEUTRAL,
            last_event=StructureEvent.NONE,
            last_swing_high=None,
            last_swing_low=None,
            prev_swing_high=None,
            prev_swing_low=None,
            is_clear=False
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

        # BOS / CHoCH detection using the current candle close
        curr_close = df.close.iloc[-1]
        
        if state.bias == Bias.BULLISH:
            if curr_close > state.last_swing_high.price:
                state.last_event = StructureEvent.BOS_BULLISH
            elif curr_close < state.last_swing_low.price:
                state.last_event = StructureEvent.CHOCH_BEARISH
        elif state.bias == Bias.BEARISH:
            if curr_close < state.last_swing_low.price:
                state.last_event = StructureEvent.BOS_BEARISH
            elif curr_close > state.last_swing_high.price:
                state.last_event = StructureEvent.CHOCH_BULLISH
        elif state.bias == Bias.NEUTRAL:
            # Check if breaking neutral structure leads to a potential bias
            if curr_close > state.last_swing_high.price:
                state.last_event = StructureEvent.BOS_BULLISH
            elif curr_close < state.last_swing_low.price:
                state.last_event = StructureEvent.BOS_BEARISH

        # is_clear logic
        state.is_clear = True
        
        # - Last event was CHoCH
        if state.last_event in [StructureEvent.CHOCH_BULLISH, StructureEvent.CHOCH_BEARISH]:
            state.is_clear = False
            
        # - Fewer than 2 confirmed swings of each type (already handled by len(swings) < 4 and walk logic)
        
        # - Price is between swings with no clear direction
        # If bias is Neutral, it's not "clear"
        if state.bias == Bias.NEUTRAL:
            state.is_clear = False
            
        # If price is range-bound between the last high and low in a Neutral structure
        if state.bias == Bias.NEUTRAL and curr_close < state.last_swing_high.price and curr_close > state.last_swing_low.price:
            state.is_clear = False

        return state

    def get_current_bias(self, df: pd.DataFrame) -> Bias:
        """Convenience method. Returns Bias only."""
        return self.analyze(df).bias
