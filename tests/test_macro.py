import json
import re
from datetime import date

import pytest
import requests

from app import macro
from app.macro import (
    MacroData, Unavailable, fear_greed_rating, parse_fear_greed, parse_treasury_csv, session_move, tenor_label,
    yield_curves,
)
from conftest import login

TREASURY_CSV = """Date,"1 Mo","1.5 Month","2 Mo","3 Mo","6 Mo","1 Yr","2 Yr","5 Yr","10 Yr","30 Yr"
10/09/2026,4.13,4.13,4.13,4.25,4.32,4.47,4.80,5.02,5.24,5.60
10/08/2026,4.14,,4.13,4.23,4.30,4.44,4.75,4.99,5.22,5.60
10/02/2026,4.10,4.11,4.12,4.20,4.28,4.40,4.70,4.95,5.20,5.55
09/09/2026,4.00,4.01,4.02,4.10,4.18,4.30,4.60,4.85,5.10,5.45
"""
LAST_YEAR_CSV = """Date,"1 Mo","2 Mo","3 Mo","6 Mo","1 Yr","2 Yr","5 Yr","10 Yr","30 Yr"
10/09/2025,4.20,4.18,4.10,3.90,3.70,3.55,3.70,4.10,4.70
"""
FEAR_GREED = {
    "fear_and_greed": {"score": 44.66, "rating": "fear", "timestamp": "2026-10-13T18:16:33+00:00",
                       "previous_close": 45.74, "previous_1_week": 55.6, "previous_1_month": 32.69,
                       "previous_1_year": 80.2},
    "fear_and_greed_historical": {"data": [{"x": 1760313600000.0, "y": 29.7}, {"x": 1760400000000.0, "y": 31.2}]},
    "market_momentum_sp500": {"score": 68.6, "rating": "greed"},
    "market_momentum_sp125": {"score": 68.6, "rating": "greed"},
    "put_call_options": {"score": 32.8, "rating": "fear"},
}


# -- pre-market ---------------------------------------------------------------------


def quote(**fields):
    return {"price": 100.0, "prev_close": 98.0, "time": 1, "pre_price": None, "pre_time": None,
            "post_price": None, "post_time": None, **fields}


def test_pre_market_move_is_measured_from_the_last_close():
    move = session_move(quote(market_state="PRE", pre_price=101.0, pre_time=5))
    assert move["session"] == "pre"
    assert move["price"] == 101.0 and move["time"] == 5
    assert move["change"] == pytest.approx(1.0)
    assert move["change_pct"] == pytest.approx(0.01)


def test_regular_session_shows_todays_change():
    move = session_move(quote(market_state="REGULAR", pre_price=99.0))
    assert move["session"] == "regular"
    assert move["change"] == pytest.approx(2.0)
    assert move["change_pct"] == pytest.approx(2 / 98)


def test_after_hours_move():
    move = session_move(quote(market_state="POSTPOST", post_price=95.0, post_time=9))
    assert (move["session"], move["change"], move["time"]) == ("post", -5.0, 9)


def test_before_pre_market_trading_starts_the_last_session_is_shown():
    move = session_move(quote(market_state="PREPRE", post_price=97.0))
    assert move["session"] == "closed"
    assert move["change"] == pytest.approx(2.0)


def test_missing_previous_close():
    assert session_move(quote(market_state="CLOSED", prev_close=None))["change_pct"] is None


class Market:
    """Quotes for the futures and sector ETFs, as MarketData returns them."""

    def __init__(self, quotes):
        self._quotes = quotes

    def quotes(self, symbols):
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    def intraday(self, symbol):
        return 5000.0, [(1, 5000.0), (2, 5010.123456)]


def test_premarket_futures_and_sectors():
    market = Market({
        "ES=F": quote(price=5050.0, prev_close=5000.0, short_name="E-Mini S&P 500 Dec 26", market_state="REGULAR"),
        "XLK": quote(market_state="PRE", pre_price=102.0, pre_time=50),
        "XLE": quote(market_state="PRE", pre_price=99.0, pre_time=60),
        "XLU": quote(market_state="PRE"),  # no pre-market trade yet
    })
    data = MacroData(market, http=object()).premarket()
    [es] = data["futures"]
    assert es["name"] == "E-Mini S&P 500 Dec 26"
    assert es["change"] == 50.0 and es["change_pct"] == pytest.approx(0.01)
    assert es["points"] == [[1, 5000.0], [2, 5010.1235]]
    assert [(s["symbol"], s["sector"], s["session"]) for s in data["sectors"]] == [
        ("XLK", "Technology", "pre"), ("XLE", "Energy", "pre"), ("XLU", "Utilities", "closed")]
    assert data["session"] == "pre"
    assert data["session_time"] == 60


