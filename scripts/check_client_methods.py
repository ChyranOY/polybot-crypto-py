
import logging
import sys

# Mocking logging to avoid errors during import if not configured
logging.basicConfig(level=logging.INFO)

try:
    from py_clob_client_v2.client import ClobClient
    print("py_clob_client_v2 is installed.")
    print("Methods of ClobClient:")
    for method in dir(ClobClient):
        if not method.startswith("_"):
            print(method)
except ImportError:
    print("py_clob_client_v2 is NOT installed.")
    # Fallback to checking local client.py if needed, but the user implies live mode works, so the lib must be there.
    sys.path.append(".")
    from src.client import ClobClient as LocalClobClient
    print("Checking local ClobClient:")
    for method in dir(LocalClobClient):
        if not method.startswith("_"):
            print(method)
