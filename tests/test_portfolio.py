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
    assert lot.cost_native == pytest.approx(1650)  # avg price paid 110/share; commissions excluded
    assert lot.realized_base == pytest.approx(-20 + 90)  # both buy commissions + (640 proceeds - 550 cost)
    assert lot.dividends_base == pytest.approx(12.5)


def test_book_cost_excludes_commissions():
    # AMZN in the TFSA export: 8 buys, 34.65 USD of commissions, then 4 shares sold
    fx = pf.FxBook(FakeMarket(), "USD", date(2022, 1, 1))
    buys = [("2022-12-06", 3, 89.0, 4.95, -271.95), ("2023-09-22", 1, 129.5389, 4.95, -134.49),
            ("2023-10-05", 2, 124.5363, 4.95, -254.02), ("2024-01-10", 2, 153.8179, 4.95, -312.59),
            ("2024-04-11", 3, 187.098, 4.95, -566.24), ("2024-08-02", 3, 163.359, 4.95, -495.03),
            ("2024-08-06", 2, 164.7878, 4.95, -334.53), ("2025-12-16", 3, 222.2792, 0, -666.84)]
    txns = [txn(i, d, pf.BUY, "AMZN", q, p, c, n, "USD") for i, (d, q, p, c, n) in enumerate(buys)]
    txns.append(txn(99, "2026-05-13", pf.SELL, "AMZN", -4, 265.765, 0.03, 1063.03, "USD"))
    lot = pf.build_lots(txns, fx)[(1, "AMZN")]
    assert lot.quantity == pytest.approx(15)
    assert lot.avg_cost == pytest.approx(157.949, abs=5e-4)
    assert lot.cost_native == pytest.approx(2369.24, abs=0.005)
    paid = 3035.69  # what the 19 shares cost in cash, commissions included
    avg = 3001.0315 / 19  # price paid per share
    assert lot.realized_base == pytest.approx(-(paid - 19 * avg) + 1063.03 - 4 * avg)
    # no money goes missing: realized + unrealized at any price P equals cash in - cash out + 15 x P,
    # i.e. realized - book cost = sale proceeds - cash paid
    assert lot.realized_base - lot.cost_native == pytest.approx(1063.03 - paid)


def test_same_day_sell_listed_before_buy_uses_the_buy():
    # newest-first export: the sell of 2 appears before the buy of 3 it was taken from
    fx = pf.FxBook(FakeMarket(), "USD", date(2026, 1, 1))
    lot = pf.build_lots([
        txn(1, "2026-02-06", pf.SELL, "MSFT", -2, 400.45, 0, 800.90, "USD"),
        txn(2, "2026-02-06", pf.BUY, "MSFT", 3, 400.255, 0, -1200.77, "USD"),
    ], fx)[(1, "MSFT")]
    assert lot.quantity == pytest.approx(1)
    assert lot.cost_native == pytest.approx(400.255)  # price paid for the share left
    assert lot.realized_base == pytest.approx(800.90 - 2 * 400.255 - (1200.77 - 3 * 400.255))


def test_snapshot_totals():
    market = FakeMarket(quotes={"VFV.TO": (150.0, 148.0, "CAD")})
    snap = pf.snapshot(TRADES, {1: "TFSA · 1"}, market)
    t = snap["totals"]
    assert not snap["holdings_only"]
    assert t["market_value"] == pytest.approx(2250)
    assert t["cash"] == pytest.approx(5000 - 1010 - 1210 + 640 + 12.5)
    assert t["total_value"] == pytest.approx(2250 + 3432.5)
    assert t["unrealized"] == pytest.approx(600)  # 15 x (150 - 110 price paid)
    assert t["day_change"] == pytest.approx(30)
    assert t["day_change_pct"] == pytest.approx(30 / (t["total_value"] - 30))
    assert t["net_contributions"] == pytest.approx(5000)
    assert t["total_gain"] == pytest.approx(585 + 85 + 12.5)
    assert t["dividends"] == pytest.approx(12.5)
    holding = snap["holdings"][0]
    assert holding["avg_cost"] == pytest.approx(110)  # price paid; commissions are not book cost
    assert holding["book_cost_native"] == pytest.approx(1650)
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
    assert h["book_cost_native"] == pytest.approx(2000)  # what the broker shows, in USD
    assert h["unrealized"] == pytest.approx(900)


def test_reverse_split_keeps_cost_and_reported_quantity():
    # 1:10 reverse split; the imported history is missing 10 of the 817 shares converted
    txns = [
        txn(1, "2023-08-01", pf.BUY, "TMF", 807, 8, 0, -6456),
        txn(2, "2023-12-04", pf.SPLIT, "TMF", 81),
        txn(3, "2023-12-04", pf.SPLIT, "TMF", -817),
    ]
    fx = pf.FxBook(FakeMarket(), "CAD", date(2023, 7, 1))
    lot = pf.build_lots(txns, fx)[1, "TMF"]
    assert lot.quantity == pytest.approx(81)
    assert lot.cost_native == pytest.approx(6456)
    assert lot.last_price == pytest.approx(8 * 817 / 81)
    sold = pf.build_lots(txns + [txn(4, "2024-03-11", pf.SELL, "TMF", -81, 60, 0, 4860)], fx)[1, "TMF"]
    assert sold.quantity == 0
    assert sold.realized_base == pytest.approx(4860 - 6456)


