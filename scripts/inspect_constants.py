import sys
try:
    from py_clob_client_v2.constants import POLYGON
    print("Polygon Constants:")
    for k, v in POLYGON.items():
        print(f"{k}: {v}")
except ImportError:
    print("py_clob_client_v2.constants.POLYGON not found")
except Exception as e:
    print(f"Error: {e}")

