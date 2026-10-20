import pytest

from app.market import next_earnings

DAY = 86400
NOW = 1_790_000_000  # a fixed "now" in unix seconds


def test_confirmed_earnings_date():
    q = {"earningsTimestamp": NOW + 5 * DAY, "earningsTimestampStart": NOW + 5 * DAY,
         "earningsTimestampEnd": NOW + 5 * DAY, "isEarningsDateEstimate": False}
    assert next_earnings(q, NOW) == {"start": NOW + 5 * DAY, "end": None, "estimated": False}


def test_estimated_window():
    q = {"earningsTimestampStart": NOW + 10 * DAY, "earningsTimestampEnd": NOW + 16 * DAY,
         "isEarningsDateEstimate": True}
    assert next_earnings(q, NOW) == {"start": NOW + 10 * DAY, "end": NOW + 16 * DAY, "estimated": True}


def test_todays_report_is_still_listed_but_past_ones_are_not():
    assert next_earnings({"earningsTimestamp": NOW - 3600}, NOW)["start"] == NOW - 3600
    assert next_earnings({"earningsTimestamp": NOW - 3 * DAY}, NOW) is None


def test_past_report_with_next_window_announced():
    q = {"earningsTimestamp": NOW - 30 * DAY, "earningsTimestampStart": NOW + 60 * DAY,
         "earningsTimestampEnd": NOW + 64 * DAY, "isEarningsDateEstimate": True}
    assert next_earnings(q, NOW) == {"start": NOW + 60 * DAY, "end": NOW + 64 * DAY, "estimated": True}


def test_no_earnings_fields():
    assert next_earnings({"symbol": "VFV.TO"}, NOW) is None


def test_candles_parse_ohlc_minor_units_and_splits():
    from datetime import date, datetime, timedelta

    from app.market import YahooClient

    client = YahooClient()
    client._get_json = lambda url, params, crumb=False: {"chart": {"result": [{
        "meta": {"currency": "GBp", "gmtoffset": 3600},
        "timestamp": [-100 * DAY, NOW, NOW + DAY],
        "indicators": {"quote": [{"open": [100, None, 300], "high": [110, None, 310], "low": [90, None, 290],
                                  "close": [105, None, 305], "volume": [1500, None, None]}]},
        "events": {"splits": {"x": {"date": NOW, "numerator": 2, "denominator": 1}},
                   "dividends": {"y": {"date": NOW + DAY, "amount": 4.5}}},
    }]}}
    currency, rows, splits, dividends = client.candles("VOD.L", start=date(1900, 1, 1), end=date(2030, 1, 1))
    assert currency == "GBP"
    assert rows[0] == (datetime(1969, 9, 23, 1, 0), 1.0, 1.1, 0.9, 1.05, 1500)  # before 1970, in pounds, local time
    assert rows[1][5] == 0  # a missing volume counts as none
    assert len(rows) == 2  # the bar without a close is skipped
    assert splits == [((datetime(1970, 1, 1) + timedelta(seconds=NOW + 3600)).date(), 2.0)]
    assert dividends == [((datetime(1970, 1, 1) + timedelta(seconds=NOW + DAY + 3600)).date(), 0.045)]  # in pounds


def test_earnings_merge_recent_quarters_over_history():
    from datetime import date

    from app.market import MarketData

    class Client:
        def recent_earnings(self, symbol):
            return [(date(2025, 10, 30), 1.77, 1.85)]

        def earnings_history(self, symbol):  # Yahoo's history dates a report a day apart sometimes
            return [(date(2025, 10, 31), 1.7, 1.8), (date(2025, 5, 1), 1.63, 1.65)]

    market = MarketData(store=None, client=Client())
    assert market.earnings("AAPL") == [(date(2025, 5, 1), 1.63, 1.65), (date(2025, 10, 30), 1.77, 1.85)]


