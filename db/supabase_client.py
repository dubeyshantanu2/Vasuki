import logging
import asyncio
import os
import json
import uuid
import datetime
from typing import Optional, List, Dict
import pandas as pd
from supabase import create_client, Client
from pydantic import BaseModel

from config.settings import SupabaseConfig
from core.market_structure import StructureState
from core.volume_profile import VolumeProfile
from core.delta import DeltaCandle
from core.big_trades import BigTrade

logger = logging.getLogger(__name__)

# Temporary mock of Signal until signal_engine is fully built
class Signal(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    id: Optional[str] = None
    symbol: str
    triggered_at: pd.Timestamp
    direction: str
    bias: str
    zone_type: str
    zone_price: float
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    t1_price: Optional[float] = None
    t2_price: Optional[float] = None
    t3_price: Optional[float] = None
    confirmations: Dict[str, bool] = {}
    confluence: float = 0.0
    is_expiry_day: bool = False

class LocalFallbackLogger:
    """
    Writes failed Supabase payloads to local JSONL files.
    One file per table per day: signals_2026-05-16.jsonl
    """
    def __init__(self, fallback_dir: str):
        if not fallback_dir.startswith('/') or not os.access(os.path.dirname(fallback_dir.rstrip('/')) or '/', os.W_OK):
            self.fallback_dir = os.path.join(os.getcwd(), "fallback")
        else:
            self.fallback_dir = fallback_dir
            
        try:
            os.makedirs(self.fallback_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create fallback directory {self.fallback_dir}: {e}")
            self.fallback_dir = os.path.join(os.getcwd(), "fallback")
            os.makedirs(self.fallback_dir, exist_ok=True)
            
        self._rotate_files()

    def _rotate_files(self):
        """Keep last 7 days of fallback files, delete older ones"""
        try:
            now = datetime.datetime.now()
            cutoff = now - datetime.timedelta(days=7)
            if not os.path.exists(self.fallback_dir):
                return
            for filename in os.listdir(self.fallback_dir):
                if not filename.endswith('.jsonl'):
                    continue
                parts = filename.split('_')
                if len(parts) >= 2:
                    date_str = parts[-1].replace('.jsonl', '')
                    try:
                        file_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
                        if file_date < cutoff:
                            os.remove(os.path.join(self.fallback_dir, filename))
                    except ValueError:
                        pass
        except Exception as e:
            logger.error(f"Failed rotating fallback files: {e}")

    def write(self, table: str, payload: dict) -> None:
        """
        Appends JSON line to fallback file.
        Filename: {table}_{date}.jsonl
        Never raises — if local write fails too, logs to stderr only.
        """
        try:
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            filename = f"{table}_{today_str}.jsonl"
            filepath = os.path.join(self.fallback_dir, filename)
            
            if "_fallback_id" not in payload:
                payload["_fallback_id"] = str(uuid.uuid4())
            payload["synced"] = False
            
            with open(filepath, 'a') as f:
                f.write(json.dumps(payload) + '\n')
        except Exception as e:
            logger.error(f"CRITICAL: Failed to write to local fallback logger: {e}")

    def get_unsynced(self, table: str) -> list[dict]:
        """
        Reads all fallback files for table.
        Returns payloads not yet synced to Supabase.
        """
        unsynced = []
        try:
            if not os.path.exists(self.fallback_dir):
                return []
            for filename in os.listdir(self.fallback_dir):
                if filename.startswith(f"{table}_") and filename.endswith('.jsonl'):
                    filepath = os.path.join(self.fallback_dir, filename)
                    with open(filepath, 'r') as f:
                        for line in f:
                            if not line.strip(): continue
                            try:
                                rec = json.loads(line)
                                if not rec.get("synced", False):
                                    unsynced.append(rec)
                            except json.JSONDecodeError:
                                pass
        except Exception as e:
            logger.error(f"Failed reading unsynced for {table}: {e}")
        return unsynced

    def mark_synced(self, table: str, record_id: str) -> None:
        """Mark a fallback record as synced (update in-place)."""
        try:
            if not os.path.exists(self.fallback_dir):
                return
            for filename in os.listdir(self.fallback_dir):
                if filename.startswith(f"{table}_") and filename.endswith('.jsonl'):
                    filepath = os.path.join(self.fallback_dir, filename)
                    records = []
                    modified = False
                    with open(filepath, 'r') as f:
                        for line in f:
                            if not line.strip(): continue
                            try:
                                rec = json.loads(line)
                                if rec.get("_fallback_id") == record_id:
                                    rec["synced"] = True
                                    modified = True
                                records.append(rec)
                            except json.JSONDecodeError:
                                pass
                    if modified:
                        with open(filepath, 'w') as f:
                            for rec in records:
                                f.write(json.dumps(rec) + '\n')
        except Exception as e:
            logger.error(f"Failed to mark synced for {table} {record_id}: {e}")

    def check_size_and_count(self) -> tuple[int, int]:
        total_size = 0
        unsynced_count = 0
        try:
            if not os.path.exists(self.fallback_dir):
                return 0, 0
            for filename in os.listdir(self.fallback_dir):
                if filename.endswith('.jsonl'):
                    filepath = os.path.join(self.fallback_dir, filename)
                    total_size += os.path.getsize(filepath)
                    with open(filepath, 'r') as f:
                        for line in f:
                            if not line.strip(): continue
                            try:
                                rec = json.loads(line)
                                if not rec.get("synced", False):
                                    unsynced_count += 1
                            except:
                                pass
        except Exception:
            pass
        return total_size, unsynced_count

class SupabaseClient:
    def __init__(self, config: SupabaseConfig):
        self.url = config.url
        self.key = config.key
        self._fallback = LocalFallbackLogger(config.fallback_dir)
        
        try:
            self.client: Client = create_client(self.url, self.key)
            logger.info("Initialized Supabase client")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}")
            self.client = None

    def _safe_float(self, val) -> Optional[float]:
        """Convert objects (like numpy/pandas types) to standard float safely, handling NaN."""
        if val is None or pd.isna(val):
            return None
        return float(val)

    async def _async_insert(self, table: str, data: dict) -> any:
        if not self.client:
            raise Exception("Supabase client not initialized")
        # Removing _fallback_id if present to prevent supabase error if column doesn't exist
        insert_data = {k: v for k, v in data.items() if k not in ["_fallback_id", "synced"]}
        return await asyncio.to_thread(self.client.table(table).insert(insert_data).execute)

    async def save_market_structure(self, symbol: str, state: StructureState) -> None:
        data = {
            "captured_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "symbol": symbol,
            "bias": state.bias.value if hasattr(state.bias, 'value') else str(state.bias),
            "last_event": state.last_event.value if hasattr(state.last_event, 'value') else str(state.last_event),
            "is_clear": state.is_clear,
            "last_swing_high": self._safe_float(state.last_swing_high.price) if state.last_swing_high else None,
            "last_swing_low": self._safe_float(state.last_swing_low.price) if state.last_swing_low else None,
            "gap_info": state.gap_info,
            "confidence": state.confidence,
            "adaptive_lookback_used": state.adaptive_lookback_used,
        }
        try:
            await self._async_insert("market_structure_snapshots", data)
        except Exception as e:
            logger.error(f"Supabase write failed for market_structure_snapshots: {e}")
            asyncio.create_task(asyncio.to_thread(self._fallback.write, "market_structure_snapshots", data))

    async def save_volume_profile(self, symbol: str, profile: VolumeProfile, session_type: str) -> None:
        data = {
            "captured_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "symbol": symbol,
            "session_type": session_type,
            "poc": self._safe_float(profile.poc),
            "vah": self._safe_float(profile.vah),
            "val": self._safe_float(profile.val),
            "total_volume": self._safe_float(profile.total_volume),
            "volume_ratio": self._safe_float(profile.volume_ratio),
            "poc_concentration_pct": self._safe_float(profile.poc_concentration_pct),
        }
        try:
            await self._async_insert("volume_profile_snapshots", data)
        except Exception as e:
            logger.error(f"Supabase write failed for volume_profile_snapshots: {e}")
            asyncio.create_task(asyncio.to_thread(self._fallback.write, "volume_profile_snapshots", data))

    async def save_delta_candle(self, symbol: str, candle: DeltaCandle) -> None:
        interval_mins = int((candle.interval_end - candle.interval_start).total_seconds() / 60)
        data = {
            "symbol": symbol,
            "interval_start": candle.interval_start.isoformat(),
            "interval_minutes": interval_mins,
            "buy_volume": self._safe_float(candle.buy_volume),
            "sell_volume": self._safe_float(candle.sell_volume),
            "delta": self._safe_float(candle.delta),
            "cumulative_delta": self._safe_float(candle.cumulative_delta),
        }
        try:
            await self._async_insert("delta_candles", data)
        except Exception as e:
            logger.error(f"Supabase write failed for delta_candles: {e}")
            asyncio.create_task(asyncio.to_thread(self._fallback.write, "delta_candles", data))

    async def save_big_trade(self, symbol: str, trade: BigTrade) -> None:
        data = {
            "symbol": symbol,
            "traded_at": trade.timestamp.isoformat(),
            "price": self._safe_float(trade.price),
            "quantity_lots": int(trade.quantity),
            "direction": trade.direction,
            "significance": trade.significance,
            "is_suspected_rollover": trade.is_suspected_rollover,
            "is_outlier": trade.is_outlier,
            "outlier_ratio": self._safe_float(trade.outlier_ratio),
        }
        try:
            await self._async_insert("big_trades", data)
        except Exception as e:
            logger.error(f"Supabase write failed for big_trades: {e}")
            asyncio.create_task(asyncio.to_thread(self._fallback.write, "big_trades", data))

    async def save_spike_event(self, symbol: str, detected_at: pd.Timestamp, candle_range: float, avg_range: float, suppression_end: Optional[pd.Timestamp]) -> None:
        data = {
            "symbol": symbol,
            "detected_at": detected_at.isoformat(),
            "candle_range": self._safe_float(candle_range),
            "avg_range": self._safe_float(avg_range),
            "suppression_end": suppression_end.isoformat() if suppression_end else None,
        }
        try:
            await self._async_insert("spike_events", data)
        except Exception as e:
            logger.error(f"Supabase write failed for spike_events: {e}")
            asyncio.create_task(asyncio.to_thread(self._fallback.write, "spike_events", data))

    def _signal_to_dict(self, signal: Signal) -> dict:
        return {
            "id": signal.id,
            "symbol": signal.symbol,
            "triggered_at": signal.triggered_at.isoformat(),
            "direction": signal.direction,
            "bias": signal.bias.value if hasattr(signal.bias, 'value') else str(signal.bias),
            "zone_type": signal.zone_type,
            "zone_price": self._safe_float(signal.zone_price),
            "entry_price": self._safe_float(signal.entry_price),
            "sl_price": self._safe_float(signal.sl_price),
            "t1_price": self._safe_float(signal.t1_price),
            "t2_price": self._safe_float(signal.t2_price),
            "t3_price": self._safe_float(signal.t3_price),
            "confirmations": signal.confirmations,
            "confluence": getattr(signal, "confluence", 0.0),
            "is_expiry_day": signal.is_expiry_day,
            "status": "active"
        }

    async def save_signal(self, signal: Signal) -> Optional[str]:
        if getattr(signal, 'id', None) is None:
            signal.id = str(uuid.uuid4())
            
        payload = self._signal_to_dict(signal)
        try:
            result = await self._async_insert("signals", payload)
            return result.data[0]["id"]
        except Exception as e:
            logger.error(f"Supabase write failed for signals: {e}")
            asyncio.create_task(asyncio.to_thread(self._fallback.write, "signals", payload))
            return None

    async def mark_signal_invalidated(self, signal_id: str, reason: str) -> None:
        data = {
            "status": "invalidated",
            "invalidation_reason": reason
        }
        try:
            await asyncio.to_thread(self.client.table("signals").update(data).eq("id", signal_id).execute)
        except Exception as e:
            logger.error(f"Supabase update failed for signals (invalidated): {e}")

    def get_today_signals(self, symbol: str) -> List[Dict]:
        if not self.client:
            logger.error("Supabase client not initialized. Cannot fetch signals.")
            return []
            
        try:
            today_start = pd.Timestamp.now(tz="Asia/Kolkata").replace(hour=0, minute=0, second=0, microsecond=0)
            
            response = (
                self.client.table("signals")
                .select("*")
                .eq("symbol", symbol)
                .gte("triggered_at", today_start.isoformat())
                .order("triggered_at", desc=True)
                .execute()
            )
            return response.data
        except Exception as e:
            logger.error(f"Failed to fetch today's signals: {e}")
            return []

    async def sync_fallback_records(self, discord_client=None) -> None:
        """
        Called at system startup.
        Reads all unsynced fallback records and attempts to insert them.
        On success: marks as synced.
        On failure: leaves for next startup attempt.
        Log summary: "Synced N fallback records to Supabase"
        """
        tables = ["market_structure_snapshots", "volume_profile_snapshots", "delta_candles", "big_trades", "spike_events", "signals"]
        synced_count = 0
        total_unsynced = 0
        
        for table in tables:
            records = self._fallback.get_unsynced(table)
            for rec in records:
                total_unsynced += 1
                try:
                    fallback_id = rec.get("_fallback_id")
                    if fallback_id:
                        await self._async_insert(table, rec)
                        self._fallback.mark_synced(table, fallback_id)
                        synced_count += 1
                except Exception as e:
                    logger.debug(f"Failed syncing fallback record for {table}: {e}")
                    
        logger.info(f"Synced {synced_count} fallback records to Supabase")
        
        # Check size limit (1MB = 1048576 bytes)
        total_size, remaining_unsynced = self._fallback.check_size_and_count()
        if total_size > 1048576 and discord_client:
            msg = f"⚠ Supabase sync issue: {remaining_unsynced} unsynced records in fallback"
            asyncio.create_task(discord_client.send_system_status("error", msg))
