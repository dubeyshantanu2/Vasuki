import pandas as pd
from data.dhan_ws import Tick
from core.delta import DeltaBuilder

def test_delta_builder():
    builder = DeltaBuilder(interval_minutes=5)
    
    # Simulate ticks for first candle (9:15 to 9:20)
    tick1 = Tick(security_id="1", timestamp=pd.Timestamp("2023-10-01 09:15:05", tz="Asia/Kolkata"), ltp=100.0, ltq=10, prev_ltp=99.0) # BUY 10
    tick2 = Tick(security_id="1", timestamp=pd.Timestamp("2023-10-01 09:16:10", tz="Asia/Kolkata"), ltp=99.5, ltq=5, prev_ltp=100.0) # SELL 5
    tick3 = Tick(security_id="1", timestamp=pd.Timestamp("2023-10-01 09:18:20", tz="Asia/Kolkata"), ltp=99.8, ltq=8, prev_ltp=99.5) # BUY 8

    res1 = builder.process_tick(tick1)
    res2 = builder.process_tick(tick2)
    res3 = builder.process_tick(tick3)
    
    assert res1 is None
    assert res2 is None
    assert res3 is None
    
    curr = builder.get_current_candle()
    assert curr is not None
    assert curr.buy_volume == 18.0
    assert curr.sell_volume == 5.0
    assert curr.delta == 13.0
    assert curr.cumulative_delta == 13.0
    assert curr.high_delta == 13.0
    
    # Simulate tick that triggers next candle (9:20)
    tick4 = Tick(security_id="1", timestamp=pd.Timestamp("2023-10-01 09:20:05", tz="Asia/Kolkata"), ltp=100.5, ltq=20, prev_ltp=99.8) # BUY 20
    res4 = builder.process_tick(tick4)
    
    assert res4 is not None # Completed candle 1
    assert res4.is_complete
    assert len(builder.get_completed_candles()) == 1
    
    curr2 = builder.get_current_candle()
    assert curr2.buy_volume == 20.0
    assert curr2.sell_volume == 0.0
    assert curr2.delta == 20.0
    assert curr2.cumulative_delta == 33.0 # 13 + 20
    
    # Test divergence
    price_series = [100.0, 99.0, 98.0, 97.0] # Lower lows
    delta_series = [10.0, -5.0, 5.0, 15.0] # Increasing delta
    div = builder.detect_divergence(price_series, delta_series, lookback=3)
    assert div == "bullish"
    
    print("All delta tests passed.")

if __name__ == "__main__":
    test_delta_builder()
