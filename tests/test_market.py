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
                                  "close": [105, None, 305]}]},
        "events": {"splits": {"x": {"date": NOW, "numerator": 2, "denominator": 1}}},
    }]}}
    currency, rows, splits = client.candles("VOD.L", start=date(1900, 1, 1), end=date(2030, 1, 1))
    assert currency == "GBP"
    assert rows[0] == (datetime(1969, 9, 23, 1, 0), 1.0, 1.1, 0.9, 1.05)  # before 1970, in pounds, local time
    assert len(rows) == 2  # the bar without a close is skipped
    assert splits == [((datetime(1970, 1, 1) + timedelta(seconds=NOW + 3600)).date(), 2.0)]