def test_key_stats_for_a_london_stock():
    from app.market import key_stats

    # As Yahoo sends them: the market cap and dividend in pounds, the price and target in pence.
    stats = key_stats({
        "summaryDetail": {"currency": "GBp", "marketCap": {"raw": 94_900_240_384}, "beta": {"raw": -0.196},
                          "trailingPE": {"raw": 24.564}, "dividendRate": {"raw": 0.25}, "dividendYield": {"raw": 0.041},
                          "yield": {}},
        "defaultKeyStatistics": {"beta": {"raw": -0.196}},
        "financialData": {"targetMeanPrice": {"raw": 637.87}},
        "calendarEvents": {"earnings": {"earningsDate": [{"raw": NOW + 2 * DAY}, {"raw": NOW + 6 * DAY}],
                                        "isEarningsDateEstimate": True}},
        "price": {"regularMarketPrice": {"raw": 610.0}, "currency": "GBp"},
    })
    assert stats == {"currency": "GBP", "price": 6.1, "market_cap": 94_900_240_384, "beta": -0.196, "pe": 24.564,
                     "earnings": {"start": NOW + 2 * DAY, "end": NOW + 6 * DAY, "estimated": True},
                     "dividend": 0.25, "yield": 0.041, "target": 6.3787,
                     "profile": {"name": None, "sector": None, "industry": None, "family": None, "category": None,
                                 "summary": None, "website": None, "ir_website": None}}


def test_key_stats_for_an_etf():
    from app.market import key_stats

    stats = key_stats({
        "summaryDetail": {"currency": "CAD", "marketCap": {}, "beta": {}, "trailingPE": {"raw": 23.67},
                          "dividendRate": {}, "dividendYield": {}, "yield": {"raw": 0.0084}},
        "defaultKeyStatistics": {"beta": None, "beta3Year": {"raw": 0.99}},
        "financialData": {"targetMeanPrice": None},
        "price": {"regularMarketPrice": {"raw": 150.0}, "marketCap": {}},
    })
    assert stats == {"currency": "CAD", "price": 150.0, "market_cap": None, "beta": 0.99, "pe": 23.67,
                     "earnings": None, "dividend": None, "yield": 0.0084, "target": None,
                     "profile": {"name": None, "sector": None, "industry": None, "family": None, "category": None,
                                 "summary": None, "website": None, "ir_website": None}}


def test_company_overview():
    from app.market import key_stats

    profile = key_stats({
        "price": {"longName": "Apple Inc.", "shortName": "APPLE INC"},
        "assetProfile": {"sector": "Technology", "industry": "Consumer Electronics", "industryDisp": "Consumer Electronics",
                         "longBusinessSummary": "Apple Inc. designs smartphones.", "website": "https://www.apple.com",
                         "irWebsite": "javascript:alert(1)"},  # only web addresses are linked
    })["profile"]
    assert profile == {"name": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics", "family": None,
                       "category": None, "summary": "Apple Inc. designs smartphones.", "website": "https://www.apple.com",
                       "ir_website": None}
    fund = key_stats({"price": {"shortName": "SPDR S&P 500"}, "assetProfile": {"phone": "1"},
                      "fundProfile": {"family": "State Street", "categoryName": "Large Blend"}})["profile"]
    assert (fund["name"], fund["sector"], fund["family"], fund["category"]) == ("SPDR S&P 500", None, "State Street", "Large Blend")


def test_parse_timeseries():
    from app.market import parse_timeseries

    point = lambda d, v, cur="USD": {"asOfDate": d, "currencyCode": cur, "reportedValue": {"raw": v}}  # noqa: E731
    figures = parse_timeseries({"timeseries": {"result": [
        {"meta": {"type": ["annualDilutedAverageShares"]}, "annualDilutedAverageShares": [point("2025-09-30", 15e9, "USD")]},
        {"meta": {"type": ["annualTotalRevenue"]}, "annualTotalRevenue": [point("2024-09-30", 391e9, "TWD"), None,
                                                                          point("2025-09-30", 416e9, "TWD")]},
        {"meta": {"type": ["trailingNetIncome"]}, "trailingNetIncome": [point("2026-06-30", 129e9, "TWD")]},
        {"meta": {"type": ["quarterlyInterestExpense"]}},  # nothing reported
    ]}})
    assert figures == {
        "currency": "TWD",  # the revenue's, not the share count's
        "annual": {"DilutedAverageShares": {"2025-09-30": 15e9}, "TotalRevenue": {"2024-09-30": 391e9, "2025-09-30": 416e9}},
        "quarterly": {},
        "trailing": {"NetIncome": {"2026-06-30": 129e9}},
    }