def test_premarket_without_any_quotes():
    with pytest.raises(Unavailable):
        MacroData(Market({}), http=object()).premarket()


# -- Fear & Greed -------------------------------------------------------------------


@pytest.mark.parametrize("score, rating", [
    (0, "extreme fear"), (24.8, "extreme fear"), (25, "fear"), (44.86, "fear"), (45, "neutral"),
    (55, "neutral"), (55.4, "greed"), (75, "greed"), (75.2, "extreme greed"), (100, "extreme greed"),
])
def test_fear_greed_bands(score, rating):
    assert fear_greed_rating(score) == rating


def test_parse_fear_greed():
    fg = parse_fear_greed(FEAR_GREED)
    assert (fg["score"], fg["rating"]) == (44.66, "fear")
    assert fg["compare"] == [
        {"label": "Previous close", "score": 45.74, "rating": "neutral"},
        {"label": "1 week ago", "score": 55.6, "rating": "greed"},
        {"label": "1 month ago", "score": 32.69, "rating": "fear"},
        {"label": "1 year ago", "score": 80.2, "rating": "extreme greed"},
    ]
    # Only the seven indicators CNN averages, in CNN's order; the 125-day duplicate is left out.
    assert [(i["name"], i["rating"]) for i in fg["indicators"]] == [
        ("Market momentum", "greed"), ("Put and call options", "fear")]
    assert fg["history"] == [("2025-10-13", 29.7), ("2025-10-14", 31.2)]


def test_parse_fear_greed_without_a_score():
    with pytest.raises(ValueError):
        parse_fear_greed({"fear_and_greed": {}})


# -- Treasury yields ----------------------------------------------------------------


def test_tenor_labels():
    assert [tenor_label(h) for h in ("1 Mo", "1.5 Month", "10 Yr", "Date", "")] == ["1M", "1.5M", "10Y", None, None]


def test_parse_treasury_csv_skips_blank_cells():
    rows = parse_treasury_csv(TREASURY_CSV)
    assert [r["date"] for r in rows] == [date(2026, 10, 9), date(2026, 10, 8), date(2026, 10, 2), date(2026, 9, 9)]
    assert rows[0]["yields"]["10Y"] == 5.24
    assert "1.5M" not in rows[1]["yields"]


