import pandas as pd
import numpy as np
from core.volume_profile import VolumeProfileEngine, InsufficientDataError

def test_volume_profile_engine():
    # Create sample DataFrame with 2 days of data to test build_session and build_prior_day
    dts1 = pd.date_range(start='2023-01-01 09:15:00', periods=75, freq='5min') # Day 1 (up to 15:25)
    dts2 = pd.date_range(start='2023-01-02 09:15:00', periods=75, freq='5min') # Day 2 (up to 15:25)
    
    timestamps = dts1.append(dts2)
    n = len(timestamps)
    
    data = {
        'timestamp': timestamps,
        'open': [100.0] * n,
        'high': [102.0] * n,
        'low': [98.0] * n,
        'close': [101.0] * n,
        'volume': [100] * n
    }
    df = pd.DataFrame(data)
    
    # Introduce some variation to make a clear POC
    # Make a huge volume node at price 100.0 for Day 2
    df.loc[100:110, 'low'] = 99.8
    df.loc[100:110, 'high'] = 100.2
    df.loc[100:110, 'volume'] = 5000 
    
    engine = VolumeProfileEngine(bucket_size=0.5, value_area_pct=0.70)
    
    # Test full build
    profile = engine.build(df)
    print(f"Full Build -> POC: {profile.poc}, VAH: {profile.vah}, VAL: {profile.val}")
    assert profile.val <= profile.poc <= profile.vah
    
    # Test nearest node
    nearest = engine.get_nearest_node(profile, profile.poc * 1.001)
    print(f"Nearest to {profile.poc * 1.001}: {nearest}")
    assert nearest == "POC"
    
    # Test session build (Day 2)
    session_profile = engine.build_session(df)
    print(f"Session Build -> POC: {session_profile.poc}, VAH: {session_profile.vah}, VAL: {session_profile.val}")
    assert session_profile.session_start == pd.Timestamp('2023-01-02 09:15:00')
    
    # Test prior day build (Day 1)
    prior_profile = engine.build_prior_day(df)
    print(f"Prior Day Build -> POC: {prior_profile.poc}, VAH: {prior_profile.vah}, VAL: {prior_profile.val}")
    assert prior_profile.session_start == pd.Timestamp('2023-01-01 09:15:00')

    print("All tests passed.")

if __name__ == "__main__":
    test_volume_profile_engine()
