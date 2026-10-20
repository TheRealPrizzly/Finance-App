from datetime import date

import pytest

from app import company as co

# A company that reports in USD and trades in CAD (USD -> CAD at 1.30, 1.35 and 1.40 today),
# with two fiscal years, five quarters and a TTM to March 2026.
FUNDAMENTALS = {
    "currency": "USD",
    "annual": {
        "TotalRevenue": {"2024-12-31": 1000, "2025-12-31": 1200},
        "NetIncome": {"2024-12-31": 100, "2025-12-31": 150},
        "NetIncomeCommonStockholders": {"2024-12-31": 100, "2025-12-31": 150},
        "EBIT": {"2024-12-31": 200, "2025-12-31": 240},
        "EBITDA": {"2024-12-31": 260, "2025-12-31": 300},
        "OperatingCashFlow": {"2024-12-31": 220, "2025-12-31": 260},
        "FreeCashFlow": {"2024-12-31": 120, "2025-12-31": 180},
        "CashDividendsPaid": {"2024-12-31": -30, "2025-12-31": -45},
        "TaxRateForCalcs": {"2024-12-31": 0.2, "2025-12-31": 0.25},
        "TotalDebt": {"2024-12-31": 500, "2025-12-31": 400},
        "CashCashEquivalentsAndShortTermInvestments": {"2024-12-31": 100, "2025-12-31": 200},
        "OrdinarySharesNumber": {"2024-12-31": 110, "2025-12-31": 100},
        "InvestedCapital": {"2024-12-31": 1000, "2025-12-31": 1200},
        "StockholdersEquity": {"2024-12-31": 500, "2025-12-31": 800},
        "TotalAssets": {"2024-12-31": 2000, "2025-12-31": 2200},
        "CurrentLiabilities": {"2024-12-31": 400, "2025-12-31": 600},
        "BasicAverageShares": {"2024-12-31": 111, "2025-12-31": 103},
        "DilutedAverageShares": {"2024-12-31": 112, "2025-12-31": 104},
        "DilutedEPS": {"2024-12-31": 0.89, "2025-12-31": 1.44},
    },
    "quarterly": {
        "TotalRevenue": {"2025-03-31": 280, "2025-06-30": 290, "2025-09-30": 310, "2025-12-31": 320, "2026-03-31": 330},
        "NetIncome": {"2025-03-31": 30, "2025-06-30": 35, "2025-09-30": 40, "2025-12-31": 45, "2026-03-31": 50},
        "OrdinarySharesNumber": {"2025-12-31": 100, "2026-03-31": 100},
        "TotalDebt": {"2026-03-31": 400},
        "CashCashEquivalentsAndShortTermInvestments": {"2026-03-31": 200},
    },
    "trailing": {
        "TotalRevenue": {"2026-03-31": 1250},
        "NetIncome": {"2026-03-31": 170},
        "NetIncomeCommonStockholders": {"2026-03-31": 170},
        "EBIT": {"2026-03-31": 250},
        "FreeCashFlow": {"2026-03-31": 200},
        "CashDividendsPaid": {"2026-03-31": -48},
        "InterestExpense": {"2026-03-31": 20},
    },
}
CLOSES = [(date(2024, 12, 31), 14.0), (date(2025, 12, 31), 21.0), (date(2026, 3, 31), 25.0)]
FX = [(date(2024, 12, 31), 1.30), (date(2025, 12, 31), 1.35)]
TODAY = date(2026, 6, 15)
PAID = [(date(2021, 3, 1), 0.10), (date(2021, 9, 1), 0.10), (date(2022, 3, 1), 0.10), (date(2022, 9, 1), 0.12),
        (date(2023, 3, 1), 0.12), (date(2023, 9, 1), 0.14), (date(2024, 3, 1), 0.14), (date(2024, 9, 1), 0.15),
        (date(2025, 2, 10), 0.15), (date(2025, 8, 10), 0.16), (date(2025, 12, 30), 0.16),  # an ex-date slipped into 2025
        (date(2026, 2, 10), 0.16)]