def test_split_under_a_new_symbol_moves_the_position():
    txns = [
        txn(1, "2024-03-12", pf.BUY, "OLD", 9, 100, 0, -900),
        txn(2, "2024-12-04", pf.SPLIT, "NEW", 36),
        txn(3, "2024-12-04", pf.SPLIT, "OLD", -9),
    ]
    lots = pf.build_lots(txns, pf.FxBook(FakeMarket(), "CAD", date(2024, 3, 1)))
    assert lots[1, "OLD"].quantity == 0
    assert lots[1, "NEW"].quantity == pytest.approx(36)
    assert lots[1, "NEW"].cost_native == pytest.approx(900)


def test_history_values_holdings_after_a_reverse_split():
    days = [date(2023, 12, 1), date(2023, 12, 4)]  # Fri, Mon
    market = FakeMarket(history={"TMF": ("CAD", {days[0]: 8.0, days[1]: 80.0})})  # as-traded closes
    txns = [
        txn(1, "2023-12-01", pf.BUY, "TMF", 807, 8, 0, -6456),
        txn(2, "2023-12-04", pf.SPLIT, "TMF", 81),
        txn(3, "2023-12-04", pf.SPLIT, "TMF", -817),
    ]
    h = pf.history(txns, market, today=days[1])
    assert [p["value"] for p in h["points"]] == pytest.approx([807 * 8, 81 * 80])


IN_KIND_WITHDRAWAL = [
    txn(1, "2021-11-05", pf.DEPOSIT, net=1200),
    txn(2, "2021-11-08", pf.BUY, "V", 5, 200, 0, -1000),
    txn(3, "2021-11-23", pf.TRANSFER, "V", -5),  # imported "WDR" row: shares out, no price, no cash
]


def test_in_kind_withdrawal_removes_shares_and_counts_their_value_as_withdrawn():
    market = FakeMarket(quotes={"V": (300.0, 300.0, "CAD")}, history={"V": ("CAD", {date(2021, 11, 23): 210.0})})
    snap = pf.snapshot(IN_KIND_WITHDRAWAL, {}, market)
    t = snap["totals"]
    assert snap["holdings"] == []
    assert t["total_value"] == pytest.approx(200)  # leftover cash only
    assert t["net_contributions"] == pytest.approx(1200 - 5 * 210)
    assert t["total_gain"] == pytest.approx(5 * 10)  # bought at 200, worth 210 when withdrawn
    assert t["realized"] == pytest.approx(0)


def test_history_treats_in_kind_withdrawal_as_money_out():
    days = [date(2021, 11, 22), date(2021, 11, 23), date(2021, 11, 24)]  # Mon to Wed
    market = FakeMarket(history={"V": ("CAD", {days[0]: 200.0, days[1]: 210.0, days[2]: 210.0})})
    txns = [txn(1, "2021-11-22", pf.DEPOSIT, net=1000), txn(2, "2021-11-22", pf.BUY, "V", 5, 200, 0, -1000),
            txn(3, "2021-11-24", pf.TRANSFER, "V", -5)]
    h = pf.history(txns, market, today=days[2])
    assert [p["value"] for p in h["points"]] == pytest.approx([1000, 1050, 0])
    assert [p["contributions"] for p in h["points"]] == pytest.approx([1000, 1000, -50])
    assert h["points"][-1]["twr"] == pytest.approx(0.05)  # the 5% gain, not a 100% loss


def test_in_kind_deposit_adds_shares_at_market_value():
    # RRSP export: 5 V shares arrive with no price or cash, then are sold
    market = FakeMarket(history={"V": ("USD", {date(2021, 11, 23): 210.0})}, fx={"USD": (1.0, {})})
    txns = [
        txn(1, "2021-11-15", pf.DEPOSIT, net=200, currency="CAD"),
        txn(2, "2021-11-23", pf.TRANSFER, "V", 5, currency="USD"),
        txn(3, "2022-04-28", pf.SELL, "V", -5, 222, 4.96, 1105.04, "USD"),
    ]
    snap = pf.snapshot(txns, {}, market)
    assert snap["holdings"] == []  # no negative position left behind
    assert snap["closed"] == [{"symbol": "V", "realized": pytest.approx(1105.04 - 5 * 210), "dividends": 0.0}]
    assert snap["totals"]["net_contributions"] == pytest.approx(200 + 5 * 210)
    assert not snap["warnings"]