def test_company_facts():
    from app.market import company_facts

    facts = company_facts({
        "majorHoldersBreakdown": {"insidersPercentHeld": {"raw": 0.0165}, "institutionsPercentHeld": {"raw": 0.6635},
                                  "institutionsFloatPercentHeld": {"raw": 0.6746}, "institutionsCount": {"raw": 7695}},
        "institutionOwnership": {"ownershipList": [
            {"organization": "Blackrock Inc.", "pctHeld": {"raw": 0.0797}, "position": {"raw": 1162996939},
             "value": {"raw": 397570510695}, "pctChange": {"raw": 0.016}, "reportDate": {"raw": NOW}}]},
        "insiderHolders": {"holders": [
            {"name": "COOK TIMOTHY D", "relation": "Officer and Director", "positionDirect": {"raw": 3237840},
             "positionDirectDate": {"raw": NOW}},
            {"name": "HUANG JEN-HSUN", "relation": "CEO", "positionIndirect": {"raw": 812050000}, "latestTransDate": {"raw": NOW}},
            {"name": "NOBODY", "relation": "Director"}]},  # no position: left out
        "summaryDetail": {"trailingAnnualDividendYield": {"raw": 0.0031}, "fiveYearAvgDividendYield": {"raw": 0.47},
                          "payoutRatio": {"raw": 0.1204}, "exDividendDate": {"raw": NOW}},
        "defaultKeyStatistics": {"sharesOutstanding": {"raw": 14594180000}},
        "financialData": {"financialCurrency": "USD"},
    })
    day = "2026-09-21"
    assert facts["financial_currency"] == "USD" and facts["shares_outstanding"] == 14594180000
    assert (facts["insiders"], facts["institutions"], facts["institutions_count"]) == (0.0165, 0.6635, 7695)
    assert facts["top_institutions"] == [{"name": "Blackrock Inc.", "pct": 0.0797, "shares": 1162996939,
                                          "value": 397570510695, "change": 0.016, "date": day}]
    assert facts["top_funds"] == []
    assert facts["insider_holders"] == [
        {"name": "COOK TIMOTHY D", "relation": "Officer and Director", "shares": 3237840, "direct": 3237840, "indirect": None, "date": day},
        {"name": "HUANG JEN-HSUN", "relation": "CEO", "shares": 812050000, "direct": None, "indirect": 812050000, "date": day},
    ]
    assert facts["dividend"] == {"trailing_yield": 0.0031, "five_year_yield": pytest.approx(0.0047), "payout_ratio": 0.1204, "ex_date": day}


def test_parse_estimates():
    from app.market import parse_estimates

    def trend(period, end, revenue, eps, cur="USD", year_ago=None):
        return {"period": period, "endDate": end,
                "revenueEstimate": {"avg": {"raw": revenue}, "numberOfAnalysts": {"raw": 30}, "revenueCurrency": cur if revenue else None},
                "earningsEstimate": {"avg": {"raw": eps}, "numberOfAnalysts": {"raw": 35}, "yearAgoEps": {"raw": year_ago},
                                     "earningsCurrency": "USD"}}

    est = parse_estimates({"earningsTrend": {"trend": [
        trend("+1y", "2027-09-30", 527e9, 9.56, year_ago=8.82), trend("0y", "2026-09-30", 477e9, 8.82, year_ago=7.46),
        trend("0q", "2026-09-30", 0, 1.98),  # Yahoo's zero revenue is a missing one
        trend("+5y", None, None, None), {"period": "-5y"}]}})
    assert [(p["date"], p["revenue"], p["eps"], p["year_ago_eps"]) for p in est["annual"]] == [
        ("2026-09-30", 477e9, 8.82, 7.46), ("2027-09-30", 527e9, 9.56, 8.82)]
    assert est["quarterly"] == [{"date": "2026-09-30", "revenue": None, "eps": 1.98, "revenue_analysts": 30,
                                 "eps_analysts": 35, "year_ago_eps": None}]
    assert (est["revenue_currency"], est["eps_currency"]) == ("USD", "USD")
    assert parse_estimates({}) == {"annual": [], "quarterly": [], "revenue_currency": None, "eps_currency": None}
