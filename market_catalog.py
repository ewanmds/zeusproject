"""HIP-3 markets shared by the OBI bot and its dashboard."""

from typing import Any, Dict


PERP_DEXS = ["xyz", "mkts", "io"]

TOKENS: Dict[str, Dict[str, Any]] = {
    "SP500": {"symbol": "xyz:SP500", "half_spread_bps": 0.7,
              "session_utc": ((810, 1200),)},
    "XYZ100": {"symbol": "xyz:XYZ100"},
    "CL": {"symbol": "xyz:CL"},
    "BRENTOIL": {"symbol": "xyz:BRENTOIL"},
    "AAPL": {"symbol": "xyz:AAPL"},
    "JPY": {"symbol": "xyz:JPY"},
    "SPCX": {"symbol": "xyz:SPCX"},
    "GOLD": {"symbol": "xyz:GOLD"},
    "SILVER": {"symbol": "xyz:SILVER"},
    "NVDA": {"symbol": "xyz:NVDA"},
    "TSLA": {"symbol": "xyz:TSLA"},
    "US500": {"symbol": "mkts:US500", "session_utc": ((810, 1200),)},
    "SKHX": {"symbol": "xyz:SKHX", "half_spread_bps": 0.8},
    "SKHXUSD": {"symbol": "xyz:SKHX", "half_spread_bps": 0.8},
    # Entropy symbols use distinct UI keys: xyz:SNDK is a different market.
    "IO_SNDK": {"symbol": "io:SNDK"},
    "IO_ANTH": {"symbol": "io:ANTH"},
}