FACTS = {
    "financial_currency": "USD", "shares_outstanding": 100,
    "insiders": 0.10, "institutions": 0.95, "institutions_float": 1.05, "institutions_count": 812,
    "top_institutions": [{"name": "Big Fund Co", "pct": 0.08}], "top_funds": [],
    "insider_holders": [{"name": "A", "shares": 1, "direct": 1, "indirect": None},
                        {"name": "B", "shares": 5, "direct": None, "indirect": 5}],
    "dividend": {"trailing_yield": 0.017, "five_year_yield": 0.02, "payout_ratio": 0.4, "ex_date": "2026-02-10"},
}
STATS = {"currency": "CAD", "market_cap": 2800, "price": 28.0, "beta": 1.2, "dividend": 0.64, "yield": 0.0229}


@pytest.fixture
def report():
    return co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY)


def table(report, freq, name):
    t = report["financials"][freq][name]
    return [c["kind"][0] + c["date"] for c in t["columns"]], {r["key"]: r["values"] for r in t["rows"]}


def test_statements_have_a_ttm_column_and_leave_out_empty_lines(report):
    cols, rows = table(report, "annual", "income")
    assert cols == ["a2024-12-31", "a2025-12-31", "t2026-03-31"]
    assert rows["TotalRevenue"] == [1000, 1200, 1250]
    assert "CostOfRevenue" not in rows  # no figures at all
    cols, rows = table(report, "annual", "balance")
    assert cols == ["a2024-12-31", "a2025-12-31"]  # balances have no TTM
    cols, _ = table(report, "quarterly", "income")
    assert cols == ["q2025-03-31", "q2025-06-30", "q2025-09-30", "q2025-12-31", "q2026-03-31"]


def test_margins(report):
    cols, rows = table(report, "annual", "margins")
    assert cols == ["a2024-12-31", "a2025-12-31", "t2026-03-31"]
    assert rows["net"] == [0.1, 0.125, 170 / 1250]
    assert rows["fcf"][:2] == [0.12, 0.15]
    assert table(report, "quarterly", "margins")[1]["net"][-1] == 50 / 330


def test_valuation_scales_todays_market_cap_and_converts_currencies(report):
    cols, rows = table(report, "annual", "valuation")
    assert cols == ["a2024-12-31", "a2025-12-31", "c2026-06-15"]
    # Today's 2,800 at half the price and 10% more shares; debt less cash at that year-end's rate.
    assert rows["market_cap"] == [pytest.approx(1540), pytest.approx(2100), 2800]
    assert rows["ev"] == [pytest.approx(1540 + 400 * 1.30), pytest.approx(2100 + 200 * 1.35), pytest.approx(2800 + 200 * 1.40)]
    assert rows["pe"][0] == pytest.approx(1540 / (100 * 1.30))
    assert rows["pe"][2] == pytest.approx(2800 / (170 * 1.40))  # today on the TTM
    assert rows["ev_ebitda"][0] == pytest.approx((1540 + 520) / (260 * 1.30))
    assert rows["fcf_yield"][1] == pytest.approx(180 * 1.35 / 2100)
    assert rows["ev_ebitda"][2] is None  # no trailing EBITDA


def test_quarterly_valuation_uses_the_last_four_quarters():
    report = co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY)
    cols, rows = table(report, "quarterly", "valuation")
    assert cols == ["q2025-12-31", "q2026-03-31", "c2026-06-15"]  # earlier quarters have no share count
    assert rows["ps"][0] == pytest.approx(2100 / (1200 * 1.35))  # four quarters added up
    assert rows["ps"][1] == pytest.approx(2500 / (1250 * 1.40))  # Yahoo's own TTM to that quarter


def test_negative_earnings_have_no_pe_but_a_negative_yield():
    losing = {**FUNDAMENTALS, "annual": {**FUNDAMENTALS["annual"],
                                          "NetIncomeCommonStockholders": {"2024-12-31": -50, "2025-12-31": 150}}}
    _, rows = table(co.company_report(losing, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY), "annual", "valuation")
    assert rows["pe"][0] is None
    assert rows["earnings_yield"][0] == pytest.approx(-50 * 1.30 / 1540)


