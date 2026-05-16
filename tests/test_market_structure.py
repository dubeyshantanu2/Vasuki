import pandas as pd
import numpy as np
from core.market_structure import MarketStructureEngine, Bias, StructureEvent

def test_market_structure():
    # Create a synthetic bullish trend: HH, HL, HH, HL
    # Indices of swings:
    # Low at 5 (price 10)
    # High at 10 (price 20)
    # Low at 15 (price 15) - HL
    # High at 20 (price 25) - HH
    
    data = {
        'timestamp': pd.date_range(start='2023-01-01', periods=30, freq='h'),
        'open': [10] * 30,
        'high': [15] * 30,
        'low': [5] * 30,
        'close': [10] * 30,
        'volume': [100] * 30
    }
    df = pd.DataFrame(data)
    
    # Set specific prices to create swings (lookback=3)
    # Swing Low at index 5: low[5]=2, neighbors[2:5] and [6:9] > 2
    df.loc[2:8, 'low'] = 5
    df.loc[5, 'low'] = 2
    
    # Swing High at index 10: high[10]=20, neighbors[7:10] and [11:13] < 20
    df.loc[7:13, 'high'] = 15
    df.loc[10, 'high'] = 20
    
    # Swing Low at index 15: low[15]=8 (HL since 8 > 2)
    df.loc[12:18, 'low'] = 10
    df.loc[15, 'low'] = 8
    
    # Swing High at index 20: high[20]=25 (HH since 25 > 20)
    df.loc[17:23, 'high'] = 20
    df.loc[20, 'high'] = 25
    
    # Final close for BOS/CHoCH check
    df.loc[29, 'close'] = 26 # Should trigger BOS Bullish
    
    engine = MarketStructureEngine(lookback=3)
    swings = engine.detect_swings(df)
    
    print(f"Detected {len(swings)} swings")
    for s in swings:
        print(f"Swing {s.type} at {s.index}: {s.price}")
        
    state = engine.analyze(df)
    print(f"Bias: {state.bias}")
    print(f"Last Event: {state.last_event}")
    print(f"Is Clear: {state.is_clear}")
    
    assert len(swings) >= 4
    assert state.bias == Bias.BULLISH
    assert state.last_event == StructureEvent.BOS_BULLISH
    assert state.is_clear == True

    # Test CHoCH Bearish
    df.loc[29, 'close'] = 7 # Breaks last low at 8
    state = engine.analyze(df)
    print(f"New Bias: {state.bias}")
    print(f"New Event: {state.last_event}")
    print(f"New Is Clear: {state.is_clear}")
    
    assert state.last_event == StructureEvent.CHOCH_BEARISH
    assert state.is_clear == False

if __name__ == "__main__":
    test_market_structure()
