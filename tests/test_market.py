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
