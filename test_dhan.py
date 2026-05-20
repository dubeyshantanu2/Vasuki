import os
import asyncio
from dotenv import load_dotenv
load_dotenv()
from dhanhq import dhanhq, DhanContext
import datetime
import pytz

client_id = os.getenv("DHAN_CLIENT_ID", "")
access_token = os.getenv("DHAN_ACCESS_TOKEN", "")

context = DhanContext(client_id, access_token)
dhan = dhanhq(context)

today = datetime.datetime.now(pytz.timezone('Asia/Kolkata'))
to_date = today.strftime("%Y-%m-%d")
from_date = (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

print(f"Fetching from {from_date} to {to_date}")
resp = dhan.intraday_minute_data(
    security_id="13",
    exchange_segment="IDX_I",
    instrument_type="INDEX",
    from_date=from_date,
    to_date=to_date,
    interval=15
)
print("Interval 15:")
print(resp)

resp2 = dhan.intraday_minute_data(
    security_id="13",
    exchange_segment="IDX_I",
    instrument_type="INDEX",
    from_date=from_date,
    to_date=to_date,
    interval=60
)
print("Interval 60:")
print(resp2)

resp3 = dhan.intraday_minute_data(
    security_id="13",
    exchange_segment="IDX_I",
    instrument_type="INDEX",
    from_date=from_date,
    to_date=to_date,
    interval=1
)
print("Interval 1:")
print(resp3)

