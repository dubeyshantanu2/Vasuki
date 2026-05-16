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

    def evaluate(
        self,
        current_price: float,
        timestamp: pd.Timestamp,
        structure_state: StructureState,
        session_profile: VolumeProfile,
        prior_day_profile: VolumeProfile,
        delta_builder: DeltaBuilder,
        footprint_builder: FootprintBuilder,
        big_trade_filter: BigTradeFilter,
    ) -> Tuple[Optional[Signal], List[GateResult]]:
        """
        Runs all 4 gates in sequence. Returns (Signal, gate_results).
        """
        results: List[GateResult] = []
        
        session_window = self._get_session_window(timestamp)
        if session_window is None:
            # If outside trade window, return (None, []) immediately
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
        zone_type = None
        zone_price = None
        
        # Check session profile first
        nn_session = self.vp_engine.get_nearest_node(session_profile, current_price, tolerance_pct=0.002)
        if nn_session:
            zone_type = nn_session
            zone_price = getattr(session_profile, nn_session.lower())
        else:
            # Fallback to prior day profile
            nn_prior = self.vp_engine.get_nearest_node(prior_day_profile, current_price, tolerance_pct=0.002)
            if nn_prior:
                zone_type = nn_prior
                zone_price = getattr(prior_day_profile, nn_prior.lower())

        if not zone_type:
            results.append(GateResult(2, False, f"Price {current_price} is not at any key volume zone"))
            logger.info(f"Gate 2 Failed: {results[-1].reason}")
            return None, results
            
        results.append(GateResult(2, True, f"Price at zone {zone_type} ({zone_price})"))

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
            "big_trade": big_trade_confirmed
        }
        confirmation_count = sum(confirmations.values())
        
        is_expiry_day = False
        if self.expiry_config and hasattr(self.expiry_config, 'is_expiry_day'):
            is_expiry_day = self.expiry_config.is_expiry_day
            if callable(is_expiry_day):
                is_expiry_day = is_expiry_day(timestamp.date())
                
        min_conf = 3 if is_expiry_day else self.config.min_confirmations
        
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
        sl_price = zone_price - sl_buffer if direction == "long" else zone_price + sl_buffer
        
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
        
        signal = Signal(
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
            session_window=session_window
        )
        
        return signal, results
