import time
from datetime import datetime
import pandas as pd
import pytz
from loguru import logger
from dhanhq import dhanhq, DhanContext

from config.settings import DhanConfig
from data import DataFetchError, InsufficientDataError


class DhanRestClient:
    """
    Fetches OHLCV candlestick data from Dhan HQ REST API.
    Wraps the dhanhq SDK and adds error handling + retry logic.
    """

    def __init__(self, config: DhanConfig):
        self.config = config
        
        # Initialize Dhan SDK (v2.2+ approach)
        try:
            context = DhanContext(config.client_id, config.access_token)
            self.dhan = dhanhq(context)
        except Exception as e:
            logger.error(f"Failed to initialize DhanHQ SDK: {e}")
            raise

        self.ist_tz = pytz.timezone("Asia/Kolkata")
        
        # Cache structure: {(security_id, interval): (timestamp_float, dataframe)}
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

    def _fetch_with_retry(self, api_func, *args, **kwargs) -> dict:
        """
        Executes API call with 3 attempts and 2s backoff.
        Logs duration and handles errors.
        """
        max_attempts = 3
        backoff_sec = 2
        last_exception = None

        for attempt in range(1, max_attempts + 1):
            start_time = time.time()
            try:
                response = api_func(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"API Call {api_func.__name__} took {duration_ms:.2f}ms "
                    f"(Attempt {attempt}/{max_attempts})"
                )
                
                # Validate response structure
                if isinstance(response, dict):
                    status = response.get("status", "").lower()
                    if status in ("failure", "error"):
                        error_msg = response.get("remarks") or response.get("error_message") or str(response)
                        raise DataFetchError(f"Dhan API returned failure: {error_msg}")
                    
                    if "data" not in response and "start_Time" not in response:
                        raise DataFetchError(f"Unrecognized response format: {response}")

                return response

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                logger.warning(
                    f"API Call {api_func.__name__} failed in {duration_ms:.2f}ms "
                    f"(Attempt {attempt}/{max_attempts}): {e}"
                )
                last_exception = e

                if attempt < max_attempts:
                    time.sleep(backoff_sec)

        raise DataFetchError(f"Permanent failure after {max_attempts} attempts. Last error: {last_exception}")

    def _process_response(self, response: dict) -> pd.DataFrame:
        """
        Processes Dhan response into normalized DataFrame.
        """
        if not response:
            raise DataFetchError("Empty response received from Dhan API")

        # Handle different response structures
        if isinstance(response, dict):
            data = response.get("data", response)
        else:
            raise DataFetchError(f"Unexpected response type: {type(response)}")

        if not data or not isinstance(data, dict):
            raise DataFetchError("Response does not contain valid data dictionary")

        # Locate the timestamp key
        time_key = next((k for k in data.keys() if "time" in k.lower()), None)
        if not time_key:
            raise DataFetchError(f"Could not find time key in response data keys: {list(data.keys())}")

        df = pd.DataFrame(data)

        if df.empty:
            raise DataFetchError("Dhan API returned no data (empty DataFrame)")

        # Map to standard column names
        actual_rename = {}
        for col in df.columns:
            lower_col = col.lower()
            for std_col in ["open", "high", "low", "close", "volume"]:
                if std_col in lower_col:
                    actual_rename[col] = std_col
            if col == time_key:
                actual_rename[col] = "timestamp"

        df = df.rename(columns=actual_rename)

        # Ensure all required columns are present
        required_cols = {"timestamp", "open", "high", "low", "close", "volume"}
        missing_cols = required_cols - set(df.columns)
        if missing_cols:
            raise DataFetchError(f"Missing required columns in API response: {missing_cols}")

        # Parse timestamps to IST
        try:
            if pd.api.types.is_numeric_dtype(df["timestamp"]):
                # Convert based on magnitude (s vs ms)
                if df["timestamp"].iloc[0] > 1e11:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                else:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
            else:
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

            df["timestamp"] = df["timestamp"].dt.tz_convert(self.ist_tz)
        except Exception as e:
            raise DataFetchError(f"Failed to parse timestamps: {e}")

        # Cast numerics
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # Sort and return
        df = df.sort_values(by="timestamp", ascending=True).reset_index(drop=True)
        return df

    def get_candles(
        self,
        security_id: str,
        exchange_segment: str,
        instrument_type: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> pd.DataFrame:
        """
        Returns DataFrame with columns:
        timestamp (pd.Timestamp, IST), open, high, low, close, volume
        Sorted ascending by timestamp.
        """
        cache_key = (security_id, interval)
        if cache_key in self._cache:
            cache_time, cached_df = self._cache[cache_key]
            if time.time() - cache_time < 60:
                logger.info(f"Cache hit for {cache_key}. Returning cached data.")
                return cached_df.copy()

        # Dhan SDK 'historical_daily_data' maps to "D"
        if interval == "D":
            response = self._fetch_with_retry(
                self.dhan.historical_daily_data,
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=from_date,
                to_date=to_date,
                expiry_code=0
            )
        else:
            try:
                interval_int = int(interval)
            except ValueError:
                raise ValueError(f"Interval must be 'D' or integer string, got: {interval}")
            
            response = self._fetch_with_retry(
                self.dhan.intraday_minute_data,
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=from_date,
                to_date=to_date,
                interval=interval_int
            )

        df = self._process_response(response)
        
        # Save to memory cache
        self._cache[cache_key] = (time.time(), df.copy())
        
        return df

    def get_today_candles(
        self,
        security_id: str,
        exchange_segment: str,
        instrument_type: str,
        interval: str,
    ) -> pd.DataFrame:
        """
        Convenience method. Fetches from today 9:15 AM IST to now.
        """
        now_ist = datetime.now(self.ist_tz)
        today_str = now_ist.strftime("%Y-%m-%d")
        
        df = self.get_candles(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            interval=interval,
            from_date=today_str,
            to_date=today_str
        )
        
        # Filter for today >= 9:15 AM
        market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        df = df[df["timestamp"] >= market_open].reset_index(drop=True)
        
        if df.empty:
            raise DataFetchError(f"No data available for today after 9:15 AM for {security_id}")
            
        return df

    def get_intraday_candles(
        self,
        security_id: str,
        exchange_segment: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> pd.DataFrame:
        """
        Fetches intraday candles using Dhan's intraday endpoint.
        Falls back to get_candles if intraday endpoint unavailable.
        """
        # Try fetching with INDEX as default instrument_type
        try:
            return self.get_candles(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type="INDEX",
                interval=interval,
                from_date=from_date,
                to_date=to_date
            )
        except DataFetchError as e:
            logger.warning(f"Intraday fetch with INDEX failed: {e}. Falling back to EQUITY...")
            return self.get_candles(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type="EQUITY",
                interval=interval,
                from_date=from_date,
                to_date=to_date
            )