def test_in_kind_deposit_without_a_price_is_flagged():
    snap = pf.snapshot([txn(1, "2021-11-23", pf.TRANSFER, "PRIVATE", 5)], {}, FakeMarket())
    assert any("PRIVATE transfer" in w for w in snap["warnings"])


# TFSA export starting 2021-09-24: VOO and part of BABA were bought before its first row
BEFORE_HISTORY = [
    txn(1, "2021-09-24", pf.DEPOSIT, net=650),
    txn(2, "2021-09-24", pf.BUY, "BABA", 1, 145, 0, -145, "USD"),
    txn(3, "2021-10-04", pf.DIVIDEND, "VOO", net=6.68, currency="USD"),
    txn(4, "2021-10-25", pf.SELL, "VOO", -6, 417.29, 4.97, 2498.78, "USD"),
    txn(5, "2022-12-22", pf.SELL, "BABA", -4, 86.83, 0, 347.32, "USD"),
]
BEFORE_MARKET = dict(
    history={"VOO": ("USD", {date(2021, 9, 24): 410.0, date(2021, 10, 25): 417.29}),
             "BABA": ("USD", {date(2021, 9, 24): 150.0, date(2022, 12, 22): 86.83})},
    fx={"USD": (1.0, {})},
)


def test_shares_sold_before_any_purchase_are_held_from_the_first_date():
    txns = pf.with_opening_positions(BEFORE_HISTORY)
    opening = [(t.date, t.symbol, t.quantity) for t in txns if t.id < 0]
    assert opening == [(date(2021, 9, 24), "BABA", 3), (date(2021, 9, 24), "VOO", 6)]
    assert pf.with_opening_positions(txns) == txns  # applied once only


def test_snapshot_has_no_negative_holdings_and_values_opening_shares_at_market():
    snap = pf.snapshot(BEFORE_HISTORY, {}, FakeMarket(**BEFORE_MARKET), base="USD")
    assert snap["holdings"] == []
    closed = {c["symbol"]: c["realized"] for c in snap["closed"]}
    assert closed["VOO"] == pytest.approx(2498.78 - 6 * 410)
    assert closed["BABA"] == pytest.approx(347.32 - (145 + 3 * 150))
    assert snap["totals"]["net_contributions"] == pytest.approx(650 + 6 * 410 + 3 * 150)
    assert sum("assumed held from 2021-09-24" in w for w in snap["warnings"]) == 2


def test_history_never_holds_negative_shares():
    h = pf.history(BEFORE_HISTORY, FakeMarket(**BEFORE_MARKET), base="USD", today=date(2021, 10, 26))
    first = h["points"][0]
    assert first["value"] == pytest.approx(650 - 145 + 4 * 150 + 6 * 410)  # cash + opening shares
    assert first["contributions"] == pytest.approx(650 + 6 * 410 + 3 * 150)
    assert all(p["value"] >= 0 for p in h["points"])


def test_position_page_dates_opening_shares_from_the_account_history():
    detail = pf.position_detail(BEFORE_HISTORY, "VOO", {}, FakeMarket(**BEFORE_MARKET), base="USD",
                                today=date(2021, 10, 26))
    assert detail["holding"] is None
    assert detail["realized"] == pytest.approx(2498.78 - 6 * 410)  # cost at 2021-09-24, not 2021-10-04


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
    annual = next(p for p in h["periods"] if p["label"] == "Annualized")
    assert annual["portfolio"] is None  # under a year of history: not annualized


def test_period_returns_annualizes_since_inception():
    points = [
        {"date": "2023-01-02", "twr": 0.0, "benchmark_twr": 0.0},
        {"date": "2025-01-01", "twr": 0.21, "benchmark_twr": 0.44},  # 730 days later
    ]
    periods = pf.period_returns(points, date(2025, 1, 1))
    assert [p["label"] for p in periods][-2:] == ["All", "Annualized"]
    annual = periods[-1]
    years = 730 / 365.25
    assert annual["portfolio"] == pytest.approx(1.21 ** (1 / years) - 1)
    assert annual["benchmark"] == pytest.approx(1.44 ** (1 / years) - 1)


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


def test_unadjust_splits_restores_as_traded_prices():
    from app.market import unadjust_splits

    # 1:4 reverse split on 2026-05-05: Yahoo shows the 2023 close of 13.32 as 53.28
    closes = {date(2023, 4, 19): 53.28, date(2026, 5, 4): 50.0, date(2026, 5, 5): 50.0}
    out = unadjust_splits(closes, [(date(2026, 5, 5), 1 / 4)])
    assert out == {date(2023, 4, 19): pytest.approx(13.32), date(2026, 5, 4): 12.5, date(2026, 5, 5): 50.0}
    # splits compound: a close before both a 2:1 and a 1:4 split is scaled by 2 × 1/4
    out = unadjust_splits({date(2020, 1, 1): 10.0}, [(date(2021, 1, 1), 2.0), (date(2026, 5, 5), 1 / 4)])
    assert out[date(2020, 1, 1)] == pytest.approx(5.0)
