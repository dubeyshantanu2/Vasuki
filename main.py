import asyncio
import sys
from datetime import datetime, timedelta
import pytz
from loguru import logger

from config.settings import CONFIG
from config.expiry_config import ExpiryManager

from data.dhan_rest import DhanRestClient
from data.dhan_ws import DhanWebSocketClient, Tick
from core.market_structure import MarketStructureEngine
from core.volume_profile import VolumeProfileEngine
from core.delta import DeltaBuilder
from core.footprint import FootprintBuilder
from core.big_trades import BigTradeFilter
from core.signal_engine import SignalEngine
from db.supabase_client import SupabaseClient
from output.discord_client import DiscordClient

class OrderFlowSystem:
    def __init__(self):
        """
        Instantiate all components from CONFIG:
        - DhanRestClient
        - DhanWebSocketClient  
        - MarketStructureEngine
        - VolumeProfileEngine
        - DeltaBuilder
        - FootprintBuilder
        - BigTradeFilter
        - SignalEngine
        - SupabaseClient
        - DiscordClient
        
        Load expiry config via ExpiryManager.
        """
        self.expiry_manager = ExpiryManager(CONFIG.trading)
        self.active_config = self.expiry_manager.apply_to_config(CONFIG.trading)
        
        self.dhan_rest = DhanRestClient(CONFIG.dhan)
        self.dhan_ws = DhanWebSocketClient(CONFIG.dhan)
        self.supabase = SupabaseClient(CONFIG.supabase)
        self.discord = DiscordClient(CONFIG.discord)
        
        self.market_structure = MarketStructureEngine(lookback=self.active_config.swing_lookback)
        self.vp_engine = VolumeProfileEngine(
            bucket_size=self.active_config.bucket_size, 
            value_area_pct=self.active_config.value_area_pct
        )
        self.delta_builder = DeltaBuilder(interval_minutes=int(self.active_config.ltf_interval))
        self.footprint_builder = FootprintBuilder(
            bucket_size=self.active_config.bucket_size,
            interval_minutes=int(self.active_config.ltf_interval)
        )
        self.big_trade_filter = BigTradeFilter(threshold_lots=self.active_config.big_trade_threshold)
        
        self.signal_engine = SignalEngine(
            trading_config=self.active_config, 
            expiry_config=self.expiry_manager.get_config()
        )
        
        self.ist_tz = pytz.timezone("Asia/Kolkata")
        
        # State
        self.structure_state = None
        self.session_profile = None
        self.prior_day_profile = None
        
        self.tick_count = 0
        self.last_signal_times = {} # zone_type -> timestamp
        self.signal_cooldown_mins = 15
        
        self._bg_tasks = []

    async def initialize(self) -> None:
        """
        Run once at startup (after market open check):
        1. Fetch 1H OHLCV for last 5 days → run MarketStructureEngine
        2. Fetch 15m OHLCV for today + yesterday → build session VP + prior day VP
        3. Log and post structure state + VP levels to Discord
        4. Save snapshots to Supabase
        """
        logger.info("Initializing OrderFlowSystem...")
        try:
            now = datetime.now(self.ist_tz)
            today_str = now.strftime("%Y-%m-%d")
            
            # 1. Fetch 1H OHLCV for last 5 days -> run MarketStructureEngine
            start_date_5d = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            htf_df = self.dhan_rest.get_candles(
                security_id=self.active_config.security_id,
                exchange_segment=self.active_config.exchange_segment,
                instrument_type="INDEX",
                interval=self.active_config.htf_interval,
                from_date=start_date_5d,
                to_date=today_str
            )
            self.structure_state = self.market_structure.analyze(htf_df)
            
            # 2. Fetch 15m OHLCV for today + yesterday -> build session VP + prior day VP
            start_date_2d = (now - timedelta(days=3)).strftime("%Y-%m-%d")
            mtf_df = self.dhan_rest.get_candles(
                security_id=self.active_config.security_id,
                exchange_segment=self.active_config.exchange_segment,
                instrument_type="INDEX",
                interval=self.active_config.mtf_interval,
                from_date=start_date_2d,
                to_date=today_str
            )
            
            # Split dataframe into prior day and today
            mtf_df['date'] = mtf_df['timestamp'].dt.date
            dates = sorted(list(set(mtf_df['date'])))
            
            prior_df = None
            today_df = None
            
            if len(dates) >= 2:
                prior_df = mtf_df[mtf_df['date'] == dates[-2]]
                today_df = mtf_df[mtf_df['date'] == dates[-1]]
            elif len(dates) == 1:
                today_df = mtf_df[mtf_df['date'] == dates[-1]]
            
            if prior_df is not None and not prior_df.empty:
                self.prior_day_profile = self.vp_engine.build_from_ohlcv(prior_df)
                
            if today_df is not None and not today_df.empty:
                self.session_profile = self.vp_engine.build_from_ohlcv(today_df)
            else:
                self.session_profile = self.prior_day_profile # Fallback
                
            # 3. Log and post structure state + VP levels to Discord
            msg = f"Structure: {self.structure_state.bias.name} | "
            if self.session_profile:
                msg += f"VP POC: {self.session_profile.poc}"
            logger.info(msg)
            asyncio.create_task(self.discord.send_system_status("started", msg))
            
            # 4. Save snapshots to Supabase
            if self.structure_state:
                self.supabase.save_market_structure(self.active_config.symbol, self.structure_state)
            if self.prior_day_profile:
                self.supabase.save_volume_profile(self.active_config.symbol, self.prior_day_profile, "prior_day")
            if self.session_profile:
                self.supabase.save_volume_profile(self.active_config.symbol, self.session_profile, "session")
                
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            raise

    def on_tick(self, tick: Tick) -> None:
        """
        Called by WebSocket client for every incoming tick.
        
        1. delta_builder.process_tick(tick) → check for completed DeltaCandle
        2. classified = delta_builder.classify_tick(tick)
        3. footprint_builder.process_tick(classified) → check for completed FootprintCandle
        4. big_trade = big_trade_filter.process_tick(classified)
           if big_trade and significance == "block": discord.send_big_trade_alert()
        
        Every N ticks (N=10 — configurable):
          5. Run signal_engine.evaluate() with current state
          6. If signal: discord.send_signal() + supabase.save_signal()
          7. Log gate results
        
        Every completed DeltaCandle:
          8. Re-run market structure (on 1H candle completion only)
          9. Save delta_candle to Supabase
        """
        self.tick_count += 1
        
        # 1. Delta
        completed_delta = self.delta_builder.process_tick(tick)
        
        # 2. Classify
        classified = self.delta_builder.classify_tick(tick)
        
        # 3. Footprint
        self.footprint_builder.process_tick(classified)
        
        # 4. Big Trade
        big_trade = self.big_trade_filter.process_tick(classified)
        if big_trade and big_trade.significance == "block":
            asyncio.create_task(self.discord.send_big_trade_alert(big_trade, self.active_config.symbol))
            # SupabaseClient fire_and_forget handles the executor logic natively
            self.supabase.save_big_trade(self.active_config.symbol, big_trade)
            
        # 5. Evaluate Signal Every 10 Ticks
        if self.tick_count % 10 == 0:
            signal_result = self.signal_engine.evaluate(
                current_price=tick.ltp,
                timestamp=tick.timestamp,
                structure_state=self.structure_state,
                session_profile=self.session_profile,
                prior_day_profile=self.prior_day_profile,
                delta_builder=self.delta_builder,
                footprint_builder=self.footprint_builder,
                big_trade_filter=self.big_trade_filter
            )
            
            if signal_result:
                signal, results = signal_result
                if signal:
                    # Check Cooldown
                    last_time = self.last_signal_times.get(signal.zone_type)
                    if not last_time or (tick.timestamp - last_time).total_seconds() / 60 > self.signal_cooldown_mins:
                        self.last_signal_times[signal.zone_type] = tick.timestamp
                        
                        # 6. Send/Save Signal
                        asyncio.create_task(self.discord.send_signal(signal))
                        self.supabase.save_signal(signal)
                        
                        # 7. Log Gate Results (already logged in engine, but can send failure logs etc if needed)
                else:
                    # If signal is None, results contains gate failures
                    asyncio.create_task(self.discord.send_gate_failure(results, tick.ltp, tick.timestamp))

        # 8-9. Every completed DeltaCandle
        if completed_delta:
            # 9. Save delta_candle
            self.supabase.save_delta_candle(self.active_config.symbol, completed_delta)
            
            # 8. Re-run market structure (on 1H candle completion only)
            if completed_delta.interval_end.minute == 0:
                asyncio.create_task(self._refresh_htf_structure())
                
            # Trigger VP refresh every 15 mins (aligned with candle closes)
            if completed_delta.interval_end.minute % 15 == 0:
                asyncio.create_task(self._refresh_volume_profile())

    async def _refresh_htf_structure(self) -> None:
        """
        Re-fetch 1H data and re-run market structure.
        Called when a 1H candle completes.
        """
        logger.info("Refreshing HTF Structure...")
        try:
            now = datetime.now(self.ist_tz)
            start_date_5d = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            today_str = now.strftime("%Y-%m-%d")
            
            htf_df = self.dhan_rest.get_candles(
                security_id=self.active_config.security_id,
                exchange_segment=self.active_config.exchange_segment,
                instrument_type="INDEX",
                interval=self.active_config.htf_interval,
                from_date=start_date_5d,
                to_date=today_str
            )
            self.structure_state = self.market_structure.analyze(htf_df)
            self.supabase.save_market_structure(self.active_config.symbol, self.structure_state)
            logger.info(f"HTF Structure updated: {self.structure_state.bias.name}")
        except Exception as e:
            logger.error(f"Failed to refresh HTF structure: {e}")

    async def _refresh_volume_profile(self) -> None:
        """
        Re-build session VP from today's 15m data.
        Called every 15 minutes.
        """
        logger.info("Refreshing Session Volume Profile...")
        try:
            now = datetime.now(self.ist_tz)
            today_str = now.strftime("%Y-%m-%d")
            
            mtf_df = self.dhan_rest.get_candles(
                security_id=self.active_config.security_id,
                exchange_segment=self.active_config.exchange_segment,
                instrument_type="INDEX",
                interval=self.active_config.mtf_interval,
                from_date=today_str,
                to_date=today_str
            )
            if not mtf_df.empty:
                self.session_profile = self.vp_engine.build_from_ohlcv(mtf_df)
                self.supabase.save_volume_profile(self.active_config.symbol, self.session_profile, "session")
                logger.info(f"Session VP updated: POC={self.session_profile.poc}")
        except Exception as e:
            logger.error(f"Failed to refresh Volume Profile: {e}")

    async def _heartbeat(self) -> None:
        """Log a heartbeat every 5 minutes."""
        while True:
            await asyncio.sleep(5 * 60)
            logger.info("System alive — processing ticks")

    async def _vp_refresh_loop(self) -> None:
        """Background task for VP refresh, just in case candle ticks stall."""
        while True:
            await asyncio.sleep(15 * 60)
            await self._refresh_volume_profile()

    async def _htf_refresh_loop(self) -> None:
        """Background task for HTF refresh, just in case candle ticks stall."""
        while True:
            await asyncio.sleep(60 * 60)
            await self._refresh_htf_structure()

    async def run(self) -> None:
        """
        Main entry point.
        
        10. Check if within market hours (9:00–15:35 IST). If not, wait.
        11. await self.initialize()
        12. Register self.on_tick with WebSocket client
        13. await discord.send_system_status("started", ...)
        14. await dhan_ws.connect([NIFTY instrument])
           (this blocks until disconnected)
        15. On KeyboardInterrupt: graceful shutdown
        """
        logger.info("Starting OrderFlowSystem...")
        
        # 10. Check market hours
        while True:
            now = datetime.now(self.ist_tz)
            # Market hours 9:00 - 15:35 IST
            start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
            end_time = now.replace(hour=15, minute=35, second=0, microsecond=0)
            
            if start_time <= now <= end_time:
                break
                
            logger.info("Outside market hours (9:00-15:35 IST). Waiting for market open...")
            await asyncio.sleep(60)

        # 11. Initialize
        await self.initialize()
        
        # 12. Register tick handler
        self.dhan_ws.register_tick_handler(self.on_tick)
        
        # 13. System status
        await self.discord.send_system_status("started", "OrderFlowSystem is now live.")
        
        # Background tasks
        self._bg_tasks.append(asyncio.create_task(self._heartbeat()))
        self._bg_tasks.append(asyncio.create_task(self._vp_refresh_loop()))
        self._bg_tasks.append(asyncio.create_task(self._htf_refresh_loop()))
        
        # 14. Connect (blocks)
        instruments = [{
            "security_id": self.active_config.security_id, 
            "exchange_segment": self.active_config.exchange_segment
        }]
        await self.dhan_ws.connect(instruments)

    async def shutdown(self) -> None:
        """
        Graceful shutdown:
        16. await dhan_ws.disconnect()
        17. Post system status "stopped" to Discord
        18. Log final session summary
        """
        logger.info("Initiating graceful shutdown...")
        for task in self._bg_tasks:
            task.cancel()
            
        await self.dhan_ws.disconnect()
        await self.discord.send_system_status("stopped", f"System stopped gracefully. Processed {self.tick_count} ticks.")
        logger.info(f"Session summary: Processed {self.tick_count} total ticks.")

if __name__ == "__main__":
    system = OrderFlowSystem()
    try:
        asyncio.run(system.run())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received.")
        asyncio.run(system.shutdown())
        sys.exit(0)