def test_share_counts(report):
    assert report["shares"]["annual"] == [
        {"date": "2024-12-31", "source": "yahoo", "basic": 111, "diluted": 112, "basic_eps": None, "diluted_eps": 0.89},
        {"date": "2025-12-31", "source": "yahoo", "basic": 103, "diluted": 104, "basic_eps": None, "diluted_eps": 1.44},
    ]
    assert report["shares"]["quarterly"] == []
    assert report["shares"]["source"] == {"annual": "yahoo", "quarterly": "yahoo"}


def test_share_counts_go_back_ten_years_in_sec_filings():
    ends = [f"{y}-12-31" for y in range(2012, 2025)]
    filed = {"currency": "USD", "source": "sec", "quarterly": {}, "trailing": {},
             "annual": {"DilutedAverageShares": {d: 112.0 for d in ends}, "BasicAverageShares": {d: 111.0 for d in ends},
                        "DilutedEPS": {d: 0.5 for d in ends}}}
    shares = co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY, filed=filed)["shares"]
    assert shares["source"]["annual"] == "sec"
    assert [(p["date"][:4], p["source"]) for p in shares["annual"]] == [(str(y), "sec") for y in range(2016, 2025)] + [("2025", "yahoo")]
    assert shares["annual"][-1]["diluted"] == 104  # counts agree on 2024 (112 both), so Yahoo's FY2025 joins
    assert shares["annual"][-2]["diluted_eps"] == 0.5 and shares["annual"][-1]["diluted_eps"] is None  # EPS doesn't (0.89 against 0.5)


def test_dividends(report):
    dv = report["dividends"]
    assert [(y["year"], round(y["amount"], 2), y["partial"]) for y in dv["years"]] == [
        (2021, 0.2, False), (2022, 0.22, False), (2023, 0.26, False), (2024, 0.29, False), (2025, 0.47, False), (2026, 0.16, True)]
    assert dv["years"][1]["growth"] is None  # 2021 may be a partial first year
    assert dv["years"][-1]["growth"] is None  # this year isn't over
    assert dv["growth"]["1Y"] == pytest.approx(0.47 / 0.29 - 1)
    assert dv["growth"]["3Y"] == pytest.approx((0.47 / 0.22) ** (1 / 3) - 1)
    assert dv["growth"]["5Y"] is None
    assert dv["streak"] == 4  # raises in 2022-2025, counted on each year's last payment
    assert dv["ttm_amount"] == pytest.approx(0.48)
    assert (dv["forward_rate"], dv["forward_yield"], dv["payout_ratio"], dv["coverage"]) == (0.64, 0.0229, 0.4, 2.5)
    assert dv["fcf_payout"] == pytest.approx(0.24)
    assert dv["history"][1] == {"date": "2025-12-31", "paid": 45, "net_income": 150, "fcf": 180, "payout": 0.3, "fcf_payout": 0.25}


def test_no_dividends():
    dv = co.company_report(FUNDAMENTALS, None, {}, [], [], 1.0, [], None, TODAY)["dividends"]
    assert (dv["years"], dv["growth"], dv["streak"], dv["forward_yield"]) == ([], {}, 0, None)


def test_returns_on_capital_and_wacc(report):
    r = report["returns"]
    fy2025, ttm = r["points"][1], r["points"][2]
    assert (fy2025["roic"], fy2025["roe"], fy2025["roce"]) == (pytest.approx(240 * 0.75 / 1200), 150 / 800, 240 / 1600)
    assert ttm["kind"] == "ttm" and ttm["roic"] == pytest.approx(250 * 0.75 / 1200)
    w = r["wacc"]
    assert w["cost_of_equity"] == pytest.approx(0.04 + 1.2 * 0.05)
    assert w["cost_of_debt"] == pytest.approx(20 / 400) and w["debt_cost_reported"]
    assert w["weight_equity"] == pytest.approx(2800 / (2800 + 560))
    assert w["wacc"] == pytest.approx(2800 / 3360 * 0.10 + 560 / 3360 * 0.05 * 0.75)
    assert r["spread"] == pytest.approx(ttm["roic"] - w["wacc"])


