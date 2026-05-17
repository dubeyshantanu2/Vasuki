import unittest
import struct
import pandas as pd
import pytz
from data.dhan_ws import DhanWebSocketClient, Tick, DhanConfig

class TestDhanWebSocket(unittest.TestCase):
    def setUp(self):
        self.config = DhanConfig(client_id="test_client", access_token="test_token")
        self.client = DhanWebSocketClient(self.config)
        self.received_ticks = []

    def tick_handler(self, tick: Tick):
        self.received_ticks.append(tick)

    def test_parse_quote_packet(self):
        # <BHBIfHIfIIIffff (50 bytes)
        # B: 4 (Response Code)
        # H: 50 (Length)
        # B: 1 (NSE_EQ)
        # I: 13 (Security ID)
        # f: 22000.50 (LTP)
        # H: 50 (LTQ)
        # I: 1715835600 (Epoch for 2024-05-16 10:40:00 UTC)
        packet = struct.pack('<BHBIfHIfIIIffff', 4, 50, 1, 13, 22000.50, 50, 1715835600, 0, 0, 0, 0, 0, 0, 0, 0)
        
        self.client.register_tick_handler(self.tick_handler)
        self.client._handle_message(packet)
        
        self.assertEqual(len(self.received_ticks), 1)
        tick = self.received_ticks[0]
        self.assertEqual(tick.security_id, "13")
        self.assertEqual(tick.ltp, 22000.50)
        self.assertEqual(tick.ltq, 50)
        self.assertEqual(tick.prev_ltp, 22000.50) # First tick, prev == current
        
        # Verify IST conversion
        # 1715835600 UTC is 2024-05-16 16:10:00 IST
        ist_tz = pytz.timezone("Asia/Kolkata")
        expected_ts = pd.Timestamp(1715835600, unit='s', tz='UTC').tz_convert(ist_tz)
        self.assertEqual(tick.timestamp, expected_ts)

    def test_prev_ltp_state(self):
        self.client.register_tick_handler(self.tick_handler)
        
        # First tick
        p1 = struct.pack('<BHBIfHIfIIIffff', 4, 50, 1, 13, 22000.50, 50, 1715835600, 0, 0, 0, 0, 0, 0, 0, 0)
        self.client._handle_message(p1)
        
        # Second tick with different LTP
        p2 = struct.pack('<BHBIfHIfIIIffff', 4, 50, 1, 13, 22005.75, 25, 1715835601, 0, 0, 0, 0, 0, 0, 0, 0)
        self.client._handle_message(p2)
        
        self.assertEqual(len(self.received_ticks), 2)
        self.assertEqual(self.received_ticks[1].prev_ltp, 22000.50)
        self.assertEqual(self.received_ticks[1].ltp, 22005.75)

    def test_skip_ticker_packet(self):
        self.client.register_tick_handler(self.tick_handler)
        
        # Ticker packet (Response Code 2)
        # <BHBIfI (16 bytes)
        p_ticker = struct.pack('<BHBIfI', 2, 16, 1, 13, 22000.50, 1715835600)
        self.client._handle_message(p_ticker)
        
        self.assertEqual(len(self.received_ticks), 0)

if __name__ == '__main__':
    unittest.main()
