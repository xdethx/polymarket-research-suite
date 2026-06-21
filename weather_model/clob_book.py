"""
Live CLOB order-book client for Polymarket weather fillability analysis.

Fetches the resting order book for a single token_id from the public CLOB
endpoint.  Returns an ``OrderBook`` dataclass with typed bid/ask levels and a
helper to compute total fillable size at or better than a limit price.

Read-only HTTP; no authentication required; no orders are placed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests

_TIMEOUT = 12
_CLOB_BOOK_URL = "https://clob.polymarket.com/book"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BookLevel:
    """Single resting level in the order book."""
    price: float   # in [0, 1]
    size: float    # tokens available at this level


@dataclass
class OrderBook:
    """Snapshot of resting bids and asks for one CLOB token.

    bids: sorted descending by price (highest bid first)
    asks: sorted ascending by price (lowest ask first)
    """
    token_id: str
    bids: List[BookLevel] = field(default_factory=list)
    asks: List[BookLevel] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_book(token_id: str) -> Optional[OrderBook]:
    """Fetch the current order book for ``token_id`` from the CLOB.

    Returns ``OrderBook`` on success, ``None`` on any network or parse error.

    Args:
        token_id: YES or NO CLOB token ID (hex string).

    Returns:
        OrderBook with bids (descending) and asks (ascending) as BookLevel
        lists, or None on failure.
    """
    try:
        resp = requests.get(
            _CLOB_BOOK_URL,
            params={"token_id": token_id},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    def _parse_levels(raw: object) -> List[BookLevel]:
        if not isinstance(raw, list):
            return []
        levels: List[BookLevel] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                p = float(item.get("price", 0))
                s = float(item.get("size", 0))
                levels.append(BookLevel(price=p, size=s))
            except (ValueError, TypeError):
                continue
        return levels

    bids = _parse_levels(data.get("bids", []))
    asks = _parse_levels(data.get("asks", []))

    # Enforce sort order: bids descending, asks ascending
    bids.sort(key=lambda lv: lv.price, reverse=True)
    asks.sort(key=lambda lv: lv.price)

    return OrderBook(token_id=token_id, bids=bids, asks=asks)


# ---------------------------------------------------------------------------
# Fillability helpers
# ---------------------------------------------------------------------------


def fillable_buy_size(book: OrderBook, limit_price: float) -> Tuple[float, float]:
    """Total tokens buyable at ≤ ``limit_price`` and the VWAP cost.

    Sweeps the ask side of the book (people willing to sell to us) up to
    ``limit_price``.  Returns (total_tokens, vwap_price).

    Args:
        book:        Order book for the token we want to BUY.
        limit_price: Maximum price per token we're willing to pay (0–1).

    Returns:
        (total_tokens_available, vwap_price)
        where total_tokens_available is the sum of ask sizes at or below
        limit_price, and vwap_price is the volume-weighted average ask price.
        Returns (0.0, 0.0) if no asks ≤ limit_price.
    """
    eligible = [lv for lv in book.asks if lv.price <= limit_price]
    if not eligible:
        return 0.0, 0.0
    total_size = sum(lv.size for lv in eligible)
    total_cost = sum(lv.price * lv.size for lv in eligible)
    vwap = total_cost / total_size if total_size > 0 else 0.0
    return total_size, vwap


def fillable_sell_size(book: OrderBook, limit_price: float) -> Tuple[float, float]:
    """Total tokens sellable at ≥ ``limit_price`` and the VWAP received.

    Sweeps the bid side of the book (people willing to buy from us).

    Args:
        book:        Order book for the token we want to SELL.
        limit_price: Minimum price per token we'll accept (0–1).

    Returns:
        (total_tokens_available, vwap_price)
        Returns (0.0, 0.0) if no bids ≥ limit_price.
    """
    eligible = [lv for lv in book.bids if lv.price >= limit_price]
    if not eligible:
        return 0.0, 0.0
    total_size = sum(lv.size for lv in eligible)
    total_proceeds = sum(lv.price * lv.size for lv in eligible)
    vwap = total_proceeds / total_size if total_size > 0 else 0.0
    return total_size, vwap


def best_ask(book: OrderBook) -> Optional[float]:
    """Return the lowest resting ask price, or None if the book is empty."""
    return book.asks[0].price if book.asks else None


def best_bid(book: OrderBook) -> Optional[float]:
    """Return the highest resting bid price, or None if the book is empty."""
    return book.bids[0].price if book.bids else None
