import pytest
from tests.test_signal_engine import test_signal_engine

try:
    test_signal_engine()
except Exception as e:
    import traceback
    traceback.print_exc()
