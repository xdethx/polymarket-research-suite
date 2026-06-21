"""
CLOB prices-history client for Polymarket weather markets.

Read-only.  Fetches the hourly price path for a YES token from the CLOB API.

W0-confirmed endpoint:
    GET https://clob.polymarket.com/prices-history
      ?market={clobTokenId}&interval=max&fidelity=60

Returns {"history": [{"t": unix_timestamp, "p": price_float}, ...]}
with hourly points back to market creation (confirmed: 33 hourly points
spanning ~31h for the London June 10 market, fetched 2026-06-09).

Usage
─────
  from weather_model.prices_history import price_path, latest_price

  path = price_path("372659...")   # list of (ts, price) or None
  p    = latest_price("372659...")  # float or None
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
_DEFAULT_TIMEOUT = 12  # seconds


# ---------------------------------------------------------------------------
# HTTP seam (monkeypatchable in tests)
# ---------------------------------------------------------------------------


def _http_get_json(url: str, params: Optional[Dict] = None) -> Optional[Any]:
    """Thin GET wrapper.  Returns parsed JSON or None on any error."""
    try:
        resp = requests.get(url, params=params, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("prices_history HTTP GET failed: %s — %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def price_path(token_id: str) -> Optional[List[Tuple[int, float]]]:
    """Fetch the full hourly price path for a YES token.

    Returns a list of (unix_timestamp, price_float) tuples ordered by time,
    or None on network/parse error.

    Parameters
    ----------
    token_id : CLOB YES token ID (clobTokenIds[0] from Gamma response).
    """
    params = {
        "market": token_id,
        "interval": "max",
        "fidelity": "60",
    }
    data = _http_get_json(_CLOB_HISTORY_URL, params)
    if data is None:
        return None

    history = data.get("history")
    if not history or not isinstance(history, list):
        return None

    result: List[Tuple[int, float]] = []
    for point in history:
        try:
            t = int(point["t"])
            p = float(point["p"])
            result.append((t, p))
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed points

    return result if result else None


def latest_price(token_id: str) -> Optional[float]:
    """Return the most recent price for a YES token.

    Returns a float in [0, 1], or None on error.
    """
    path = price_path(token_id)
    if path is None:
        return None
    return path[-1][1]
