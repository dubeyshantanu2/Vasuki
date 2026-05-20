import asyncio
import struct
import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
import pandas as pd
import pytz
import websockets
from loguru import logger
from threading import Lock

from config.settings import DhanConfig, CONFIG
from datetime import datetime
import time as builtin_time


@dataclass
class Tick:
    security_id: str
    timestamp: pd.Timestamp    # IST, timezone-aware
    ltp: float                 # Last Traded Price
    ltq: int                   # Last Traded Quantity (in lots)
    prev_ltp: float            # Previous LTP for uptick rule


class DhanWebSocketClient:
    """
    Streams real-time tick data from Dhan Live Market Feed.
    Calls registered handlers for each tick received.
    Handles reconnection automatically.
    """

    def __init__(self, config: DhanConfig):
        self.config = config
        self.handlers: List[Callable[[Tick], None]] = []
        self.session_start_handlers: List[Callable[[], None]] = []
        self.gap_handlers: List[Callable[[pd.Timestamp, pd.Timestamp], None]] = []
        self._circuit_breaker_handlers: List[Callable[[], None]] = []
        self._circuit_breaker_resume_handlers: List[Callable[[], None]] = []
        self.handlers_lock = Lock()
        self.prev_ltps: Dict[str, float] = {}
        self.ist_tz = pytz.timezone("Asia/Kolkata")
        self.websocket = None
        self._running = False
        self._reconnect_wait = 0
        self._max_reconnect_wait = 30
        
        self._suppressed_ticks_count = 0
        self._last_suppressed_log_time = 0
        self._session_start_emitted_for_date = None
        self._last_tick_timestamp: Optional[pd.Timestamp] = None
        self._just_reconnected = False
        
        self._silence_start: Optional[datetime] = None
        self._circuit_breaker_suspected: bool = False
        
        # Dhan V2 exchange segment strings mapping
        self._segment_map = {
            "NSE_EQ": "NSE_EQ",
            "NSE_FNO": "NSE_FNO",
            "NSE_CURR": "NSE_CURRENCY",
            "BSE_EQ": "BSE_EQ",
            "MCX_COMM": "MCX_COMM",
            "BSE_CURR": "BSE_CURRENCY",
            "BSE_FNO": "BSE_FNO",
            "IDX_I": "IDX_I"
        }

    def register_tick_handler(self, handler: Callable[[Tick], None]) -> None:
        """Register a callback to receive every tick."""
        with self.handlers_lock:
            if handler not in self.handlers:
                self.handlers.append(handler)

    def register_session_start_handler(self, handler: Callable[[], None]) -> None:
        """Register a callback to be called once when clock crosses 09:15."""
        with self.handlers_lock:
            if handler not in self.session_start_handlers:
                self.session_start_handlers.append(handler)

    def register_circuit_breaker_handler(self, handler: Callable[[], None]) -> None:
        with self.handlers_lock:
            if handler not in self._circuit_breaker_handlers:
                self._circuit_breaker_handlers.append(handler)

    def register_circuit_breaker_resume_handler(self, handler: Callable[[], None]) -> None:
        with self.handlers_lock:
            if handler not in self._circuit_breaker_resume_handlers:
                self._circuit_breaker_resume_handlers.append(handler)

    async def _monitor_silence(self) -> None:
        while self._running:
            await asyncio.sleep(30)
            if self._last_tick_timestamp is None:
                continue
            
            try:
                now = datetime.now(self.ist_tz)
                last_tick = self._last_tick_timestamp.astimezone(self.ist_tz)
            except Exception:
                now = datetime.now(self.ist_tz)
                last_tick = self._last_tick_timestamp.tz_convert(self.ist_tz)

            silence = (now - last_tick).total_seconds()
            
            if silence > CONFIG.trading.circuit_breaker_silence_threshold_seconds:
                if not self._circuit_breaker_suspected:
                    self._circuit_breaker_suspected = True
                    self._silence_start = now
                    logger.warning(f"No ticks for {silence:.0f}s — circuit breaker suspected")
                    with self.handlers_lock:
                        cb_handlers_to_call = list(self._circuit_breaker_handlers)
                    for handler in cb_handlers_to_call:
                        try:
                            handler()
                        except Exception as e:
                            logger.error(f"Error in circuit breaker handler: {e}")
            else:
                if self._circuit_breaker_suspected:
                    self._circuit_breaker_suspected = False
                    duration = (now - self._silence_start).total_seconds() / 60 if self._silence_start else 0
                    logger.info(f"Ticks resumed — circuit breaker lifted after {duration:.1f} minutes")
                    self._silence_start = None
                    with self.handlers_lock:
                        resume_handlers_to_call = list(self._circuit_breaker_resume_handlers)
                    for handler in resume_handlers_to_call:
                        try:
                            handler()
                        except Exception as e:
                            logger.error(f"Error in circuit breaker resume handler: {e}")

    async def connect(self, instruments: list[dict]) -> None:
        """
        Connect to Dhan WebSocket and start streaming.
        instruments: [{"security_id": "13", "exchange_segment": "IDX_I"}]
        Runs indefinitely. Reconnects on disconnect with exponential backoff.
        Max reconnect wait: 30 seconds.
        """
        self._running = True
        self._reconnect_wait = 0
        
        asyncio.create_task(self._monitor_silence())

        while self._running:
            try:
                if self._circuit_breaker_suspected:
                    logger.info("Not reconnecting — circuit breaker suspected, not a disconnect")
                    await asyncio.sleep(5)
                    continue

                if self._reconnect_wait > 0:
                    logger.info(f"Reconnecting in {self._reconnect_wait}s...")
                    await asyncio.sleep(self._reconnect_wait)
                    self._reconnect_wait = min(self._reconnect_wait * 2, self._max_reconnect_wait)
                
                # Dhan V2 uses query params for authentication
                url = (
                    f"wss://api-feed.dhan.co?version=2"
                    f"&token={self.config.access_token}"
                    f"&clientId={self.config.client_id}"
                    f"&authType=2"
                )
                
                async with websockets.connect(url) as ws:
                    self.websocket = ws
                    logger.info("Connected to Dhan Live Market Feed")
                    
                    # Reset backoff on successful connection
                    # Note: We set it to 0 so the first reconnection after a drop is immediate.
                    self._reconnect_wait = 0
                    
                    # Subscribe to Quote data (RequestCode 17) to ensure we get LTP + LTQ.
                    # Dhan Ticker (15) packets do not contain LTQ.
                    subscription_message = {
                        "RequestCode": 17,
                        "InstrumentCount": len(instruments),
                        "InstrumentList": [
                            {
                                "ExchangeSegment": self._segment_map.get(inst["exchange_segment"], inst["exchange_segment"]),
                                "SecurityId": inst["security_id"]
                            } for inst in instruments
                        ]
                    }
                    await ws.send(json.dumps(subscription_message))
                    logger.info(f"Subscribed to {len(instruments)} instruments")

                    async for message in ws:
                        if not self._running:
                            break
                        self._handle_message(message)
                        
            except websockets.exceptions.ConnectionClosed:
                logger.info("Dhan WebSocket connection closed by server")
                self._just_reconnected = True
                if self._reconnect_wait == 0:
                    self._reconnect_wait = 2
            except Exception as e:
                logger.error(f"Dhan WebSocket error: {e}")
                self._just_reconnected = True
                if self._reconnect_wait == 0:
                    self._reconnect_wait = 2
            
            if not self._running:
                break

    def _should_process_tick(self, tick: Tick) -> bool:
        """
        Returns False if tick timestamp is before SESSION_START.
        SESSION_START = 09:15:00 IST
        Returns False if tick timestamp is after SESSION_CLOSE (15:30).
        Returns True only during active market hours.
        """
        IST = pytz.timezone("Asia/Kolkata")
        try:
            now = tick.timestamp.astimezone(IST).time()
        except Exception:
            now = tick.timestamp.tz_convert(IST).time()
            
        session_start = datetime.strptime(CONFIG.trading.session_start, "%H:%M").time()
        session_close = datetime.strptime(CONFIG.trading.session_close, "%H:%M").time()
        return session_start <= now <= session_close

    def _handle_message(self, data: bytes) -> None:
        """Parses binary packet and calls handlers."""
        if not isinstance(data, bytes) or len(data) < 1:
            return

        # First byte is Feed Response Code
        # 2 = Ticker, 4 = Quote, 8 = Full
        response_code = struct.unpack('<B', data[0:1])[0]
        
        tick = None
        # Process Quote (4) and Full (8) as they contain LTQ field.
        # Ticker (2) is skipped silently per requirements as it lacks LTQ.
        if response_code == 4:
            tick = self._parse_quote_packet(data)
        elif response_code == 8:
            tick = self._parse_full_packet(data)
        
        if tick:
            if self._just_reconnected and self._last_tick_timestamp is not None:
                gap_start = self._last_tick_timestamp
                gap_end = tick.timestamp
                if (gap_end - gap_start).total_seconds() > 5.0:
                    with self.handlers_lock:
                        gap_handlers_to_call = list(self.gap_handlers)
                    for handler in gap_handlers_to_call:
                        try:
                            handler(gap_start, gap_end)
                        except Exception as e:
                            logger.error(f"Error in gap handler: {e}")
                self._just_reconnected = False
            
            self._last_tick_timestamp = tick.timestamp

            # Maintain prev_ltp state per security_id
            # For the first tick of a session, prev_ltp equals current ltp
            tick.prev_ltp = self.prev_ltps.get(tick.security_id, tick.ltp)
            if tick.ltq > 0:
                self.prev_ltps[tick.security_id] = tick.ltp
            
            logger.debug(f"Tick: {tick.security_id} | LTP: {tick.ltp} | LTQ: {tick.ltq}")
            
            # Check for SESSION_START event based on tick timestamp
            current_date = tick.timestamp.date()
            if self._session_start_emitted_for_date != current_date:
                try:
                    now_time = tick.timestamp.astimezone(self.ist_tz).time()
                except Exception:
                    now_time = tick.timestamp.tz_convert(self.ist_tz).time()
                    
                session_start_time = datetime.strptime(CONFIG.trading.session_start, "%H:%M").time()
                if now_time >= session_start_time:
                    self._session_start_emitted_for_date = current_date
                    with self.handlers_lock:
                        start_handlers_to_call = list(self.session_start_handlers)
                    for handler in start_handlers_to_call:
                        try:
                            handler()
                        except Exception as e:
                            logger.error(f"Error in session start handler: {e}")

            # Thread-safe handler execution
            with self.handlers_lock:
                handlers_to_call = list(self.handlers)
            
            if self._should_process_tick(tick):
                for handler in handlers_to_call:
                    try:
                        handler(tick)
                    except Exception as e:
                        logger.error(f"Error in tick handler: {e}")
            else:
                logger.debug(f"Pre/post market tick suppressed: {tick.ltp} at {tick.timestamp}")
                self._suppressed_ticks_count += 1
                current_time = builtin_time.time()
                if current_time - self._last_suppressed_log_time >= 60:
                    logger.info(f"Suppressing pre-open ticks: {self._suppressed_ticks_count:,} ticks suppressed so far")
                    self._last_suppressed_log_time = current_time

    def _parse_quote_packet(self, data: bytes) -> Optional[Tick]:
        """
        Unpacks 50-byte Quote packet.
        Format: <BHBIfHIfIIIffff
        Indices: 3=SecurityID, 4=LTP, 5=LTQ, 6=LTT(Epoch)
        """
        if len(data) < 50:
            return None
        
        try:
            unpacked = struct.unpack('<BHBIfHIfIIIffff', data[:50])
            return Tick(
                security_id=str(unpacked[3]),
                timestamp=pd.Timestamp(unpacked[6], unit='s', tz='UTC').tz_convert(self.ist_tz),
                ltp=float(unpacked[4]),
                ltq=int(unpacked[5]),
                prev_ltp=0.0 # populated in _handle_message
            )
        except Exception as e:
            logger.error(f"Failed to parse quote packet: {e}")
            return None

    def _parse_full_packet(self, data: bytes) -> Optional[Tick]:
        """
        Unpacks 162-byte Full packet.
        Format: <BHBIfHIfIIIIIIffff100s
        Indices: 3=SecurityID, 4=LTP, 5=LTQ, 6=LTT(Epoch)
        """
        if len(data) < 162:
            return None
        
        try:
            unpacked = struct.unpack('<BHBIfHIfIIIIIIffff100s', data[:162])
            return Tick(
                security_id=str(unpacked[3]),
                timestamp=pd.Timestamp(unpacked[6], unit='s', tz='UTC').tz_convert(self.ist_tz),
                ltp=float(unpacked[4]),
                ltq=int(unpacked[5]),
                prev_ltp=0.0 # populated in _handle_message
            )
        except Exception as e:
            logger.error(f"Failed to parse full packet: {e}")
            return None

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self.websocket:
            try:
                # RequestCode 12 signals disconnection to Dhan servers
                await self.websocket.send(json.dumps({"RequestCode": 12}))
                await self.websocket.close()
            except Exception:
                pass
            logger.info("Dhan WebSocket disconnected")
