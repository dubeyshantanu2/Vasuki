import pandas as pd
from config.settings import TradingConfig
from core.market_structure import StructureState, Bias, StructureEvent
from core.volume_profile import VolumeProfile, VolumeProfileNode
from core.delta import DeltaBuilder, DeltaCandle
from core.footprint import FootprintBuilder, FootprintCandle, FootprintLevel
from core.big_trades import BigTradeFilter
from core.signal_engine import SignalEngine

def test_signal_engine():
    config = TradingConfig()
    engine = SignalEngine(trading_config=config)
    
    # Current time in prime window
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    # Replace time with 10:00:00 to ensure it falls within the prime window
    ts = now.replace(hour=10, minute=0, second=0, microsecond=0)
    
    # Mocks
    structure_state = StructureState(
        bias=Bias.BULLISH,
        last_event=StructureEvent.NONE,
        last_swing_high=None,
        last_swing_low=None,
        prev_swing_high=None,
        prev_swing_low=None,
        is_clear=True
    )
    
    nodes = [VolumeProfileNode(price=100.0, volume=100, buy_volume=50, sell_volume=50)]
    session_profile = VolumeProfile(nodes=nodes, poc=100.0, vah=102.0, val=98.0, total_volume=100, value_area_pct=0.7, session_start=ts, session_end=ts)
    prior_day_profile = VolumeProfile(nodes=nodes, poc=90.0, vah=92.0, val=88.0, total_volume=100, value_area_pct=0.7, session_start=ts, session_end=ts)
    
    delta_builder = DeltaBuilder()
    footprint_builder = FootprintBuilder()
    big_trade_filter = BigTradeFilter()
    
    # 1. Test failing gate 1 (neutral bias)
    struct_neutral = StructureState(bias=Bias.NEUTRAL, last_event=StructureEvent.NONE, last_swing_high=None, last_swing_low=None, prev_swing_high=None, prev_swing_low=None, is_clear=True)
    sig1, res1 = engine.evaluate(100.0, ts, struct_neutral, session_profile, prior_day_profile, delta_builder, footprint_builder, big_trade_filter)
    assert sig1 is None
    assert len(res1) == 1
    assert not res1[0].passed
    
    # 2. Test failing gate 2 (not at zone)
    sig2, res2 = engine.evaluate(105.0, ts, structure_state, session_profile, prior_day_profile, delta_builder, footprint_builder, big_trade_filter)
    assert sig2 is None
    assert len(res2) == 2
    assert not res2[1].passed
    
    # 3. Test failing gate 3 (zone-bias alignment: Bullish but at VAH breaking down)
    # At VAH (102.0) and current price (101.99) -> direction long requires price >= VAH
    sig3, res3 = engine.evaluate(101.9, ts, structure_state, session_profile, prior_day_profile, delta_builder, footprint_builder, big_trade_filter)
    assert sig3 is None
    assert not res3[-1].passed
    
    # 4. Pass gate 1, 2, 3 but fail 4 (no confirmations)
    # At VAL (98.0) and Bullish -> Long
    sig4, res4 = engine.evaluate(98.0, ts, structure_state, session_profile, prior_day_profile, delta_builder, footprint_builder, big_trade_filter)
    assert sig4 is None
    assert not res4[-1].passed
    
    # 5. Pass all gates (mocking footprint and big trade)
    # Add a recent footprint candle with bid absorption
    fc = FootprintCandle(interval_start=ts, interval_end=ts, is_complete=True, bid_absorption=True)
    footprint_builder.completed_candles.append(fc)
    
    # Add a dominant buy side in big trades
    from core.big_trades import BigTrade
    bt = BigTrade(timestamp=pd.Timestamp.now(tz="Asia/Kolkata"), price=98.0, quantity=1000, direction="buy", significance="block")
    big_trade_filter.big_trades.append(bt)
    
    sig5, res5 = engine.evaluate(98.0, ts, structure_state, session_profile, prior_day_profile, delta_builder, footprint_builder, big_trade_filter)
    assert sig5 is not None
    assert sig5.direction == "long"
    assert sig5.zone_type == "VAL"
    assert sig5.zone_price == 98.0
    assert sig5.sl_price == 98.0 - config.sl_buffer_points
    assert sig5.confirmation_count == 2
    
    print("All signal engine tests passed.")

if __name__ == "__main__":
    test_signal_engine()
