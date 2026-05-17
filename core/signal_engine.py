import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict
import pandas as pd

from core.market_structure import StructureState, Bias
from core.volume_profile import VolumeProfile, VolumeProfileEngine
from core.delta import DeltaBuilder
from core.footprint import FootprintBuilder
from core.big_trades import BigTradeFilter
from config.settings import TradingConfig

# Attempt to import ExpiryConfig, fallback if not found
try:
    from config.expiry_config import ExpiryConfig
except ImportError:
    class ExpiryConfig:
        pass

logger = logging.getLogger(__name__)

@dataclass
class CooldownEntry:
    zone_type: str          # "POC", "VAH", "VAL"
    zone_price: float       # exact zone price
    direction: str          # "long" or "short"
    fired_at: pd.Timestamp
    t1_reached: bool = False

@dataclass
class Signal:
    symbol: str
    triggered_at: pd.Timestamp
    direction: str              # "long" or "short"
    bias: Bias
    zone_type: str              # "POC", "VAH", "VAL"
    zone_price: float
    current_price: float
    sl_price: float
    t1_price: float
    t2_price: float
    t3_price: float
    confirmations: dict         # {"delta": bool, "footprint": bool, "big_trade": bool}
    confirmation_count: int     # sum of True values in confirmations
    is_expiry_day: bool
    session_window: str         # "prime", "secondary", "final"
    confluence: dict = None
    id: str = ""                # UUID of the signal
    vp_snapshot_id: str = ""    # ID of the VP snapshot used at signal time
    vp_poc_at_signal: float = 0.0 # POC value when signal fired
    vp_vah_at_signal: float = 0.0
    vp_val_at_signal: float = 0.0
    # CONVENTION: Signal targets (t1, t2, t3) are FROZEN after creation.
    # VP migration never updates an existing signal's targets.

@dataclass
class ConfluenceZone:
    primary_zone: str          # "POC", "VAH", "VAL"
    all_zones: list[str]       # all zones present in confluence
    price: float               # center price of the confluence
    zone_range: tuple[float, float]   # (low, high) of all confluent levels
    strength: str              # "single", "double", "triple"
    sources: list[str]         # e.g. ["session_VAL", "prior_day_VAL"]

@dataclass 
class GateResult:
    gate: int
    passed: bool
    reason: str

