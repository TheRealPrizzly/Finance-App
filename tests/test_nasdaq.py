from app.nasdaq import NasdaqData, parse_forecast


def test_parse_forecast():
    row = lambda end, eps, n: {"fiscalEnd": end, "consensusEPSForecast": eps, "noOfEstimates": n}  # noqa: E731
    forecast = parse_forecast({"data": {
        "yearlyForecast": {"rows": [row("Sep 2027", 9.59, 11), row("Sep 2026", 8.75, 6), row("N/A", 1.0, 1), row("Sep 2028", "--", 0)]},
        "quarterlyForecast": {"rows": [row("Dec 2026", 2.9, 6)]}}})
    assert forecast == {"annual": [{"month": "2026-09", "eps": 8.75, "analysts": 6}, {"month": "2027-09", "eps": 9.59, "analysts": 11}],
                        "quarterly": [{"month": "2026-12", "eps": 2.9, "analysts": 6}]}
    assert parse_forecast(None) == {"annual": [], "quarterly": []}


def test_only_us_listings_are_asked():
    nasdaq = NasdaqData()
    nasdaq.session = None  # any request would fail loudly
    assert nasdaq.forecast("SHOP.TO") is None and nasdaq.forecast("BRK-B") is None and nasdaq.forecast("^GSPC") is None
