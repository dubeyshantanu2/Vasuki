from enum import Enum

class Bias(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

class StructureEvent(Enum):
    BOS_BULLISH = "bos_bullish"
    BOS_BEARISH = "bos_bearish"
    CHOCH_BULLISH = "choch_bullish"   # potential bullish reversal forming
    CHOCH_BEARISH = "choch_bearish"   # potential bearish reversal forming
    FAILED_CHOCH_BULLISH = "failed_choch_bullish"
    FAILED_CHOCH_BEARISH = "failed_choch_bearish"
    GAP_OPEN = "gap_open"
    NONE = "none"