class SignalEngine:
    def __init__(
        self,
        trading_config: TradingConfig,
        expiry_config: Optional[ExpiryConfig] = None,
    ):
        self.config = trading_config
        self.expiry_config = expiry_config
        self.vp_engine = VolumeProfileEngine(
            bucket_size=self.config.bucket_size, 
            value_area_pct=self.config.value_area_pct
        )
        self.spike_threshold_multiplier = self.config.spike_threshold_multiplier
        self.spike_suppression_candles = self.config.spike_suppression_candles
        self._spike_detected_at: Optional[pd.Timestamp] = None
        self._candles_since_spike: int = 0
        self._last_suppression_candle: Optional[pd.Timestamp] = None
        self.needs_vp_refresh: bool = False

    def _get_session_window(self, timestamp: pd.Timestamp) -> Optional[str]:
        """
        Returns "prime", "secondary", "final", or None if outside trade windows.
        Returns None during no-trade periods (open, lunch, close).
        """
        t = timestamp.time()
        
        t_no_trade = pd.Timestamp(self.config.no_trade_until).time()
        t_prime = pd.Timestamp(self.config.prime_end).time()
        t_sec = pd.Timestamp(self.config.secondary_end).time()
        t_lunch = pd.Timestamp(self.config.lunch_end).time()
        t_final = pd.Timestamp(self.config.final_end).time()
        
        if t < t_no_trade:
            return None
        elif t <= t_prime:
            return "prime"
        elif t <= t_sec:
            return "secondary"
        elif t < t_lunch:
            return None
        elif t <= t_final:
            return "final"
        else:
            return None

    def _is_trade_window_open(self, timestamp: pd.Timestamp) -> bool:
        """Returns True if current time is in a valid trade window."""
        return self._get_session_window(timestamp) is not None

    def find_confluence_zones(
        self,
        session_profile: VolumeProfile,
        prior_day_profile: VolumeProfile,
        current_price: float,
        confluence_threshold_pts: float = 10.0,
    ) -> list[ConfluenceZone]:
        """
        Collect all key nodes from both profiles:
        session: POC, VAH, VAL
        prior day: POC, VAH, VAL
        Total: up to 6 nodes
        
        Group nodes that are within confluence_threshold_pts of each other.
        Return ConfluenceZone for each group.
        Sort by proximity to current_price.
        """
        nodes = []
        if session_profile and not session_profile.is_flat_profile:
            nodes.extend([
                ("POC", session_profile.poc, "session_POC"),
                ("VAH", session_profile.vah, "session_VAH"),
                ("VAL", session_profile.val, "session_VAL")
            ])
        if prior_day_profile and not prior_day_profile.is_flat_profile:
            nodes.extend([
                ("POC", prior_day_profile.poc, "prior_day_POC"),
                ("VAH", prior_day_profile.vah, "prior_day_VAH"),
                ("VAL", prior_day_profile.val, "prior_day_VAL")
            ])
            
        if not nodes:
            return []
            
        groups = []
        for node_type, price, source in nodes:
            added = False
            for group in groups:
                if any(abs(price - g_price) <= confluence_threshold_pts for _, g_price, _ in group):
                    group.append((node_type, price, source))
                    added = True
                    break
            if not added:
                groups.append([(node_type, price, source)])
                
        confluence_zones = []
        for group in groups:
            prices = [p for _, p, _ in group]
            center_price = sum(prices) / len(prices)
            zone_range = (min(prices), max(prices))
            all_zones = [t for t, _, _ in group]
            sources = [s for _, _, s in group]
            
            primary_zone = all_zones[0]
            if "POC" in all_zones:
                primary_zone = "POC"
            elif "VAH" in all_zones:
                primary_zone = "VAH"
            elif "VAL" in all_zones:
                primary_zone = "VAL"
                
            strength_map = {1: "single", 2: "double", 3: "triple", 4: "triple", 5: "triple", 6: "triple"}
            strength = strength_map.get(len(group), "single")
            
            confluence_zones.append(ConfluenceZone(
                primary_zone=primary_zone,
                all_zones=all_zones,
                price=center_price,
                zone_range=zone_range,
                strength=strength,
                sources=sources
            ))
            
        confluence_zones.sort(key=lambda z: abs(z.price - current_price))
        return confluence_zones

    def _detect_volatility_spike(
        self,
        recent_candles_range: list[float],   # list of (high-low) for last 10 candles
        current_candle_range: float,
    ) -> tuple[bool, float]:
        """
        Computes average range of last 10 candles.
        If current candle range > SPIKE_THRESHOLD_MULTIPLIER * avg_range:
            → spike detected → return (True, avg_range)
        """
        if len(recent_candles_range) < 5:
            return False, 0.0
        avg_range = sum(recent_candles_range[-10:]) / min(len(recent_candles_range), 10)
        return current_candle_range > (avg_range * self.spike_threshold_multiplier), avg_range
