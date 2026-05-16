class DataFetchError(Exception):
    """Raised when data cannot be fetched from the API after retries."""
    pass

class InsufficientDataError(Exception):
    """Raised when the fetched data is insufficient for analysis (e.g. not enough candles)."""
    pass