import logging
import math
from dataclasses import dataclass
from typing import Optional, List
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

class InsufficientDataError(Exception):
    pass

@dataclass
class VolumeProfileNode:
    price: float          # lower edge of bucket
    volume: float         # total volume at this price level
    buy_volume: float     # approximated (50/50 split — refined later by delta)
    sell_volume: float

@dataclass
class VolumeProfile:
    id: str                           # UUID of the profile snapshot
    nodes: List[VolumeProfileNode]   # sorted by price ascending
    poc: float                        # price of POC bucket
    vah: float                        # upper edge of value area
    val: float                        # lower edge of value area
    total_volume: float
    value_area_pct: float             # typically 0.70
    session_start: pd.Timestamp
    session_end: pd.Timestamp
    is_low_conviction: bool = False
    volume_ratio: float = 1.0
    is_flat_profile: bool = False
    poc_concentration_pct: float = 0.0

class VolumeProfileEngine:
    THIN_VOLUME_THRESHOLD: float = 0.40  # below 40% of average = thin

    def __init__(self, bucket_size: float = 0.5, value_area_pct: float = 0.70, flat_profile_threshold_pct: float = 0.15):
        self.bucket_size = bucket_size
        self.value_area_pct = value_area_pct
        self.flat_profile_threshold_pct = flat_profile_threshold_pct
        self._avg_daily_volume: float = 0.0   # set by orchestrator at startup

    def set_baseline_volume(self, avg_daily_volume: float) -> None:
        self._avg_daily_volume = avg_daily_volume

    def _compute_volume_ratio(
        self,
        session_volume: float,
        avg_daily_volume: float,
    ) -> float:
        """Returns session_volume / avg_daily_volume. Capped at 2.0."""
        if avg_daily_volume == 0:
            return 1.0
        return min(session_volume / avg_daily_volume, 2.0)
        
    def _compute_poc_concentration(self, nodes: list[VolumeProfileNode]) -> float:
        """
        Returns the combined volume % of the top 3 buckets.
        
        top_3_volume = sum of highest 3 node volumes
        total_volume = sum of all node volumes
        concentration = top_3_volume / total_volume
        """
        if not nodes:
            return 0.0
        sorted_nodes = sorted(nodes, key=lambda n: n.volume, reverse=True)
        top_3 = sorted_nodes[:3]
        top_3_vol = sum(n.volume for n in top_3)
        total_vol = sum(n.volume for n in nodes)
        return top_3_vol / total_vol if total_vol > 0 else 0.0

    def _get_bucket(self, price: float) -> float:
        return round(price / self.bucket_size) * self.bucket_size

    def build(self, df: pd.DataFrame) -> VolumeProfile:
        """
        Build Fixed Range Volume Profile from OHLCV DataFrame.
        """
        if len(df) < 10:
            raise InsufficientDataError(f"Requires at least 10 candles to build profile, got {len(df)}")
            
        timestamps = df['timestamp'] if 'timestamp' in df.columns else df.index
        session_start = pd.Timestamp(timestamps.min())
        session_end = pd.Timestamp(timestamps.max())

        buckets = {}
        total_volume = 0.0

        lows = df['low'].values
        highs = df['high'].values
        vols = df['volume'].values

        for i in range(len(df)):
            low = float(lows[i])
            high = float(highs[i])
            vol = float(vols[i])
            
            start_bucket = self._get_bucket(low)
            end_bucket = self._get_bucket(high)
            
            # Number of buckets
            num_buckets = int(round((end_bucket - start_bucket) / self.bucket_size)) + 1
            if num_buckets < 1:
                num_buckets = 1
                
            vol_per_bucket = vol / num_buckets
            buy_vol = vol_per_bucket / 2.0
            sell_vol = vol_per_bucket / 2.0
            
            for j in range(num_buckets):
                b_price = round(start_bucket + j * self.bucket_size, 6)
                if b_price not in buckets:
                    buckets[b_price] = {'vol': 0.0, 'buy': 0.0, 'sell': 0.0}
                buckets[b_price]['vol'] += vol_per_bucket
                buckets[b_price]['buy'] += buy_vol
                buckets[b_price]['sell'] += sell_vol
                total_volume += vol_per_bucket

        nodes = []
        for p in sorted(buckets.keys()):
            nodes.append(VolumeProfileNode(
                price=p,
                volume=buckets[p]['vol'],
                buy_volume=buckets[p]['buy'],
                sell_volume=buckets[p]['sell']
            ))

        if not nodes:
            raise InsufficientDataError("No nodes generated for volume profile.")

        # Find POC (tiebreak uses the first max, which is the lower price since it's sorted, or just use max)
        poc_idx = max(range(len(nodes)), key=lambda i: nodes[i].volume)
        poc = nodes[poc_idx].price

        # Value Area Calculation
        target_vol = total_volume * self.value_area_pct
        vol_sum = nodes[poc_idx].volume
        
        up_idx = poc_idx + 1
        down_idx = poc_idx - 1
        
        while vol_sum < target_vol and (up_idx < len(nodes) or down_idx >= 0):
            up_vol = nodes[up_idx].volume if up_idx < len(nodes) else -1.0
            down_vol = nodes[down_idx].volume if down_idx >= 0 else -1.0
            
            if up_vol == -1.0 and down_vol == -1.0:
                break
                
            # Tiebreak: prefer upside
            if up_vol >= down_vol:
                vol_sum += up_vol
                up_idx += 1
            else:
                vol_sum += down_vol
                down_idx -= 1
                
        # Value Area limits
        highest_bucket_idx = up_idx - 1
        lowest_bucket_idx = down_idx + 1
        
        vah = nodes[highest_bucket_idx].price + self.bucket_size
        val = nodes[lowest_bucket_idx].price

        logger.info(f"Built Volume Profile: POC={poc:.2f}, VAH={vah:.2f}, VAL={val:.2f}, TotalVol={total_volume:.2f}")

        import uuid
        profile = VolumeProfile(
            id=str(uuid.uuid4()),
            nodes=nodes,
            poc=poc,
            vah=vah,
            val=val,
            total_volume=total_volume,
            value_area_pct=self.value_area_pct,
            session_start=session_start,
            session_end=session_end
        )
        
        profile.volume_ratio = self._compute_volume_ratio(
            session_volume=profile.total_volume,
            avg_daily_volume=self._avg_daily_volume
        )
        profile.is_low_conviction = profile.volume_ratio < self.THIN_VOLUME_THRESHOLD
        
        if profile.is_low_conviction:
            logger.warning(f"Low conviction session: volume {profile.volume_ratio:.0%} of average")

        profile.poc_concentration_pct = self._compute_poc_concentration(nodes)
        profile.is_flat_profile = profile.poc_concentration_pct < self.flat_profile_threshold_pct
        
        if profile.is_flat_profile:
            logger.warning(f"Flat profile detected: top 3 buckets = {profile.poc_concentration_pct:.1%} of volume — skipping")

        return profile

    def _get_datetime_series(self, df: pd.DataFrame) -> pd.Series:
        if 'timestamp' in df.columns:
            return pd.to_datetime(df['timestamp'])
        elif isinstance(df.index, pd.DatetimeIndex):
            return df.index.to_series()
        else:
            return pd.to_datetime(df.index)

    def build_session(self, df: pd.DataFrame) -> VolumeProfile:
        """
        Filter df to current session (9:15 AM IST to now) then call build().
        Assumes timestamps are in IST (or tz-naive representing IST).
        """
        dts = self._get_datetime_series(df)
        dates = dts.dt.date
        latest_date = dates.max()
        
        # Filter for latest date and time >= 09:15
        mask = (dates == latest_date) & (dts.dt.time >= pd.Timestamp('09:15').time())
        session_df = df[mask]
        
        return self.build(session_df)

    def build_prior_day(self, df: pd.DataFrame) -> VolumeProfile:
        """
        Filter df to previous trading day (9:15 to 15:30 IST) then call build().
        Raises InsufficientDataError if prior day data not present in df.
        """
        dts = self._get_datetime_series(df)
        unique_dates = sorted(dts.dt.date.unique())
        
        if len(unique_dates) < 2:
            raise InsufficientDataError("Prior day data not present in DataFrame.")
            
        prior_date = unique_dates[-2]
        
        # Filter for prior date and 09:15 <= time <= 15:30
        mask = (dts.dt.date == prior_date) & \
               (dts.dt.time >= pd.Timestamp('09:15').time()) & \
               (dts.dt.time <= pd.Timestamp('15:30').time())
               
        prior_df = df[mask]
        return self.build(prior_df)

    def get_nearest_node(
        self,
        profile: VolumeProfile,
        price: float,
        tolerance_pct: float = 0.002,   # 0.2% of price
    ) -> Optional[str]:
        """
        Returns "POC", "VAH", "VAL", or None.
        Checks if price is within tolerance of any key node.
        Used by signal engine to check if price is 'at a zone'.
        """
        tol = price * tolerance_pct
        
        # Check closest node
        dists = {
            "POC": abs(profile.poc - price),
            "VAH": abs(profile.vah - price),
            "VAL": abs(profile.val - price)
        }
        
        # Filter by tolerance
        valid_nodes = {k: v for k, v in dists.items() if v <= tol}
        if not valid_nodes:
            return None
            
        # Return the one with minimum distance
        return min(valid_nodes, key=valid_nodes.get)

    def is_price_in_value_area(self, profile: VolumeProfile, price: float) -> bool:
        """Returns True if val <= price <= vah."""
        return profile.val <= price <= profile.vah