def evaluate(
    self,
    current_price: float,
    timestamp: pd.Timestamp,
    current_high: float,
    current_low: float,
    recent_candle_ranges: list[float],
    structure_state: StructureState,
    session_profile: VolumeProfile,
    prior_day_profile: VolumeProfile,
    delta_builder: DeltaBuilder,
    footprint_builder: FootprintBuilder,
    big_trade_filter: BigTradeFilter,
    discord_client=None,
    supabase_client=None,
) -> Tuple[Optional[Signal], List[GateResult]]:
    """
    Runs all 4 gates in sequence. Returns (Signal, gate_results).
    """
    if self.expiry_config and getattr(self.expiry_config, 'no_trading_today', False):
        return None, []

    self._purge_expired_cooldowns(timestamp)
    results: List[GateResult] = []
        
        session_window = self._get_session_window(timestamp)
        if session_window is None:
            # If outside trade window, return (None, []) immediately
            return None, []

        # Volatility Spike Check
        current_range = current_high - current_low
        is_spike, avg_range = self._detect_volatility_spike(recent_candle_ranges, current_range)
        
        if is_spike:
            self._spike_detected_at = timestamp
            self._candles_since_spike = 0
            self._last_suppression_candle = timestamp.floor('5min')
            logger.warning(
                f"Volatility spike detected: range {current_range:.1f} pts "
                f"— signals suppressed for {self.spike_suppression_candles} candles"
            )
            if discord_client:
                import asyncio
                asyncio.create_task(discord_client.send_system_status("warning", 
                    f"Volatility spike: {current_range:.0f}pt candle — paused"))
            if supabase_client:
                # Calculate suppression end time approximately
                suppression_end = timestamp + pd.Timedelta(minutes=5 * self.spike_suppression_candles)
                supabase_client.save_spike_event(self.config.symbol, timestamp, current_range, avg_range, suppression_end)
            return None, []
        
        if self._spike_detected_at is not None:
            current_candle_start = timestamp.floor('5min')
            if self._last_suppression_candle != current_candle_start:
                self._candles_since_spike += 1
                self._last_suppression_candle = current_candle_start
                
            if self._candles_since_spike < self.spike_suppression_candles:
                # Optionally reduce log spam by logging only once per new candle if needed, but per prompt:
                logger.info(f"Post-spike suppression: {self._candles_since_spike}/{self.spike_suppression_candles}")
                return None, []
            else:
                self._spike_detected_at = None
                self._candles_since_spike = 0
                self.needs_vp_refresh = True
                logger.info("Post-spike suppression lifted — resuming normal evaluation")
                return None, []

        # ---------------------------------------------------------
        # GATE 1 - Market Structure
        # ---------------------------------------------------------
        if not structure_state.is_clear:
            results.append(GateResult(1, False, "Structure state is not clear"))
            logger.info(f"Gate 1 Failed: {results[-1].reason}")
            return None, results
            
        if structure_state.bias == Bias.NEUTRAL:
            results.append(GateResult(1, False, "Market bias is NEUTRAL"))
            logger.info(f"Gate 1 Failed: {results[-1].reason}")
            return None, results
            
        results.append(GateResult(1, True, f"Structure is clear. Bias is {structure_state.bias.name}"))

        # ---------------------------------------------------------
        # GATE 2 - Location
        # ---------------------------------------------------------
        confluence_zones = self.find_confluence_zones(
            session_profile, prior_day_profile, current_price,
            confluence_threshold_pts=getattr(self.config, 'confluence_threshold_pts', 10.0)
        )
        
        nearest = None
        if confluence_zones:
            closest_zone = confluence_zones[0]
            tol = closest_zone.price * 0.002
            if abs(closest_zone.price - current_price) <= tol:
                nearest = closest_zone

        if not nearest:
            results.append(GateResult(2, False, f"Price {current_price} is not at any key volume zone"))
            logger.info(f"Gate 2 Failed: {results[-1].reason}")
            return None, results
            
        zone_type = nearest.primary_zone
        zone_price = nearest.price
        results.append(GateResult(2, True, f"Price at zone {zone_type} ({zone_price}) [Confluence: {nearest.strength}]"))

        # ---------------------------------------------------------
        # GATE 3 - Zone-Bias Alignment
        # ---------------------------------------------------------
        direction = None
        if structure_state.bias == Bias.BULLISH:
            if zone_type in ["VAL", "POC"]:
                direction = "long"
            elif zone_type == "VAH" and current_price >= zone_price:
                direction = "long"
        elif structure_state.bias == Bias.BEARISH:
            if zone_type in ["VAH", "POC"]:
                direction = "short"
            elif zone_type == "VAL" and current_price <= zone_price:
                direction = "short"

        if direction is None:
            results.append(GateResult(3, False, f"Zone {zone_type} does not align with {structure_state.bias.name} bias"))
            logger.info(f"Gate 3 Failed: {results[-1].reason}")
            return None, results
            
        results.append(GateResult(3, True, f"Zone {zone_type} aligns with {structure_state.bias.name} for {direction.upper()}"))

        # ---------------------------------------------------------
        # GATE 4 - Confirmation (Order Flow)
        # ---------------------------------------------------------
        delta_confirmed = False
        footprint_confirmed = False
        big_trade_confirmed = False
        
        # 4a. Delta Confirmation
        fp_candles = footprint_builder.get_completed_candles()
        delta_candles = delta_builder.get_completed_candles()
        
        if len(fp_candles) >= 4 and len(delta_candles) >= 4:
            # Get last 4 candles to pass to detect_divergence with lookback=3
            recent_fp = fp_candles[-4:]
            recent_delta = delta_candles[-4:]
            
            delta_series = [c.delta for c in recent_delta]
            
            if direction == "long":
                # For bullish divergence (absorption at lows), we look at candle lows
                price_series = [min(c.levels.keys()) for c in recent_fp if c.levels]
                if len(price_series) == 4:
                    div = delta_builder.detect_divergence(price_series, delta_series, lookback=3)
                    if div == "bullish":
                        delta_confirmed = True
            elif direction == "short":
                # For bearish divergence (distribution at highs), we look at candle highs
                price_series = [max(c.levels.keys()) for c in recent_fp if c.levels]
                if len(price_series) == 4:
                    div = delta_builder.detect_divergence(price_series, delta_series, lookback=3)
                    if div == "bearish":
                        delta_confirmed = True

        # 4b. Footprint Confirmation
        if fp_candles:
            last_fp = fp_candles[-1]
            if direction == "long":
                if last_fp.bid_absorption or last_fp.stacked_imbalance_buy:
                    footprint_confirmed = True
            elif direction == "short":
                if last_fp.ask_absorption or last_fp.stacked_imbalance_sell:
                    footprint_confirmed = True

        # 4c. Big Trade Confirmation
        tol = zone_price * 0.002
        z_range = (zone_price - tol, zone_price + tol)
        dominant_side = big_trade_filter.get_dominant_side(within_seconds=60, price_range=z_range)
        
        if direction == "long" and dominant_side == "buy":
            big_trade_confirmed = True
        elif direction == "short" and dominant_side == "sell":
            big_trade_confirmed = True

        confirmations = {
            "delta": delta_confirmed,
            "footprint": footprint_confirmed,
            "big_trade": big_trade_confirmed,
            "low_conviction_day": session_profile.is_low_conviction if session_profile else False
        }
        
        # We don't count low_conviction_day as a passed confirmation, so we sum only the other three
        confirmation_count = sum(1 for k, v in confirmations.items() if k != "low_conviction_day" and k != "high_confidence_structure" and v is True)
        
        is_expiry_day = False
        if self.expiry_config and hasattr(self.expiry_config, 'is_expiry_day'):
            is_expiry_day = self.expiry_config.is_expiry_day
            if callable(is_expiry_day):
                is_expiry_day = is_expiry_day(timestamp.date())
                
        min_conf = 3 if is_expiry_day else self.config.min_confirmations

        if session_profile and session_profile.is_low_conviction:
            min_conf += 1
            logger.info(f"Low conviction profile — raising confirmation threshold to {min_conf}")
        
        if structure_state.confidence == "high" and not is_expiry_day:
            min_conf = max(1, min_conf - 1)
            confirmations["high_confidence_structure"] = True
        
        if confirmation_count < min_conf:
            results.append(GateResult(
                4, False, 
                f"Insufficient confirmations: {confirmation_count}/{min_conf} (Delta:{delta_confirmed}, FP:{footprint_confirmed}, BT:{big_trade_confirmed})"
            ))
            logger.info(f"Gate 4 Failed: {results[-1].reason}")
            return None, results
            
        results.append(GateResult(4, True, f"Confirmations passed: {confirmation_count}/{min_conf}"))
        logger.info("All Gates Passed! Emitting Signal.")

        # ---------------------------------------------------------
        # BUILD SIGNAL
        # ---------------------------------------------------------
        sl_buffer = self.config.sl_buffer_points
        if direction == "long":
            sl_price = nearest.zone_range[0] - sl_buffer
        else:
            sl_price = nearest.zone_range[1] + sl_buffer
        
        if zone_type == "VAL":
            t1_price = session_profile.poc
            t2_price = session_profile.vah
        elif zone_type == "VAH":
            t1_price = session_profile.poc
            t2_price = session_profile.val
        else: # POC
            t1_price = session_profile.vah if direction == "long" else session_profile.val
            # Fallback to further targets if needed, use prior day poc for t2
            t2_price = prior_day_profile.poc if prior_day_profile else t1_price

        t3_price = prior_day_profile.poc if prior_day_profile else t2_price
        
        import uuid
        signal = Signal(
            id=str(uuid.uuid4()),
            symbol=self.config.symbol,
            triggered_at=timestamp,
            direction=direction,
            bias=structure_state.bias,
            zone_type=zone_type,
            zone_price=zone_price,
            current_price=current_price,
            sl_price=sl_price,
            t1_price=t1_price,
            t2_price=t2_price,
            t3_price=t3_price,
            confirmations=confirmations,
            confirmation_count=confirmation_count,
            is_expiry_day=is_expiry_day,
            session_window=session_window,
            confluence={
                "strength": nearest.strength,
                "all_zones": nearest.all_zones,
                "sources": nearest.sources,
            },
            vp_snapshot_id=session_profile.id if session_profile else "",
            vp_poc_at_signal=session_profile.poc if session_profile else 0.0,
            vp_vah_at_signal=session_profile.vah if session_profile else 0.0,
            vp_val_at_signal=session_profile.val if session_profile else 0.0
        )
        
        return signal, results

    def is_signal_still_valid(
        self,
        signal: Signal,
        current_profile: VolumeProfile,
        current_price: float,
    ) -> tuple[bool, str]:
        """
        After a signal fires, call this on each subsequent evaluate() to
        check if the setup is still intact.
        
        Invalidation conditions:
        1. POC has migrated more than 30 points from signal time POC
           → "POC migration: {old} → {new} — setup invalidated"
        
        2. Price has moved more than 20 points AWAY from the zone
           in the WRONG direction (signal zone is no longer relevant)
           → "Price departed zone — setup invalidated"
        
        3. Current profile no longer has a zone near the original signal zone
           (zone dissolved into the profile)
           → "Zone dissolved — setup invalidated"
        
        Returns (True, "") if still valid.
        Returns (False, reason) if invalidated.
        """
        # We don't invalidate if price has reached T1
        if signal.direction == "long" and current_price >= signal.t1_price:
            return True, ""
        elif signal.direction == "short" and current_price <= signal.t1_price:
            return True, ""

        if not current_profile:
            return True, ""

        # 1. POC Migration
        poc_migration = abs(current_profile.poc - signal.vp_poc_at_signal)
        if poc_migration > self.config.poc_migration_threshold_points:
            return False, f"POC migration: {signal.vp_poc_at_signal:.2f} → {current_profile.poc:.2f} — setup invalidated"

        # 2. Price moved > 20 points away in WRONG direction
        if signal.direction == "long":
            if current_price < (signal.zone_price - 20):
                return False, "Price departed zone — setup invalidated"
        else:
            if current_price > (signal.zone_price + 20):
                return False, "Price departed zone — setup invalidated"

        # 3. Zone dissolved
        # Check if the original zone price is near any current zone (POC, VAH, VAL)
        tolerance = signal.zone_price * 0.002
        zone_found = False
        for current_zone_price in [current_profile.poc, current_profile.vah, current_profile.val]:
            if abs(current_zone_price - signal.zone_price) <= tolerance:
                zone_found = True
                break
                
        if not zone_found:
            return False, "Zone dissolved — setup invalidated"

        return True, ""
