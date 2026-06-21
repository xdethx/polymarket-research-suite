"""Tests for weather_model/clob_book.py — pure-function helpers only, no network."""
import pytest

from weather_model.clob_book import (
    BookLevel,
    OrderBook,
    best_ask,
    best_bid,
    fillable_buy_size,
    fillable_sell_size,
)


def _make_book(bids=None, asks=None) -> OrderBook:
    """Convenience factory.  Sorts bids descending, asks ascending."""
    bid_levels = sorted(
        [BookLevel(price=p, size=s) for p, s in (bids or [])],
        key=lambda lv: lv.price,
        reverse=True,
    )
    ask_levels = sorted(
        [BookLevel(price=p, size=s) for p, s in (asks or [])],
        key=lambda lv: lv.price,
    )
    return OrderBook(token_id="test", bids=bid_levels, asks=ask_levels)


# ---------------------------------------------------------------------------
# best_ask / best_bid
# ---------------------------------------------------------------------------


class TestBestPrices:
    def test_best_ask_returns_lowest(self):
        book = _make_book(asks=[(0.10, 5.0), (0.08, 3.0), (0.12, 2.0)])
        assert best_ask(book) == pytest.approx(0.08)

    def test_best_bid_returns_highest(self):
        book = _make_book(bids=[(0.05, 5.0), (0.07, 3.0), (0.06, 2.0)])
        assert best_bid(book) == pytest.approx(0.07)

    def test_best_ask_empty_book_returns_none(self):
        book = _make_book()
        assert best_ask(book) is None

    def test_best_bid_empty_book_returns_none(self):
        book = _make_book()
        assert best_bid(book) is None


# ---------------------------------------------------------------------------
# fillable_buy_size — sweep ask side up to limit_price
# ---------------------------------------------------------------------------


class TestFillableBuySize:
    def test_single_level_fillable(self):
        book = _make_book(asks=[(0.05, 10.0)])
        size, vwap = fillable_buy_size(book, limit_price=0.10)
        assert size == pytest.approx(10.0)
        assert vwap == pytest.approx(0.05)

    def test_partial_fill_respects_limit(self):
        # Asks at 0.05 (fillable) and 0.12 (above limit)
        book = _make_book(asks=[(0.05, 10.0), (0.12, 5.0)])
        size, vwap = fillable_buy_size(book, limit_price=0.10)
        assert size == pytest.approx(10.0)   # only the 0.05 level
        assert vwap == pytest.approx(0.05)

    def test_multi_level_vwap(self):
        # Two fillable levels: 5 tokens @ 0.04 + 5 tokens @ 0.06
        book = _make_book(asks=[(0.04, 5.0), (0.06, 5.0)])
        size, vwap = fillable_buy_size(book, limit_price=0.10)
        assert size == pytest.approx(10.0)
        assert vwap == pytest.approx(0.05)   # (0.04*5 + 0.06*5) / 10

    def test_no_asks_at_or_below_limit(self):
        book = _make_book(asks=[(0.15, 10.0)])
        size, vwap = fillable_buy_size(book, limit_price=0.10)
        assert size == 0.0
        assert vwap == 0.0

    def test_empty_book(self):
        book = _make_book()
        size, vwap = fillable_buy_size(book, limit_price=0.50)
        assert size == 0.0
        assert vwap == 0.0

    def test_exact_limit_price_included(self):
        """A level priced exactly at limit_price is included (≤ not <)."""
        book = _make_book(asks=[(0.10, 7.0)])
        size, vwap = fillable_buy_size(book, limit_price=0.10)
        assert size == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# fillable_sell_size — sweep bid side at or above limit_price
# ---------------------------------------------------------------------------


class TestFillableSellSize:
    def test_single_level_fillable(self):
        book = _make_book(bids=[(0.70, 10.0)])
        size, vwap = fillable_sell_size(book, limit_price=0.60)
        assert size == pytest.approx(10.0)
        assert vwap == pytest.approx(0.70)

    def test_partial_fill_below_limit_excluded(self):
        book = _make_book(bids=[(0.70, 10.0), (0.50, 5.0)])
        size, vwap = fillable_sell_size(book, limit_price=0.60)
        assert size == pytest.approx(10.0)   # 0.50 level excluded

    def test_no_bids_at_or_above_limit(self):
        book = _make_book(bids=[(0.40, 10.0)])
        size, vwap = fillable_sell_size(book, limit_price=0.50)
        assert size == 0.0
        assert vwap == 0.0

    def test_empty_book(self):
        book = _make_book()
        size, vwap = fillable_sell_size(book, limit_price=0.50)
        assert size == 0.0
        assert vwap == 0.0


# ---------------------------------------------------------------------------
# OrderBook sort order preserved
# ---------------------------------------------------------------------------


class TestOrderBookSortOrder:
    def test_asks_ascending(self):
        book = _make_book(asks=[(0.10, 1.0), (0.05, 1.0), (0.08, 1.0)])
        prices = [lv.price for lv in book.asks]
        assert prices == sorted(prices)

    def test_bids_descending(self):
        book = _make_book(bids=[(0.05, 1.0), (0.09, 1.0), (0.07, 1.0)])
        prices = [lv.price for lv in book.bids]
        assert prices == sorted(prices, reverse=True)
