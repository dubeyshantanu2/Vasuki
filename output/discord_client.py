import asyncio
import time
from collections import deque
from typing import List

import aiohttp
import pandas as pd
from loguru import logger

from config.settings import DiscordConfig
from core.signal_engine import Signal, GateResult
from core.big_trades import BigTrade


class DiscordClient:
    def __init__(self, config: DiscordConfig):
        self.config = config
        self._timestamps = deque(maxlen=5)
        self._lock = asyncio.Lock()

    def _format_price(self, price: float) -> str:
        """Format price with Indian comma notation (e.g., 22,450.0 or 1,22,450.0)."""
        if price is None:
            return "0.0"
        s = f"{price:.1f}"
        if "." in s:
            integer_part, decimal_part = s.split(".")
        else:
            integer_part, decimal_part = s, "0"

        if len(integer_part) > 3:
            last_3 = integer_part[-3:]
            rest = integer_part[:-3]
            chunks = []
            while rest:
                chunks.insert(0, rest[-2:])
                rest = rest[:-2]
            return ",".join(chunks) + "," + last_3 + "." + decimal_part
        return f"{integer_part}.{decimal_part}"

    async def _wait_rate_limit(self) -> None:
        """Ensure max 5 messages per 5 seconds."""
        async with self._lock:
            if len(self._timestamps) == 5:
                elapsed = time.monotonic() - self._timestamps[0]
                if elapsed < 5.0:
                    await asyncio.sleep(5.0 - elapsed)
            self._timestamps.append(time.monotonic())

    async def _post(self, webhook_url: str, payload: dict) -> None:
        """Internal. POST to webhook with retry (3 attempts, 1s backoff)."""
        if not webhook_url:
            return

        await self._wait_rate_limit()

        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(webhook_url, json=payload, timeout=10) as resp:
                        if resp.status in (200, 204):
                            return
                        elif resp.status == 429:
                            try:
                                data = await resp.json()
                                retry_after = data.get("retry_after", 1.0)
                            except Exception:
                                retry_after = 1.0
                            await asyncio.sleep(retry_after)
                            continue
                        else:
                            text = await resp.text()
                            logger.error(f"Discord webhook error {resp.status}: {text}")
            except Exception as e:
                logger.error(f"Discord POST failed (attempt {attempt + 1}/3): {e}")

            if attempt < 2:
                await asyncio.sleep(1)

    def _build_signal_box(self, signal: Signal) -> str:
        def pad(text: str) -> str:
            return f"│ {text:<35} │"

        def row(k: str, v: str) -> str:
            left = f"{k:<11}"
            return f"│ {left} │ {v:<21} │"

        lines = []
        lines.append("┌─────────────────────────────────────┐")
        
        zone_price_str = self._format_price(signal.zone_price)
        lines.append(row("Zone", f"{signal.zone_type} @ {zone_price_str}"))
        
        bias_val = getattr(signal.bias, 'value', signal.bias)
        lines.append(row("Bias", f"{str(bias_val).capitalize()} (1H)"))
        
        lines.append(row("Entry", "Next 5m candle open"))
        lines.append(row("Stop Loss", self._format_price(signal.sl_price)))
        lines.append(row("Target 1", self._format_price(signal.t1_price)))
        lines.append(row("Target 2", self._format_price(signal.t2_price)))
        lines.append(row("Target 3", self._format_price(signal.t3_price)))
        lines.append("├─────────────────────────────────────┤")
        lines.append(pad("Confirmations:"))

        for conf, passed in signal.confirmations.items():
            icon = "✅" if passed else "❌"
            name = conf.replace("_", " ").title()
            if conf.lower() == "delta":
                name = "Delta Divergence"
            elif conf.lower() == "footprint":
                name = "Footprint Absorption"
            elif conf.lower() == "big_trade":
                name = "Big Trade"
            lines.append(pad(f"{icon} {name}"))

        lines.append("├─────────────────────────────────────┤")

        window = str(signal.session_window).capitalize()
        if str(signal.session_window).lower() == "prime":
            window += " (9:45–11:30)"

        lines.append(row("Window", window))
        lines.append(row("Expiry Day", "Yes" if signal.is_expiry_day else "No"))
        lines.append("└─────────────────────────────────────┘")

        return "```text\n" + "\n".join(lines) + "\n```"

    async def send_signal(self, signal: Signal) -> None:
        """Send a formatted trade signal embed to Discord."""
        if not self.config.webhook_url:
            return

        is_long = signal.direction.lower() == "long"
        title = f"{'🔼 LONG' if is_long else '🔽 SHORT'} SIGNAL — {signal.symbol}"
        color = 0x00C853 if is_long else 0xD50000

        embed = {
            "title": title,
            "color": color,
            "description": self._build_signal_box(signal),
            "footer": {
                "text": signal.triggered_at.strftime("%H:%M:%S IST")
            }
        }

        await self._post(self.config.webhook_url, {"embeds": [embed]})

    async def send_gate_failure(
        self,
        gate_results: List[GateResult],
        current_price: float,
        timestamp: pd.Timestamp,
    ) -> None:
        """
        Optional: send gate failure summary to a separate debug webhook.
        Only send if at least GATE 2 passed (price was at a zone).
        Format: compact text, not embed.
        """
        if not self.config.alert_webhook_url:
            return

        gate_2_passed = any(g.gate == 2 and g.passed for g in gate_results)
        if not gate_2_passed:
            return

        failed_gates = [g for g in gate_results if not g.passed]
        if not failed_gates:
            return

        time_str = timestamp.strftime("%H:%M:%S IST")
        price_str = self._format_price(current_price)
        msg = f"🚫 **Gate Failure** at {time_str} | Price: {price_str}\n"
        for g in failed_gates:
            msg += f"• Gate {g.gate}: {g.reason}\n"

        await self._post(self.config.alert_webhook_url, {"content": msg})

    async def send_system_status(self, status: str, message: str) -> None:
        """Send system health messages to alert webhook."""
        if not self.config.alert_webhook_url:
            return

        emoji_map = {
            "started": "🟢",
            "stopped": "🔴",
            "reconnecting": "🔄",
            "error": "⚠️"
        }
        icon = emoji_map.get(status.lower(), "ℹ️")

        payload = {
            "content": f"{icon} **System {status.capitalize()}**: {message}"
        }
        await self._post(self.config.alert_webhook_url, payload)

    async def send_big_trade_alert(self, trade: BigTrade, symbol: str) -> None:
        """
        Send a block trade alert (1000+ lots) immediately when detected.
        Format: simple embed with price, quantity, direction.
        Only for significance == 'block'.
        """
        if not self.config.webhook_url:
            return

        if trade.significance.lower() != "block":
            return

        is_buy = trade.direction.lower() == "buy"
        color = 0x00C853 if is_buy else 0xD50000
        title = f"🐋 BLOCK {trade.direction.upper()} — {symbol}"

        embed = {
            "title": title,
            "color": color,
            "description": (
                f"**Quantity:** {trade.quantity} lots\n"
                f"**Price:** {self._format_price(trade.price)}\n"
                f"**Time:** {trade.timestamp.strftime('%H:%M:%S IST')}"
            )
        }

        await self._post(self.config.webhook_url, {"embeds": [embed]})
