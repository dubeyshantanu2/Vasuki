import pandas as pd
from data.dhan_ws import Tick
from core.delta import ClassifiedTick
from core.footprint import FootprintBuilder

def test_footprint_builder():
    builder = FootprintBuilder(interval_minutes=5, bucket_size=0.5, imbalance_threshold=3.0, stacked_count=3)
    
    # 1. First Candle (9:15 to 9:20)
    # Let's create some levels.
    
    ticks_c1 = [
        # Level 100.0: sell 5, buy 0 (price bucket 100.0) -> Low
        Tick("1", pd.Timestamp("2023-10-01 09:15:05", tz="Asia/Kolkata"), 100.0, 5, 100.5), # SELL
        # Level 100.5: buy 10, sell 0 (price bucket 100.5)
        Tick("1", pd.Timestamp("2023-10-01 09:16:00", tz="Asia/Kolkata"), 100.5, 10, 100.0), # BUY
        # Level 101.0: buy 10, sell 0 (price bucket 101.0)
        Tick("1", pd.Timestamp("2023-10-01 09:17:00", tz="Asia/Kolkata"), 101.0, 10, 100.5), # BUY
        # Level 101.5: buy 20, sell 0 (price bucket 101.5) -> High
        Tick("1", pd.Timestamp("2023-10-01 09:18:00", tz="Asia/Kolkata"), 101.5, 20, 101.0), # BUY
    ]
    
    for t in ticks_c1:
        direction = "buy" if t.ltp >= t.prev_ltp else "sell"
        ct = ClassifiedTick(t, direction, t.ltq if direction == "buy" else 0, t.ltq if direction == "sell" else 0)
        builder.process_tick(ct)

    # Trigger close of candle 1
    t_close1 = Tick("1", pd.Timestamp("2023-10-01 09:20:05", tz="Asia/Kolkata"), 100.5, 1, 101.5) # SELL
    ct_close1 = ClassifiedTick(t_close1, "sell", 0, 1)
    c1 = builder.process_tick(ct_close1)
    
    assert c1 is not None
    assert c1.is_complete
    assert len(c1.levels) == 4
    assert c1.dominant_side == "buy"
    # No absorption on first candle
    assert not c1.bid_absorption
    assert not c1.ask_absorption
    
    # Check stacked imbalance buy on C1
    # 100.5 (buy 10, sell 0) => ratio inf >= 3.0
    # 101.0 (buy 10, sell 0) => ratio inf >= 3.0
    # 101.5 (buy 20, sell 0) => ratio inf >= 3.0
    # That's 3 consecutive levels of buy imbalance
    assert c1.stacked_imbalance_buy

    # 2. Second Candle (9:20 to 9:25)
    # Prior High = 101.5, Prior Low = 100.0
    # Let's create Bid Absorption: 
    # Price reaches 100.0 (LOW), sell > buy, but doesn't break below 100.0
    ticks_c2 = [
        # Level 100.0 (LOW)
        Tick("1", pd.Timestamp("2023-10-01 09:21:00", tz="Asia/Kolkata"), 100.0, 10, 100.5), # SELL
        Tick("1", pd.Timestamp("2023-10-01 09:21:05", tz="Asia/Kolkata"), 100.0, 2, 99.5), # BUY (to add some buy vol)
        # Level 100.5 (HIGH)
        Tick("1", pd.Timestamp("2023-10-01 09:22:00", tz="Asia/Kolkata"), 100.5, 10, 100.0), # BUY
        Tick("1", pd.Timestamp("2023-10-01 09:22:05", tz="Asia/Kolkata"), 100.5, 2, 101.0), # SELL
    ]
    # For 100.0: sell_vol = 10, buy_vol = 2 -> sell > buy. Curr low (100.0) >= Prior low (100.0). -> bid_absorption = True
    # For 100.5: buy_vol = 10, sell_vol = 2 -> buy > sell. Curr high (100.5) <= Prior high (101.5). -> ask_absorption = True
    
    for t in ticks_c2:
        direction = "buy" if t.ltp >= t.prev_ltp else "sell"
        ct = ClassifiedTick(t, direction, t.ltq if direction == "buy" else 0, t.ltq if direction == "sell" else 0)
        builder.process_tick(ct)

    # Trigger close of candle 2
    t_close2 = Tick("1", pd.Timestamp("2023-10-01 09:25:05", tz="Asia/Kolkata"), 100.5, 1, 100.0) # BUY
    ct_close2 = ClassifiedTick(t_close2, "buy", 1, 0)
    c2 = builder.process_tick(ct_close2)
    
    assert c2 is not None
    assert c2.is_complete
    
    assert c2.bid_absorption is True, "Expected bid absorption"
    assert c2.ask_absorption is True, "Expected ask absorption"
    
    print("All footprint tests passed.")

if __name__ == "__main__":
    test_footprint_builder()