def test_returns_on_capital_go_back_ten_years_in_sec_filings():
    ends = [f"{y}-12-31" for y in range(2012, 2025)]
    lines = {"EBIT": 100.0, "NetIncome": 60.0, "TaxRateForCalcs": 0.2, "InvestedCapital": 800.0,
             "StockholdersEquity": 600.0, "TotalAssets": 1500.0, "CurrentLiabilities": 500.0}
    filed = {"currency": "USD", "source": "sec", "quarterly": {}, "trailing": {},
             "annual": {key: {d: v for d in ends} for key, v in lines.items()}}
    r = co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY, filed=filed)["returns"]
    assert r["source"] == "sec"
    # the last ten years (2016-2024 filed, then Yahoo's newer FY2025), and Yahoo's TTM as the filings have none
    assert [(p["date"][:4], p["kind"], p["source"]) for p in r["points"]] == (
        [(str(y), "annual", "sec") for y in range(2016, 2025)] + [("2025", "annual", "yahoo"), ("2026", "ttm", "yahoo")])
    first = r["points"][0]
    assert (first["roic"], first["roe"], first["roce"]) == (pytest.approx(100 * 0.8 / 800), 0.1, 0.1)
    assert r["points"][-2]["roe"] == 150 / 800  # Yahoo's own figures for its years
    assert co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY)["returns"]["source"] == "yahoo"


def test_wacc_needs_a_beta():
    assert co.company_report(FUNDAMENTALS, FACTS, {**STATS, "beta": None}, CLOSES, PAID, 1.40, FX, 0.04, TODAY)["returns"]["wacc"] is None


def test_ownership(report):
    o = report["ownership"]
    assert o["public"] == 0.0  # Yahoo's 95% of institutions plus 10% of insiders tops 100%
    assert [(h["name"], h["pct"]) for h in o["insider_holders"]] == [("B", 0.05), ("A", 0.01)]


def test_a_fund_has_no_statements():
    report = co.company_report({"currency": None, "annual": {}, "quarterly": {}, "trailing": {}}, None,
                               {"currency": "USD"}, [], PAID, 1.0, [], 0.04, TODAY)
    assert report["financials"] is None and report["shares"] is None and report["returns"] is None
    assert report["ownership"] is None and report["dividends"]["years"]


def test_no_wacc_without_ebit():
    bank = {**FUNDAMENTALS, "annual": {k: v for k, v in FUNDAMENTALS["annual"].items() if k != "EBIT"},
            "trailing": {k: v for k, v in FUNDAMENTALS["trailing"].items() if k != "EBIT"}}
    r = co.company_report(bank, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY)["returns"]
    assert [p["roe"] for p in r["points"]] == [0.2, 150 / 800, pytest.approx(170 / 800)]
    assert all(p["roic"] is None and p["roce"] is None for p in r["points"])
    assert r["wacc"] is None and r["spread"] is None


def test_income_statement_comes_from_sec_filings_when_they_go_back_further():
    filed = {"currency": "USD", "source": "sec",
             "annual": {"TotalRevenue": {f"{y}-12-31": 100.0 + y for y in range(2012, 2026)}},
             "quarterly": {"TotalRevenue": {"2026-03-31": 300.0}}, "trailing": {"TotalRevenue": {"2026-03-31": 1300.0}}}
    fin = co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY, filed=filed)["financials"]
    income = fin["annual"]["income"]
    assert income["source"] == "sec" and income["currency"] == "USD"
    assert [c["date"][:4] for c in income["columns"]] == [str(y) for y in range(2016, 2026)] + ["2026"]  # 10 years and the TTM
    assert income["columns"][-1]["kind"] == "ttm" and income["rows"][0]["values"][-1] == 1300.0
    assert fin["quarterly"]["income"]["source"] == "yahoo"  # one filed quarter against Yahoo's five
    assert fin["annual"]["balance"]["columns"][0]["date"] == "2024-12-31"  # the other statements stay on Yahoo


