import struct
import pandas as pd
import pytz

# The Dhan API custom epoch starts at 1980-01-01 00:00:00
DHAN_EPOCH_START = pd.Timestamp("1980-01-01 00:00:00", tz="Asia/Kolkata")

# Example epoch from a past tick packet or a recent unix timestamp if we don't have one.
# Let's see what happens if we just use a standard unix timestamp vs Dhan epoch
import time
current_unix = int(time.time())

# Dhan custom epoch seconds = current IST time - 1980-01-01 IST
current_ist = pd.Timestamp.now(tz="Asia/Kolkata")
dhan_epoch_seconds = int((current_ist - DHAN_EPOCH_START).total_seconds())

print(f"Current Unix Epoch: {current_unix}")
print(f"Dhan Epoch Seconds: {dhan_epoch_seconds}")

# How the code parses it currently:
parsed_unix = pd.Timestamp(current_unix, unit='s', tz='UTC').tz_convert("Asia/Kolkata")
print(f"Code parsing unix as UTC: {parsed_unix}")

parsed_dhan = pd.Timestamp(dhan_epoch_seconds, unit='s', tz='UTC').tz_convert("Asia/Kolkata")
print(f"Code parsing Dhan epoch as UTC: {parsed_dhan}")

# Correct way to parse Dhan epoch
correct_parsed = DHAN_EPOCH_START + pd.Timedelta(seconds=dhan_epoch_seconds)
print(f"Correct parsed Dhan epoch: {correct_parsed}")

