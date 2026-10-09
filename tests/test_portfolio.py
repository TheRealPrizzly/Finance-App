from datetime import date

import pytest

from app import portfolio as pf
from conftest import FakeMarket


def txn(id, d, kind, symbol="", quantity=0.0, price=0.0, costs=0.0, net=0.0, currency="CAD", account=1):
    return pf.Txn(id, date.fromisoformat(d), kind, symbol, quantity, price, costs, net, currency, account)


TRADES = [
    txn(1, "2025-01-02", pf.DEPOSIT, net=5000),
    txn(2, "2025-01-03", pf.BUY, "VFV.TO", 10, 100, 10, -1010),
    txn(3, "2025-02-03", pf.BUY, "VFV.TO", 10, 120, 10, -1210),
    txn(4, "2025-03-03", pf.SELL, "VFV.TO", -5, 130, 10, 640),
    txn(5, "2025-03-28", pf.DIVIDEND, "VFV.TO", net=12.5),
]


def test_average_cost_and_realized_gain():
    fx = pf.FxBook(FakeMarket(), "CAD", date(2025, 1, 1))
    lot = pf.build_lots(TRADES, fx)[(1, "VFV.TO")]
    assert lot.quantity == pytest.approx(15)
    assert lot.cost_native == pytest.approx(1665)  # avg cost 111/share
    assert lot.realized_base == pytest.approx(85)  # 640 proceeds - 555 cost
    assert lot.dividends_base == pytest.approx(12.5)


def test_snapshot_totals():
    market = FakeMarket(quotes={"VFV.TO": (150.0, 148.0, "CAD")})
    snap = pf.snapshot(TRADES, {1: "TFSA · 1"}, market)
    t = snap["totals"]
    assert not snap["holdings_only"]
    assert t["market_value"] == pytest.approx(2250)
    assert t["cash"] == pytest.approx(5000 - 1010 - 1210 + 640 + 12.5)
    assert t["total_value"] == pytest.approx(2250 + 3432.5)
    assert t["unrealized"] == pytest.approx(585)
    assert t["day_change"] == pytest.approx(30)
    assert t["day_change_pct"] == pytest.approx(30 / (t["total_value"] - 30))
    assert t["net_contributions"] == pytest.approx(5000)
    assert t["total_gain"] == pytest.approx(585 + 85 + 12.5)
    assert t["dividends"] == pytest.approx(12.5)
    holding = snap["holdings"][0]
    assert holding["avg_cost"] == pytest.approx(111)
    assert holding["weight"] == pytest.approx(2250 / t["total_value"])
    assert snap["cash"] == [{"account_id": 1, "account": "TFSA · 1", "currency": "CAD",
                             "amount": pytest.approx(3432.5), "amount_base": pytest.approx(3432.5)}]


def test_foreign_holding_uses_purchase_date_fx_for_book_cost():
    market = FakeMarket(
        quotes={"AAPL": (250.0, 250.0, "USD")},
        fx={"USD": (1.40, {date(2025, 1, 2): 1.30})},
    )
    txns = [txn(1, "2025-01-02", pf.BUY, "AAPL", 10, 200, 0, -2000, "USD")]
    snap = pf.snapshot(txns, {}, market)
    h = snap["holdings"][0]
    assert h["market_value"] == pytest.approx(3500)  # 10 * 250 * 1.40
    assert h["book_cost"] == pytest.approx(2600)  # 2000 * 1.30
    assert h["unrealized"] == pytest.approx(900)


def test_holdings_only_mode_treats_buys_as_contributions():
    market = FakeMarket(quotes={"XEQT": (35.0, 34.0, "CAD")})
    txns = [txn(1, "2025-01-02", pf.BUY, "XEQT", 100, 30, 0, -3000)]
    snap = pf.snapshot(txns, {}, market)
    assert snap["holdings_only"]
    assert snap["totals"]["total_value"] == pytest.approx(3500)
    assert snap["totals"]["net_contributions"] == pytest.approx(3000)
    assert snap["totals"]["total_gain"] == pytest.approx(500)


def test_missing_quote_falls_back_to_last_trade_price():
    txns = [txn(1, "2025-01-02", pf.BUY, "PRIVATE", 10, 12, 0, -120)]
    snap = pf.snapshot(txns, {}, FakeMarket())
    assert snap["holdings"][0]["price"] == 12
    assert not snap["holdings"][0]["live"]
    assert snap["warnings"]


def test_sector_allocation_looks_through_etfs_and_includes_cash():
    market = FakeMarket(
        quotes={"VFV.TO": (100.0, 100.0, "CAD")},
        sectors={"VFV.TO": {"Technology": 0.4, "Financial Services": 0.6}},
    )
    txns = [txn(1, "2025-01-02", pf.DEPOSIT, net=1500), txn(2, "2025-01-02", pf.BUY, "VFV.TO", 10, 100, 0, -1000)]
    sectors = {s["sector"]: s["value"] for s in pf.snapshot(txns, {}, market)["sectors"]}
    assert sectors == {"Financial Services": pytest.approx(600), "Cash": pytest.approx(500), "Technology": pytest.approx(400)}


def test_history_time_weighted_return_ignores_deposits():
    days = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]  # Mon-Wed
    market = FakeMarket(
        quotes={"VFV.TO": (110.0, 110.0, "CAD")},
        history={"VFV.TO": ("CAD", dict(zip(days, [100.0, 110.0, 110.0])))},
    )
    txns = [
        txn(1, "2025-01-06", pf.DEPOSIT, net=1000),
        txn(2, "2025-01-06", pf.BUY, "VFV.TO", 10, 100, 0, -1000),
        txn(3, "2025-01-08", pf.DEPOSIT, net=1100),
    ]
    h = pf.history(txns, market, benchmark="VFV.TO", today=days[-1])
    values = [p["value"] for p in h["points"]]
    assert values == pytest.approx([1000, 1100, 2200])
    assert h["points"][-1]["twr"] == pytest.approx(0.10)
    assert h["points"][-1]["contributions"] == pytest.approx(2100)
    # Same contributions invested in the benchmark (here the same ETF) end at the same value.
    assert h["points"][-1]["benchmark_value"] == pytest.approx(2200)
    assert h["points"][-1]["benchmark_twr"] == pytest.approx(0.10)
    since_inception = next(p for p in h["periods"] if p["label"] == "All")
    assert since_inception["portfolio"] == pytest.approx(0.10)


def test_history_skips_weekends_but_keeps_their_flows():
    market = FakeMarket(history={"VFV.TO": ("CAD", {date(2025, 1, 3): 100.0, date(2025, 1, 6): 100.0})})
    txns = [txn(1, "2025-01-03", pf.DEPOSIT, net=100), txn(2, "2025-01-04", pf.DEPOSIT, net=50)]  # Fri, Sat
    h = pf.history(txns, market, today=date(2025, 1, 6))
    assert [p["date"] for p in h["points"]] == ["2025-01-03", "2025-01-06"]
    assert h["points"][-1]["value"] == pytest.approx(150)
    assert h["points"][-1]["twr"] == pytest.approx(0.0)


def test_compute_net_amount():
    assert pf.compute_net_amount(pf.BUY, 10, 5, 1) == -51
    assert pf.compute_net_amount(pf.SELL, 10, 5, 1) == 49
    assert pf.compute_net_amount(pf.WITHDRAWAL, amount=100) == -100
    assert pf.compute_net_amount(pf.DEPOSIT, amount=-100) == 100
    assert pf.compute_net_amount(pf.DIVIDEND, amount=-3) == -3  # withholding tax
