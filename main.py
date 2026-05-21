import asyncio
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import pytz
from loguru import logger
from aiohttp import web

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
        
        self.market_structure = MarketStructureEngine(
            lookback=self.active_config.swing_lookback,
            gap_threshold_pct=self.active_config.gap_threshold_pct,
            atr_multiplier_medium=self.active_config.atr_multiplier_medium,
            atr_multiplier_high=self.active_config.atr_multiplier_high,
            atr_lookback_candles=self.active_config.atr_lookback_candles
        )
        self.vp_engine = VolumeProfileEngine(
            bucket_size=self.active_config.bucket_size, 
            value_area_pct=self.active_config.value_area_pct,
            flat_profile_threshold_pct=self.active_config.flat_profile_threshold_pct
        )
        self.delta_builder = DeltaBuilder(
            interval_minutes=int(self.active_config.ltf_interval),
            outlier_cap_multiplier=self.active_config.outlier_cap_multiplier
        )
        self.footprint_builder = FootprintBuilder(
            bucket_size=self.active_config.bucket_size,
            interval_minutes=int(self.active_config.ltf_interval)
        )
        self.big_trade_filter = BigTradeFilter(threshold_lots=self.active_config.big_trade_threshold)
        self.big_trade_filter.set_expiry_mode(self.expiry_manager.get_config().is_expiry_day)
        
        self.signal_engine = SignalEngine(
            trading_config=self.active_config, 
            expiry_config=self.expiry_manager.get_config()
        )
        
        self.ist_tz = pytz.timezone("Asia/Kolkata")
        
        # State
        self.structure_state = None
        self.session_profile = None
        self.prior_day_profile = None
        self.last_gap_alert_date = None
        
        self.tick_count = 0
        self.last_signal_times = {} # zone_type -> timestamp
        self.signal_cooldown_mins = 15
        self.active_signals = {}
        self.current_price = 0.0
        
        # Variables for volatility spike detection
        self.recent_candle_ranges: list[float] = []
        self.current_high: float = 0.0
        self.current_low: float = float('inf')
        self.current_candle_start: Optional[pd.Timestamp] = None
        
        # Data Health Monitoring
        self._last_tick_time: Optional[pd.Timestamp] = None
        self._tick_count_window: int = 0
        self._window_start: Optional[pd.Timestamp] = None
        self._market_data_healthy: bool = True
        self._signals_paused: bool = False
        self._gap_paused: bool = False
        
        # REST API Rate Limiting
        self.MAX_CALLS_PER_MINUTE: int = 10
        self._api_call_times: deque = deque(maxlen=self.MAX_CALLS_PER_MINUTE)
        self._api_rate_lock = asyncio.Lock()
        self._api_waiting_calls: int = 0
        
        self._bg_tasks = []

    async def _wait_for_api_slot(self) -> None:
        self._api_waiting_calls += 1
        if self._api_waiting_calls > 3:
            logger.warning(f"REST call queue backed up: {self._api_waiting_calls} calls waiting")
            
        async with self._api_rate_lock:
            now = time.monotonic()
            if len(self._api_call_times) == self.MAX_CALLS_PER_MINUTE:
                oldest = self._api_call_times[0]
                wait = 60.0 - (now - oldest)
                if wait > 0:
                    logger.debug(f"Rate limiting REST calls: waiting {wait:.1f}s")
                    await asyncio.sleep(wait)
            self._api_call_times.append(time.monotonic())
        self._api_waiting_calls -= 1

    def _check_trading_day(self) -> bool:
        """
        Cross-reference today's date against ExpiryManager._is_exchange_holiday().
        If today is a holiday:
            Log: "Today is an NSE holiday — system will not start"
            Send Discord: "📅 NSE Holiday — VASUKI not active today"
            Return False
        Return True
        """
        now = datetime.now(self.ist_tz)
        if self.expiry_manager._is_exchange_holiday(now):
            logger.info("Today is an NSE holiday — system will not start")
            asyncio.create_task(self.discord.send_system_status("warning", "📅 NSE Holiday — VASUKI not active today"))
            return False
        return True

    def _on_circuit_breaker(self) -> None:
        """
        Called when circuit breaker is suspected.
        1. Pause signal evaluation (set self._signals_paused = True)
        2. Do NOT reset DeltaBuilder, FootprintBuilder, or BigTradeFilter
        3. Send Discord: "⏸ Circuit breaker suspected — trading paused"
        4. Log current session state summary
        """
        self._signals_paused = True
        msg = "⏸ Circuit breaker suspected — trading paused"
        logger.warning(msg)
        asyncio.create_task(self.discord.send_system_status("warning", msg))
        if self.session_profile:
            logger.info(f"Circuit Breaker paused at Session POC: {self.session_profile.poc}, Total Volume: {self.session_profile.total_volume:.2f}")

    def _on_circuit_breaker_resume(self) -> None:
        """
        Called when ticks resume after circuit breaker.
        1. Resume signal evaluation (self._signals_paused = False)
        2. Trigger VP rebuild immediately
        3. Send Discord: "▶ Trading resumed — VP refreshed"
        """
        self._signals_paused = False
        asyncio.create_task(self._refresh_volume_profile())
        msg = "▶ Trading resumed — VP refreshed"
        logger.info(msg)
        asyncio.create_task(self.discord.send_system_status("started", msg))

    async def _monitor_data_health(self) -> None:
        """
        Background task. Runs every 60 seconds after 9:15 IST.
        Checks for zero ticks and low tick rates.
        """
        while True:
            await asyncio.sleep(60)
            now = pd.Timestamp.now(tz="Asia/Kolkata")
            
            # Start monitoring after 09:25 IST
            monitor_start = now.replace(hour=9, minute=25, second=0, microsecond=0)
            if now < monitor_start:
                continue
                
            # Market hours check (stop monitoring after 15:35)
            monitor_end = now.replace(hour=15, minute=35, second=0, microsecond=0)
            if now > monitor_end:
                continue

            no_data_limit = self.active_config.no_data_alert_minutes * 60
            last_tick_secs = (now - self._last_tick_time).total_seconds() if self._last_tick_time else float('inf')
            
            # Check 1 - Zero tick detection
            if self._last_tick_time is None or last_tick_secs > no_data_limit:
                if self._market_data_healthy:
                    msg = "🔴 No market data received. Possible holiday or NSE outage."
                    logger.error(msg)
                    asyncio.create_task(self.discord.send_system_status("error", msg))
                    self._market_data_healthy = False
            
            # Check 3 - Recovery
            elif not self._market_data_healthy:
                msg = "🟢 Market data restored"
                logger.info(msg)
                asyncio.create_task(self.discord.send_system_status("started", msg))
                self._market_data_healthy = True
            
            # Check 2 - Low tick rate
            if self._window_start is None:
                self._window_start = now
                self._tick_count_window = 0
            elif (now - self._window_start).total_seconds() >= 300: # 5 minutes
                if self._tick_count_window < self.active_config.heartbeat_tick_minimum:
                    logger.warning(f"Low tick rate: {self._tick_count_window} ticks in last 5 min")
                self._window_start = now
                self._tick_count_window = 0

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
            await self.supabase.sync_fallback_records(discord_client=self.discord)
            now = datetime.now(self.ist_tz)
            today_str = now.strftime("%Y-%m-%d")
            
            # 0. Compute 60-day Baseline ATR for adaptive lookback
            start_date_90d = (now - timedelta(days=90)).strftime("%Y-%m-%d")
            try:
                await self._wait_for_api_slot()
                daily_df = self.dhan_rest.get_candles(
                    security_id=self.active_config.security_id,
                    exchange_segment=self.active_config.exchange_segment,
                    instrument_type=self.active_config.instrument_type,
                    interval="D",
                    from_date=start_date_90d,
                    to_date=today_str
                )
                if not daily_df.empty and len(daily_df) > 10:
                    self.market_structure._baseline_atr = float((daily_df['high'] - daily_df['low']).mean())
                    logger.info(f"Baseline ATR computed: {self.market_structure._baseline_atr:.2f}")
                else:
                    logger.warning("Insufficient daily data for ATR calculation.")
                    
                # 0.1 Compute 20-day Average Daily Volume for Low Conviction Profile
                if not daily_df.empty and len(daily_df) > 0:
                    recent_daily = daily_df.iloc[-20:]
                    if len(recent_daily) < 10:
                        logger.warning(f"Only {len(recent_daily)} days available for volume baseline.")
                    avg_daily_vol = float(recent_daily['volume'].mean())
                    self.vp_engine.set_baseline_volume(avg_daily_vol)
                    logger.info(f"Baseline Average Daily Volume computed: {avg_daily_vol:.2f}")
            except Exception as e:
                logger.error(f"Failed to fetch daily data for ATR and Volume: {e}")
            
            # 1. Fetch 1H OHLCV for last 5 days -> run MarketStructureEngine
            start_date_5d = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            await self._wait_for_api_slot()
            htf_df = self.dhan_rest.get_candles(
                security_id=self.active_config.security_id,
                exchange_segment=self.active_config.exchange_segment,
                instrument_type=self.active_config.instrument_type,
                interval=self.active_config.htf_interval,
                from_date=start_date_5d,
                to_date=today_str
            )
            self.structure_state = self.market_structure.analyze(htf_df)
            
            gap_info = self.market_structure.detect_gap_open(htf_df)
            expiry_config = self.expiry_manager.get_config()
            
            if expiry_config.is_expiry_day and gap_info:
                expiry_config = self.expiry_manager.apply_gap_override(expiry_config, gap_info)
                if expiry_config.gap_detected:
                    logger.warning(f"Expiry + gap open: effective window = {expiry_config.effective_window}")
                    asyncio.create_task(self.discord.send_system_status("warning",
                        f"Expiry day + {gap_info['type']}: window adjusted to {expiry_config.effective_window}"
                    ))
                self.signal_engine.expiry_config = expiry_config
            
            # 2. Fetch 15m OHLCV for today + yesterday -> build session VP + prior day VP
            start_date_2d = (now - timedelta(days=3)).strftime("%Y-%m-%d")
            await self._wait_for_api_slot()
            mtf_df = self.dhan_rest.get_candles(
                security_id=self.active_config.security_id,
                exchange_segment=self.active_config.exchange_segment,
                instrument_type=self.active_config.instrument_type,
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
            
            if prior_df is not None and len(prior_df) >= 10:
                try:
                    self.prior_day_profile = self.vp_engine.build(prior_df)
                except Exception as e:
                    logger.warning(f"Failed to build prior day profile: {e}")
                    
            if today_df is not None and len(today_df) >= 10:
                try:
                    self.session_profile = self.vp_engine.build(today_df)
                except Exception as e:
                    logger.warning(f"Failed to build session profile: {e}")
                    self.session_profile = self.prior_day_profile
            else:
                logger.info("Not enough data for today's session profile yet. Using prior day profile as fallback.")
                self.session_profile = self.prior_day_profile # Fallback
                
            # 3. Log and post structure state + VP levels to Discord
            msg = f"Structure: {self.structure_state.bias.name} | "
            if self.session_profile:
                msg += f"VP POC: {self.session_profile.poc}"
            logger.info(msg)
            asyncio.create_task(self.discord.send_system_status("started", msg))
            
            # 4. Save snapshots to Supabase
            if self.structure_state:
                asyncio.create_task(self.supabase.save_market_structure(self.active_config.symbol, self.structure_state))
                self._check_gap_alert()
            if self.prior_day_profile:
                asyncio.create_task(self.supabase.save_volume_profile(self.active_config.symbol, self.prior_day_profile, "prior_day"))
            if self.session_profile:
                asyncio.create_task(self.supabase.save_volume_profile(self.active_config.symbol, self.session_profile, "session"))
                
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            raise

    def on_feed_gap(self, start: pd.Timestamp, end: pd.Timestamp) -> None:
        """
        Called by WebSocket client when a gap of >5s is detected upon reconnection.
        Marks the current DeltaCandle as incomplete (has_feed_gap).
        """
        if self.delta_builder and self.delta_builder.current_candle:
            self.delta_builder.current_candle.has_feed_gap = True
            logger.warning(f"Feed gap detected from {start} to {end} — marking candle incomplete")

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
        self.current_price = tick.ltp
        
        # Data Health Monitoring
        self._last_tick_time = tick.timestamp
        self._tick_count_window += 1
        
        # Track 5m candle range for spike detection
        candle_start = tick.timestamp.floor('5min')
        if self.current_candle_start is None:
            self.current_candle_start = candle_start
        elif candle_start != self.current_candle_start:
            if self.current_high > 0 and self.current_low != float('inf'):
                self.recent_candle_ranges.append(self.current_high - self.current_low)
            self.current_high = 0.0
            self.current_low = float('inf')
            self.current_candle_start = candle_start
            
        self.current_high = max(self.current_high, tick.ltp)
        self.current_low = min(self.current_low, tick.ltp)
        
        # 1. Delta
        completed_delta = self.delta_builder.process_tick(tick)
        
        # 2. Classify
        classified = self.delta_builder.classify_tick(tick)
        
        if classified is not None:
            # 3. Footprint
            self.footprint_builder.process_tick(classified)
            
            # 4. Big Trade
            big_trade = self.big_trade_filter.process_tick(classified)
            if big_trade:
                if big_trade.is_outlier:
                    asyncio.create_task(self.discord.send_system_status("warning", f"🚨 Outlier trade: {big_trade.quantity} lots — suspected rollover, excluded from signal"))
                elif big_trade.significance == "block":
                    asyncio.create_task(self.discord.send_big_trade_alert(big_trade, self.active_config.symbol))
                
                # SupabaseClient fire_and_forget handles the executor logic natively
                asyncio.create_task(self.supabase.save_big_trade(self.active_config.symbol, big_trade))
            
        if self._signals_paused or self._gap_paused:
            # Still process ticks (build delta/footprint) but don't evaluate signals
            # This preserves state while the market is restabilizing
            pass
        # 5. Evaluate Signal Every 10 Ticks
        elif self.tick_count % 10 == 0:
            signal_result = self.signal_engine.evaluate(
                current_price=tick.ltp,
                timestamp=tick.timestamp,
                current_high=self.current_high,
                current_low=self.current_low,
                recent_candle_ranges=self.recent_candle_ranges,
                structure_state=self.structure_state,
                session_profile=self.session_profile,
                prior_day_profile=self.prior_day_profile,
                delta_builder=self.delta_builder,
                footprint_builder=self.footprint_builder,
                big_trade_filter=self.big_trade_filter,
                discord_client=self.discord,
                supabase_client=self.supabase
            )
            
            if self.signal_engine.needs_vp_refresh:
                asyncio.create_task(self._refresh_volume_profile())
                self.signal_engine.needs_vp_refresh = False
            
            if signal_result:
                signal, results = signal_result
                if signal:
                    # Check Cooldown
                    last_time = self.last_signal_times.get(signal.zone_type)
                    if not last_time or (tick.timestamp - last_time).total_seconds() / 60 > self.signal_cooldown_mins:
                        self.last_signal_times[signal.zone_type] = tick.timestamp
                        
                        # 6. Send/Save Signal
                        asyncio.create_task(self.discord.send_signal(signal))
                        asyncio.create_task(self.supabase.save_signal(signal))
                        
                        # 7. Log Gate Results (already logged in engine, but can send failure logs etc if needed)
                else:
                    # If signal is None, results contains gate failures
                    asyncio.create_task(self.discord.send_gate_failure(results, tick.ltp, tick.timestamp))

        # 8-9. Every completed DeltaCandle
        if completed_delta:
            # 9. Save delta_candle
            asyncio.create_task(self.supabase.save_delta_candle(self.active_config.symbol, completed_delta))

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
            
            await self._wait_for_api_slot()
            htf_df = self.dhan_rest.get_candles(
                security_id=self.active_config.security_id,
                exchange_segment=self.active_config.exchange_segment,
                instrument_type=self.active_config.instrument_type,
                interval=self.active_config.htf_interval,
                from_date=start_date_5d,
                to_date=today_str
            )
            self.structure_state = self.market_structure.analyze(htf_df)
            asyncio.create_task(self.supabase.save_market_structure(self.active_config.symbol, self.structure_state))
            logger.info(f"HTF Structure updated: {self.structure_state.bias.name}")
            self._check_gap_alert()
        except Exception as e:
            logger.error(f"Failed to refresh HTF structure: {e}")

    def _check_gap_alert(self):
        """Check if we need to send a gap alert"""
        if not self.structure_state or not getattr(self.structure_state, 'gap_info', None):
            if self._gap_paused:
                self._gap_paused = False
                logger.info("Gap settled — resuming signals")
                asyncio.create_task(self.discord.send_system_status("started", "▶ Gap settled — signals resumed"))
            return
            
        if self.structure_state.last_event.value == "gap_open":
            self._gap_paused = True
            now = datetime.now(self.ist_tz).date()
            if self.last_gap_alert_date != now:
                gap_info = self.structure_state.gap_info
                msg = f"⚠ Gap open {gap_info['type']}: {gap_info['gap_points']:.0f} pts — signals paused"
                asyncio.create_task(self.discord.send_system_status("error", msg))
                self.last_gap_alert_date = now
        elif self._gap_paused:
            self._gap_paused = False
            logger.info("Gap settled — resuming signals")
            asyncio.create_task(self.discord.send_system_status("started", "▶ Gap settled — signals resumed"))

    async def _refresh_volume_profile(self) -> None:
        """
        Re-build session VP from today's 15m data.
        Called every 15 minutes.
        """
        logger.info("Refreshing Session Volume Profile...")
        try:
            now = datetime.now(self.ist_tz)
            today_str = now.strftime("%Y-%m-%d")
            
            await self._wait_for_api_slot()
            mtf_df = self.dhan_rest.get_candles(
                security_id=self.active_config.security_id,
                exchange_segment=self.active_config.exchange_segment,
                instrument_type=self.active_config.instrument_type,
                interval=self.active_config.mtf_interval,
                from_date=today_str,
                to_date=today_str
            )
            if not mtf_df.empty:
                self.session_profile = self.vp_engine.build(mtf_df)
                asyncio.create_task(self.supabase.save_volume_profile(self.active_config.symbol, self.session_profile, "session"))
                logger.info(f"Session VP updated: POC={self.session_profile.poc}")
                
                # Check active signals for invalidation
                if self.current_price > 0:
                    invalidated_ids = []
                    for sig_id, signal in self.active_signals.items():
                        valid, reason = self.signal_engine.is_signal_still_valid(
                            signal, self.session_profile, self.current_price
                        )
                        if not valid:
                            asyncio.create_task(self.discord.send_signal_invalidation(signal, reason))
                            asyncio.create_task(self.supabase.mark_signal_invalidated(signal.id, reason))
                            invalidated_ids.append(sig_id)
                            
                    for sig_id in invalidated_ids:
                        del self.active_signals[sig_id]
        except Exception as e:
            logger.error(f"Failed to refresh Volume Profile: {e}")

    async def _heartbeat(self) -> None:
        """Log a heartbeat every 5 minutes."""
        while True:
            await asyncio.sleep(5 * 60)
            cooldown_state = [(c.zone_type, c.direction, c.t1_reached) for c in self.signal_engine._cooldowns]
            logger.info(f"System alive — processing ticks. Cooldowns: {cooldown_state}")

    async def _schedule_vp_refresh(self) -> None:
        """
        Calculate next offset from now.
        Sleep until then. Call _refresh_volume_profile().
        Repeat indefinitely.
        """
        offset_mins = self.active_config.vp_refresh_offset_mins
        while True:
            try:
                now = datetime.now(self.ist_tz)
                current_interval = (now.minute // 15) * 15
                candidate_fire = now.replace(minute=current_interval, second=0, microsecond=0) + timedelta(minutes=offset_mins)
                
                if candidate_fire <= now:
                    candidate_fire += timedelta(minutes=15)
                
                sleep_seconds = (candidate_fire - now).total_seconds()
                await asyncio.sleep(sleep_seconds)
                await self._refresh_volume_profile()
            except Exception as e:
                logger.error(f"VP refresh task crashed: {e}. Restarting in 5s.")
                await asyncio.sleep(5)

    async def _schedule_structure_refresh(self) -> None:
        """Same pattern but for 60-minute boundary."""
        offset_mins = self.active_config.structure_refresh_offset_mins
        while True:
            try:
                now = datetime.now(self.ist_tz)
                candidate_fire = now.replace(minute=offset_mins, second=0, microsecond=0)
                
                if candidate_fire <= now:
                    candidate_fire += timedelta(hours=1)
                
                sleep_seconds = (candidate_fire - now).total_seconds()
                await asyncio.sleep(sleep_seconds)
                await self._refresh_htf_structure()
            except Exception as e:
                logger.error(f"Structure refresh task crashed: {e}. Restarting in 5s.")
                await asyncio.sleep(5)

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
        await self.discord.send_system_status("started", "OrderFlowSystem container started and initializing.")
        
        if not self._check_trading_day():
            logger.info("Holiday detected. Sleeping to keep container alive for health checks...")
            while True:
                await asyncio.sleep(3600)
            return
        
        # 10. Check market hours
        while True:
            now = datetime.now(self.ist_tz)
            # Market hours 9:15 - 15:35 IST
            start_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
            end_time = now.replace(hour=15, minute=35, second=0, microsecond=0)
            
            if start_time <= now <= end_time:
                break
                
            logger.info("Outside market hours (9:00-15:35 IST). Waiting for market open...")
            await asyncio.sleep(60)

        # 11. Initialize
        await self.initialize()

        # 12. Register tick handler
        self.dhan_ws.register_tick_handler(self.on_tick)
        self.dhan_ws.register_gap_handler(self.on_feed_gap)
        self.dhan_ws.register_circuit_breaker_handler(self._on_circuit_breaker)
        self.dhan_ws.register_circuit_breaker_resume_handler(self._on_circuit_breaker_resume)

        # 13. System status
        await self.discord.send_system_status("started", "OrderFlowSystem is now live.")
        
        # Background tasks
        self._bg_tasks.append(asyncio.create_task(self._heartbeat()))
        self._bg_tasks.append(asyncio.create_task(self._monitor_data_health()))
        self._bg_tasks.append(asyncio.create_task(self._schedule_vp_refresh()))
        self._bg_tasks.append(asyncio.create_task(self._schedule_structure_refresh()))
        
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

async def start_health_check():
    """Simple health check server for Fly.io."""
    async def handle(request):
        return web.Response(text="OK")
    
    app = web.Application()
    app.add_routes([web.get('/', handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Health check server started on port 8080")

if __name__ == "__main__":
    async def main_entry():
        # Start health check immediately
        try:
            await start_health_check()
        except Exception as e:
            print(f"Failed to start health check server: {e}")

        system = None
        try:
            system = OrderFlowSystem()
            await system.run()
        except asyncio.CancelledError:
            logger.info("Main task cancelled (likely due to shutdown).")
            if system:
                await system.shutdown()
            raise
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received.")
            if system:
                await system.shutdown()
        except Exception as e:
            logger.exception(f"System crashed: {e}")
            # Keep alive so we can see logs and pass health checks
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    break

    try:
        asyncio.run(main_entry())
    except KeyboardInterrupt:
        logger.info("Process stopped by KeyboardInterrupt.")
