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
    NONE = "none"
