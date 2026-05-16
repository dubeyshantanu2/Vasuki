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

from config.settings import DhanConfig


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
        self.handlers_lock = Lock()
        self.prev_ltps: Dict[str, float] = {}
        self.ist_tz = pytz.timezone("Asia/Kolkata")
        self.websocket = None
        self._running = False
        self._reconnect_wait = 0
        self._max_reconnect_wait = 30
        
        # Dhan V2 exchange segment strings mapping
        self._segment_map = {
            "NSE_EQ": "NSE_EQ",
            "NSE_FNO": "NSE_FNO",
            "NSE_CURR": "NSE_CURRENCY",
            "BSE_EQ": "BSE_EQ",
            "MCX_COMM": "MCX_COMM",
            "BSE_CURR": "BSE_CURRENCY",
            "BSE_FNO": "BSE_FNO"
        }

    def register_tick_handler(self, handler: Callable[[Tick], None]) -> None:
        """Register a callback to receive every tick."""
        with self.handlers_lock:
            if handler not in self.handlers:
                self.handlers.append(handler)

    async def connect(self, instruments: list[dict]) -> None:
        """
        Connect to Dhan WebSocket and start streaming.
        instruments: [{"security_id": "13", "exchange_segment": "NSE_EQ"}]
        Runs indefinitely. Reconnects on disconnect with exponential backoff.
        Max reconnect wait: 30 seconds.
        """
        self._running = True
        self._reconnect_wait = 0

        while self._running:
            try:
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
                if self._reconnect_wait == 0:
                    self._reconnect_wait = 2
            except Exception as e:
                logger.error(f"Dhan WebSocket error: {e}")
                if self._reconnect_wait == 0:
                    self._reconnect_wait = 2
            
            if not self._running:
                break

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
            # Maintain prev_ltp state per security_id
            # For the first tick of a session, prev_ltp equals current ltp
            tick.prev_ltp = self.prev_ltps.get(tick.security_id, tick.ltp)
            self.prev_ltps[tick.security_id] = tick.ltp
            
            logger.debug(f"Tick: {tick.security_id} | LTP: {tick.ltp} | LTQ: {tick.ltq}")
            
            # Thread-safe handler execution
            with self.handlers_lock:
                handlers_to_call = list(self.handlers)
            
            for handler in handlers_to_call:
                try:
                    handler(tick)
                except Exception as e:
                    logger.error(f"Error in tick handler: {e}")

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