def test_newer_yahoo_periods_follow_the_filings_where_the_sources_agree():
    years = range(2015, 2025)
    filed = {"currency": "USD", "source": "sec", "quarterly": {}, "trailing": {},
             "annual": {"TotalRevenue": {f"{y}-12-28": 1000.0 for y in years},  # 52-week years ending a few days early
                        "DilutedEPS": {f"{y}-12-28": 0.18 for y in years}}}
    income = co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY, filed=filed)["financials"]["annual"]["income"]
    assert [(c["date"], c["source"]) for c in income["columns"][-3:]] == [
        ("2024-12-28", "sec"), ("2025-12-31", "yahoo"), ("2026-03-31", "yahoo")]  # Yahoo's FY2025 and TTM
    rows = {r["key"]: r["values"] for r in income["rows"]}
    assert rows["TotalRevenue"][-2:] == [1200, 1250]  # revenue agrees on 2024 (1,000 both), so Yahoo's newer years join
    assert rows["DilutedEPS"][-3:] == [0.18, None, None]  # EPS doesn't (0.89 against 0.18): left blank


def test_a_filed_ttm_borrows_what_it_lacks_from_yahoos_for_the_same_quarter():
    years = range(2015, 2026)
    filed = {"currency": "USD", "source": "sec", "quarterly": {},
             "annual": {"NetIncome": {f"{y}-12-31": 150.0 for y in years}},
             "trailing": {"NetIncome": {"2026-03-28": 170.0}, "TotalRevenue": {"2026-03-28": 1300.0}}}
    income = co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY, filed=filed)["financials"]["annual"]["income"]
    rows = {r["key"]: r["values"] for r in income["rows"]}
    assert income["columns"][-1] == {"kind": "ttm", "date": "2026-03-28", "source": "sec"}
    assert rows["NetIncome"][-1] == 170.0 and rows["TotalRevenue"][-1] == 1300.0  # the filings' own
    assert rows["EBIT"][-1] == 250  # missing from the filings: Yahoo's TTM, as nothing contradicts it


def test_balance_sheet_and_cash_flow_come_from_sec_filings_too():
    ends = [f"{y}-12-31" for y in range(2013, 2026)]
    filed = {"currency": "USD", "source": "sec", "quarterly": {}, "trailing": {},
             "annual": {"TotalAssets": {d: 2000.0 for d in ends}, "OperatingCashFlow": {d: 220.0 for d in ends},
                        "TotalRevenue": {d: 1000.0 for d in ends}}}
    fin = co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY, filed=filed)["financials"]["annual"]
    for name in ("income", "balance", "cash"):
        assert fin[name]["source"] == "sec"
        assert [c["date"][:4] for c in fin[name]["columns"] if c["source"] == "sec"] == [str(y) for y in range(2016, 2026)]
    assert all(c["kind"] != "ttm" for c in fin["balance"]["columns"])  # balances have no TTM


def yahoo_estimates(eps_currency="USD", year_ago=1.44, first_year="2026-12-31"):
    point = lambda d, rev, eps, ago=None: {"date": d, "revenue": rev, "eps": eps, "revenue_analysts": 20,  # noqa: E731
                                           "eps_analysts": 25, "year_ago_eps": ago}
    return {"annual": [point(first_year, 1400, 1.80, year_ago), point("2027-12-31", 1600, 2.10, 1.80)],
            "quarterly": [point("2026-06-30", 340, 0.40), point("2026-09-30", 350, 0.45)],
            "revenue_currency": "USD", "eps_currency": eps_currency}


NASDAQ = {"annual": [{"month": m, "eps": e, "analysts": 4} for m, e in (("2026-12", 1.75), ("2027-12", 2.0), ("2028-12", 2.5), ("2029-12", 3.0))],
          "quarterly": [{"month": m, "eps": e, "analysts": 3} for m, e in (("2026-06", 0.41), ("2026-09", 0.44), ("2026-12", 0.5))]}


def with_estimates(analysts=None, forecast=NASDAQ):
    return co.company_report(FUNDAMENTALS, FACTS, STATS, CLOSES, PAID, 1.40, FX, 0.04, TODAY,
                             analysts=analysts or yahoo_estimates(), forecast=forecast)["financials"]