str]:
        """
        After a signal fires, call this on each subsequent evaluate() to
        check if the setup is still intact.
        
        Invalidation conditions:
        1. POC has migrated more than 30 points from signal time POC
           → "POC migration: {old} → {new} — setup invalidated"
        
        2. Price has moved more than 20 points AWAY from the zone
           in the WRONG direction (signal zone is no longer relevant)
           → "Price departed zone — setup invalidated"
        
        3. Current profile no longer has a zone near the original signal zone
           (zone dissolved into the profile)
           → "Zone dissolved — setup invalidated"
        
        Returns (True, "") if still valid.
        Returns (False, reason) if invalidated.
        """
        # We don't invalidate if price has reached T1
        if signal.direction == "long" and current_price >= signal.t1_price:
            return True, ""
        elif signal.direction == "short" and current_price <= signal.t1_price:
            return True, ""

        if not current_profile:
            return True, ""

        # 1. POC Migration
        poc_migration = abs(current_profile.poc - signal.vp_poc_at_signal)
        if poc_migration > self.config.poc_migration_threshold_points:
            return False, f"POC migration: {signal.vp_poc_at_signal:.2f} → {current_profile.poc:.2f} — setup invalidated"

        # 2. Price moved > 20 points away in WRONG direction
        if signal.direction == "long":
            if current_price < (signal.zone_price - 20):
                return False, "Price departed zone — setup invalidated"
        else:
            if current_price > (signal.zone_price + 20):
                return False, "Price departed zone — setup invalidated"

        # 3. Zone dissolved
        # Check if the original zone price is near any current zone (POC, VAH, VAL)
        tolerance = signal.zone_price * 0.002
        zone_found = False
        for current_zone_price in [current_profile.poc, current_profile.vah, current_profile.val]:
            if abs(current_zone_price - signal.zone_price) <= tolerance:
                zone_found = True
                break
                
        if not zone_found:
            return False, "Zone dissolved — setup invalidated"

        return True, ""
             
        if not zone_found:
            return False, "Zone dissolved — setup invalidated"

        return True, ""
