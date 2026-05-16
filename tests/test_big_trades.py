import pandas as pd
from data.dhan_ws import Tick
from core.delta import ClassifiedTick
from core.big_trades import BigTradeFilter, BigTrade

def test_big_trades():
    db_sink = []
    
    def mock_supabase_callback(bt: BigTrade):
        db_sink.append(bt)

    filter = BigTradeFilter(
        threshold_lots=500,
        block_threshold_lots=1000,
        on_big_trade=mock_supabase_callback
    )
    
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    
    # 1. Old tick (outside 60s)
    t4 = Tick("1", now - pd.Timedelta(seconds=120), 102.0, 800, 101.0) # BUY
    ct4 = ClassifiedTick(t4, "buy", 800, 0)
    filter.process_tick(ct4)
    
    # 2. Normal tick, below threshold
    t1 = Tick("1", now - pd.Timedelta(seconds=50), 100.0, 100, 99.0) # BUY
    ct1 = ClassifiedTick(t1, "buy", 100, 0)
    res1 = filter.process_tick(ct1)
    assert res1 is None
    
    # 3. Large tick
    t2 = Tick("1", now - pd.Timedelta(seconds=40), 100.5, 600, 100.0) # BUY
    ct2 = ClassifiedTick(t2, "buy", 600, 0)
    res2 = filter.process_tick(ct2)
    assert res2 is not None
    assert res2.significance == "large"
    assert res2.direction == "buy"
    
    # 4. Block tick
    t3 = Tick("1", now - pd.Timedelta(seconds=30), 101.0, 1500, 101.5) # SELL
    ct3 = ClassifiedTick(t3, "sell", 0, 1500)
    res3 = filter.process_tick(ct3)
    assert res3 is not None
    assert res3.significance == "block"
    assert res3.direction == "sell"
    
    # Test DB sink
    assert len(db_sink) == 3 # 3 big trades processed
    
    # Test get_recent_big_trades
    recent = filter.get_recent_big_trades(within_seconds=60)
    assert len(recent) == 2 # Only the first two, since t4 is old and the list reverses from the end
    
    # Test price range
    range_trades = filter.get_recent_big_trades(within_seconds=60, price_range=(100.0, 100.8))
    assert len(range_trades) == 1
    assert range_trades[0].quantity == 600
    
    # Test dominant side
    # Recent trades: buy 600, sell 1500. Sell > Buy by 20%+
    dominant = filter.get_dominant_side(within_seconds=60)
    assert dominant == "sell"
    
    # Add a huge buy to flip dominance
    t5 = Tick("1", now - pd.Timedelta(seconds=10), 100.5, 3000, 100.0) # BUY
    ct5 = ClassifiedTick(t5, "buy", 3000, 0)
    filter.process_tick(ct5)
    
    dominant_new = filter.get_dominant_side(within_seconds=60)
    # buy 3600, sell 1500 -> Buy > Sell * 1.2
    assert dominant_new == "buy"
    
    # Filter by price range to isolate the sell block
    dom_range = filter.get_dominant_side(within_seconds=60, price_range=(100.8, 101.5))
    assert dom_range == "sell" # Only the 1500 sell is in this range
    
    filter.reset_session()
    assert len(filter.big_trades) == 0

    print("All big trades tests passed.")

if __name__ == "__main__":
    test_big_trades()