def test_estimates_follow_the_reported_periods_up_to_four_years():
    fin = with_estimates()
    cols, rows = table({"financials": fin}, "annual", "income")
    assert cols == ["a2024-12-31", "a2025-12-31", "t2026-03-31", "e2026-12-31", "e2027-12-31", "e2028-12-31", "e2029-12-31"]
    assert rows["TotalRevenue"][3:] == [1400, 1600, None, None]  # analysts' revenue only from Yahoo, for two years
    assert rows["DilutedEPS"][3:] == [1.80, 2.10, 2.5, 3.0]  # then Nasdaq's EPS, as it agrees with Yahoo's on 2026 and 2027
    assert rows["NetIncome"][3:] == [None] * 4
    est = fin["annual"]["income"]["columns"][3]
    assert (est["freq"], est["revenue_analysts"], est["eps_analysts"], est["eps_source"]) == ("annual", 20, 25, "yahoo")
    assert fin["annual"]["income"]["columns"][5]["eps_source"] == "nasdaq"
    # Quarters: the income statement has no EPS line by quarter, so Nasdaq's EPS-only quarters add nothing
    cols, rows = table({"financials": fin}, "quarterly", "income")
    assert cols[-3:] == ["q2026-03-31", "e2026-06-30", "e2026-09-30"] and rows["TotalRevenue"][-2:] == [340, 350]
    assert table({"financials": fin}, "annual", "margins")[0][-1] == "t2026-03-31"  # no estimated margins


def test_forward_multiples_use_todays_price_and_enterprise_value():
    cols, rows = table({"financials": with_estimates()}, "annual", "valuation")
    assert cols[2:] == ["c2026-06-15", "e2026-12-31", "e2027-12-31", "e2028-12-31", "e2029-12-31"]
    assert rows["ps"][3] == pytest.approx(2800 / (1400 * 1.40))
    assert rows["ev_sales"][3] == pytest.approx((2800 + 200 * 1.40) / (1400 * 1.40))
    assert rows["pe"][3] == pytest.approx(28 / (1.80 * 1.40)) and rows["pe"][5] == pytest.approx(28 / (2.5 * 1.40))
    assert rows["earnings_yield"][6] == pytest.approx(3.0 * 1.40 / 28)
    assert rows["ps"][5] is None and rows["market_cap"][3] is None  # no revenue that far ahead; no market cap for the future


def test_eps_estimates_in_other_units_are_left_out():
    # in another currency than the statements (an ADR's per-ADR EPS in USD, statements in TWD)
    _, rows = table({"financials": with_estimates(yahoo_estimates(eps_currency="TWD"))}, "annual", "income")
    assert rows["DilutedEPS"][3:] == [None, None] and rows["TotalRevenue"][3:] == [1400, 1600]
    # Yahoo's EPS a year before is twice the reported 1.44 (per something else than a share): no EPS, nor Nasdaq's
    cols, rows = table({"financials": with_estimates(yahoo_estimates(year_ago=2.88))}, "annual", "income")
    assert len(cols) == 5 and rows["DilutedEPS"][3:] == [None, None]
    # but analysts' adjusted EPS can be well off the reported one (Shopify's 1.43 against 0.94)
    _, rows = table({"financials": with_estimates(yahoo_estimates(year_ago=2.1))}, "annual", "income")
    assert rows["DilutedEPS"][3] == 1.80
    # Nasdaq disagreeing with Yahoo on the years both have: only Yahoo's two years
    off = {**NASDAQ, "annual": [{**r, "eps": r["eps"] * 1.5} for r in NASDAQ["annual"]]}
    cols, _ = table({"financials": with_estimates(forecast=off)}, "annual", "income")
    assert cols[3:] == ["e2026-12-31", "e2027-12-31"]


def test_estimates_for_a_reported_year_are_dropped():
    cols, _ = table({"financials": with_estimates(yahoo_estimates(first_year="2025-12-31"), forecast=None)}, "annual", "income")
    assert cols[3:] == ["e2027-12-31"]  # Yahoo hadn't moved on from FY2025 yet