def test_yield_curves_compare_with_a_week_a_month_and_a_year_earlier():
    data = yield_curves(parse_treasury_csv(LAST_YEAR_CSV) + parse_treasury_csv(TREASURY_CSV))
    assert data["tenors"] == ["1M", "1.5M", "2M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "30Y"]
    assert [(c["key"], c["date"]) for c in data["curves"]] == [
        ("latest", "2026-10-09"), ("1w", "2026-10-02"), ("1m", "2026-09-09"), ("1y", "2025-10-09")]
    latest, *_, year_ago = data["curves"]
    assert latest["yields"][data["tenors"].index("10Y")] == 5.24
    assert year_ago["yields"][1] is None  # the 1.5-month bill wasn't published then


def test_yield_curves_without_data():
    with pytest.raises(ValueError):
        yield_curves([])


# -- fetching and caching -------------------------------------------------------------


class Response:
    def __init__(self, text="", data=None, status=200):
        self.text, self._data, self.status_code = text, data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class Http:
    """Answers GETs by URL fragment; an exception class as the answer is raised."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for fragment, answer in self.routes.items():
            if fragment in url:
                if isinstance(answer, type) and issubclass(answer, Exception):
                    raise answer("unreachable")
                return answer
        raise AssertionError(f"unexpected request: {url}")


def treasury_routes():
    year = date.today().year
    return {f"csv/{year}/": Response(TREASURY_CSV), f"csv/{year - 1}/": Response(LAST_YEAR_CSV)}


def test_sentiment_is_cached():
    http = Http({"fearandgreed": Response(data=FEAR_GREED)})
    service = MacroData(Market({}), http=http)
    assert service.sentiment()["score"] == 44.66
    assert service.sentiment()["score"] == 44.66
    assert len(http.calls) == 1


def test_stale_sentiment_is_served_when_cnn_fails(monkeypatch):
    http = Http({"fearandgreed": Response(data=FEAR_GREED)})
    service = MacroData(Market({}), http=http)
    service.sentiment()
    http.routes["fearandgreed"] = requests.ConnectionError
    monkeypatch.setattr(macro, "SENTIMENT_TTL", -1)  # everything cached is now stale
    assert service.sentiment()["score"] == 44.66


def test_failed_source_is_not_retried_straight_away():
    http = Http({"fearandgreed": requests.ConnectionError})
    service = MacroData(Market({}), http=http)
    for _ in range(3):
        with pytest.raises(Unavailable, match="Fear & Greed"):
            service.sentiment()
    assert len(http.calls) == 1


def test_rates_from_the_treasury():
    data = MacroData(Market({}), http=Http(treasury_routes())).rates()
    assert data["source"] == "treasury"
    assert data["curves"][0]["date"] == "2026-10-09"


def test_rates_fall_back_to_yahoo():
    market = Market({"^TNX": quote(price=5.21, prev_close=5.24, time=1791914875), "^IRX": quote(price=4.1, prev_close=4.1)})
    data = MacroData(market, http=Http({"treasury.gov": Response(status=403)})).rates()
    assert data["source"] == "yahoo"
    assert data["tenors"] == ["3M", "10Y"]
    assert [c["key"] for c in data["curves"]] == ["latest", "prev"]
    assert data["curves"][0]["yields"] == [4.1, 5.21]
    assert data["curves"][1]["yields"] == [4.1, 5.24]


def test_rates_unavailable_everywhere():
    with pytest.raises(Unavailable):
        MacroData(Market({}), http=Http({"treasury.gov": requests.ConnectionError})).rates()


# -- pages ----------------------------------------------------------------------------


@pytest.fixture
def macro_app(app):
    market = Market({"ES=F": quote(market_state="REGULAR"), "XLK": quote(market_state="REGULAR")})
    app.config["MACRO"] = MacroData(market, http=Http({**treasury_routes(), "fearandgreed": requests.ConnectionError}))
    return app


def test_markets_tab(macro_app, client):
    assert client.get("/markets").status_code == 302  # sign-in required
    login(client)
    html = client.get("/markets").get_data(as_text=True)
    assert '<body data-page="markets" class="has-sidenav">' in html
    assert 'href="/markets" aria-current="page">Markets</a>' in html
    assert "<h1>Markets</h1>" in html and "Macros" not in html
    for heading in ("Pre-market", "Market sentiment", "Fear &amp; Greed Index", "Bonds &amp; rates", "US Treasury yield curve"):
        assert heading in html
    assert "https://www.cnn.com/markets/fear-and-greed" in html
    resp = client.get("/macros")  # the tab's old name and address
    assert resp.status_code == 301 and resp.headers["Location"] == "/markets"
    # The browser fetches CNN's feed itself, labelled with the server's indicator names.
    assert 'data-fear-greed-feed="https://production.dataviz.cnn.io/index/fearandgreed/graphdata"' in html
    meta = json.loads(re.search(r"data-fear-greed-meta='([^']*)'", html).group(1))
    assert meta["indicators"][0] == ["market_momentum_sp500", "Market momentum", "S&P 500 vs its 125-day moving average"]
    assert meta["compare"][0] == ["previous_close", "Previous close"]


def test_macro_apis(macro_app, client):
    login(client)
    premarket = client.get("/api/markets/premarket").get_json()
    assert [f["symbol"] for f in premarket["futures"]] == ["ES=F"]
    assert premarket["session"] == "regular"
    assert client.get("/api/markets/rates").get_json()["source"] == "treasury"
    resp = client.get("/api/markets/sentiment")
    assert resp.status_code == 502
    assert "Fear & Greed" in resp.get_json()["error"]
