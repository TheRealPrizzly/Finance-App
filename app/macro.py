"""Market-wide indicators for the Markets tab: index futures and sector ETFs before the
open, CNN's Fear & Greed Index and the US Treasury yield curve.

Futures and sector quotes come from Yahoo Finance through MarketData and share its
caches. Fear & Greed comes from the JSON feed behind cnn.com/markets/fear-and-greed, and
yields from the Treasury's daily par yield curve CSV; both are cached here.
"""
import csv
import io
import logging
import re
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import requests
from flask import current_app

from .market import USER_AGENT, get_market

log = logging.getLogger(__name__)

FUTURES = (("ES=F", "S&P 500 futures"), ("NQ=F", "Nasdaq 100 futures"))
# One Select Sector SPDR ETF per S&P 500 sector.
SECTOR_ETFS = (
    ("XLK", "Technology"), ("XLC", "Communication Services"), ("XLY", "Consumer Discretionary"),
    ("XLF", "Financials"), ("XLV", "Health Care"), ("XLI", "Industrials"), ("XLE", "Energy"),
    ("XLB", "Materials"), ("XLP", "Consumer Staples"), ("XLU", "Utilities"), ("XLRE", "Real Estate"),
)

FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FEAR_GREED_PAGE = "https://www.cnn.com/markets/fear-and-greed"
FEAR_GREED_INDICATORS = (
    ("market_momentum_sp500", "Market momentum", "S&P 500 vs its 125-day moving average"),
    ("stock_price_strength", "Stock price strength", "NYSE stocks at 52-week highs vs lows"),
    ("stock_price_breadth", "Stock price breadth", "McClellan Volume Summation Index"),
    ("put_call_options", "Put and call options", "5-day average put/call ratio"),
    ("market_volatility_vix", "Market volatility", "VIX vs its 50-day moving average"),
    ("safe_haven_demand", "Safe haven demand", "Stock vs bond returns over 20 trading days"),
    ("junk_bond_demand", "Junk bond demand", "Junk vs investment-grade bond yield spread"),
)
FEAR_GREED_COMPARE = (
    ("previous_close", "Previous close"), ("previous_1_week", "1 week ago"),
    ("previous_1_month", "1 month ago"), ("previous_1_year", "1 year ago"),
)

TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                "daily-treasury-rates.csv/{year}/all")
TREASURY_PAGE = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                 "TextView?type=daily_treasury_yield_curve")
# Used when the Treasury site can't be reached. ^IRX is the 13-week bill.
YAHOO_YIELDS = (("^IRX", "3M"), ("^FVX", "5Y"), ("^TNX", "10Y"), ("^TYX", "30Y"))
CURVE_AGES = (("1w", 7), ("1m", 30), ("1y", 365))  # comparison curves: key, days before the latest

SENTIMENT_TTL = 10 * 60
RATES_TTL = 3 * 60 * 60
RETRY_AFTER = 10 * 60  # after a failed request with nothing cached, wait this long before trying again


class Unavailable(Exception):
    """A source couldn't be reached and nothing is cached."""


# -- pre-market ----------------------------------------------------------------------


def session_move(q):
    """The move to show for a quote: the pre-market price before the open, the after-hours
    price after the close, otherwise the regular session's change.

    Returns {"session": "pre" | "regular" | "post" | "closed", "price", "change",
    "change_pct", "time"}. Extended-hours moves are measured from the last close."""
    state = (q.get("market_state") or "").upper()
    last, prev = q.get("price"), q.get("prev_close")
    if state in ("PRE", "PREPRE") and q.get("pre_price") and last:
        session, price, base, when = "pre", q["pre_price"], last, q.get("pre_time")
    elif state in ("POST", "POSTPOST") and q.get("post_price") and last:
        session, price, base, when = "post", q["post_price"], last, q.get("post_time")
    else:
        session, price, base, when = ("regular" if state == "REGULAR" else "closed"), last, prev, q.get("time")
    change = price - base if price is not None and base else None
    return {"session": session, "price": price, "change": change,
            "change_pct": change / base if change is not None else None, "time": when}


# -- Fear & Greed --------------------------------------------------------------------


def fear_greed_rating(score):
    """CNN's bands, applied to the unrounded score as CNN does (44.9 shows as 45 but is "fear")."""
    if score is None:
        return None
    if score < 25:
        return "extreme fear"
    if score < 45:
        return "fear"
    if score <= 55:
        return "neutral"
    if score <= 75:
        return "greed"
    return "extreme greed"


def parse_fear_greed(data):
    fg = data.get("fear_and_greed") or {}
    if fg.get("score") is None:
        raise ValueError("no Fear & Greed score in the response")
    indicators = []
    for key, name, detail in FEAR_GREED_INDICATORS:
        item = data.get(key) or {}
        if item.get("score") is not None:
            indicators.append({"key": key, "name": name, "detail": detail, "score": item["score"],
                               "rating": item.get("rating") or fear_greed_rating(item["score"])})
    history = [
        [datetime.fromtimestamp(p["x"] / 1000, timezone.utc).date().isoformat(), p["y"]]
        for p in ((data.get("fear_and_greed_historical") or {}).get("data") or [])
        if p.get("x") and p.get("y") is not None
    ]
    return {
        "score": fg["score"],
        "rating": fg.get("rating") or fear_greed_rating(fg["score"]),
        "updated": fg.get("timestamp"),
        "compare": [{"label": label, "score": fg.get(key), "rating": fear_greed_rating(fg.get(key))}
                    for key, label in FEAR_GREED_COMPARE if fg.get(key) is not None],
        "indicators": indicators,
        "history": sorted(dict(history).items()),  # one point per day
        "source_url": FEAR_GREED_PAGE,
    }


# -- Treasury yields -----------------------------------------------------------------


def tenor_label(header):
    """'1 Mo' -> '1M', '1.5 Month' -> '1.5M', '10 Yr' -> '10Y'; None for other columns."""
    m = re.fullmatch(r"\s*([\d.]+)\s*(mo|month|months|yr|year|years)\s*", header or "", re.I)
    if not m:
        return None
    return f"{float(m.group(1)):g}{'M' if m.group(2).lower().startswith('mo') else 'Y'}"


def tenor_months(label):
    return float(label[:-1]) * (12 if label.endswith("Y") else 1)


def parse_treasury_csv(text):
    """Rows of the Treasury's daily par yield curve CSV as [{"date", "yields": {tenor: %}}]."""
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    tenors = [tenor_label(h) for h in header]
    rows = []
    for raw in reader:
        if not raw or not raw[0].strip():
            continue
        try:
            day = datetime.strptime(raw[0].strip(), "%m/%d/%Y").date()
        except ValueError:
            continue
        yields = {}
        for tenor, cell in zip(tenors, raw):
            if tenor and cell.strip():
                try:
                    yields[tenor] = float(cell)
                except ValueError:
                    pass
        if yields:
            rows.append({"date": day, "yields": yields})
    return rows


def yield_curves(rows):
    """The latest curve plus the curves about a week, a month and a year earlier."""
    rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])
    if not rows:
        raise ValueError("no yield curve data")
    latest = rows[-1]
    tenors = sorted(latest["yields"], key=tenor_months)
    curves = [("latest", latest)]
    for key, days in CURVE_AGES:
        earlier = [r for r in rows if r["date"] <= latest["date"] - timedelta(days=days)]
        if earlier:
            curves.append((key, earlier[-1]))
    return {
        "source": "treasury",
        "source_url": TREASURY_PAGE,
        "tenors": tenors,
        "curves": [{"key": key, "date": r["date"].isoformat(), "yields": [r["yields"].get(t) for t in tenors]}
                   for key, r in curves],
    }


# -- service -------------------------------------------------------------------------


class MacroData:
    def __init__(self, market, http=None, timeout=15):
        self.market = market
        if http is None:
            http = requests.Session()
            http.headers["User-Agent"] = USER_AGENT
        self.http = http
        self.timeout = timeout
        self._cache = {}  # key -> (fetched_at, value)
        self._failed = {}  # key -> time of the last failure with nothing cached
        self._lock = threading.Lock()

    def _cached(self, key, ttl, fetch):
        """fetch() cached for ``ttl`` seconds. A failed fetch serves the last good value;
        with none, it raises Unavailable and isn't retried for RETRY_AFTER seconds."""
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        if not hit and time.time() - self._failed.get(key, 0) < RETRY_AFTER:
            raise Unavailable(key)
        try:
            value = fetch()
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("%s request failed: %s", key, exc)
            if hit:
                return hit[1]
            self._failed[key] = time.time()
            raise Unavailable(key) from exc
        with self._lock:
            self._cache[key] = (time.time(), value)
        return value

    def premarket(self):
        quotes = self.market.quotes([s for s, _ in FUTURES + SECTOR_ETFS])
        futures = []
        for symbol, label in FUTURES:
            q = quotes.get(symbol)
            if not q or q.get("price") is None:
                continue
            _, points = self.market.intraday(symbol)
            prev = q.get("prev_close")  # futures: the previous settlement
            change = q["price"] - prev if prev else None
            futures.append({
                "symbol": symbol, "label": label, "name": q.get("short_name") or q.get("name"),
                "price": q["price"], "prev_close": prev, "change": change,
                "change_pct": change / prev if change is not None else None, "time": q.get("time"),
                "points": [[ts, round(close, 4)] for ts, close in points],
            })
        sectors = []
        for symbol, sector in SECTOR_ETFS:
            q = quotes.get(symbol)
            if q and q.get("price") is not None:
                sectors.append({"symbol": symbol, "sector": sector, **session_move(q)})
        if not futures and not sectors:
            raise Unavailable("Yahoo Finance didn't return any quotes.")
        sessions = Counter(s["session"] for s in sectors)
        session = sessions.most_common(1)[0][0] if sessions else None
        return {
            "futures": futures,
            "sectors": sectors,
            "session": session,
            "session_time": max((s["time"] for s in sectors if s["session"] == session and s["time"]), default=None),
        }

    def sentiment(self):
        try:
            return self._cached("fear_greed", SENTIMENT_TTL, self._fear_greed)
        except Unavailable:
            raise Unavailable("CNN's Fear & Greed Index can't be reached right now.") from None

    def _fear_greed(self):
        r = self.http.get(FEAR_GREED_URL, timeout=self.timeout,
                          headers={"Referer": "https://www.cnn.com/", "Origin": "https://www.cnn.com"})
        r.raise_for_status()
        return parse_fear_greed(r.json())

    def rates(self):
        try:
            return self._cached("treasury", RATES_TTL, self._treasury)
        except Unavailable:
            return self._yahoo_yields()

    def _treasury(self):
        year = date.today().year
        rows = []
        for y in (year - 1, year):  # last year's file has the curve a year ago, and early January's
            r = self.http.get(TREASURY_URL.format(year=y), timeout=self.timeout, params={
                "type": "daily_treasury_yield_curve", "field_tdr_date_value": y, "page": "", "_format": "csv"})
            r.raise_for_status()
            rows += parse_treasury_csv(r.text)
        return yield_curves(rows)

    def _yahoo_yields(self):
        quotes = self.market.quotes([s for s, _ in YAHOO_YIELDS])
        got = [(tenor, quotes[s]) for s, tenor in YAHOO_YIELDS if quotes.get(s) and quotes[s].get("price") is not None]
        if not got:
            raise Unavailable("Treasury yields can't be reached right now.")
        when = max((q.get("time") or 0 for _, q in got), default=0)
        return {
            "source": "yahoo",
            "source_url": TREASURY_PAGE,
            "tenors": [tenor for tenor, _ in got],
            "curves": [
                {"key": "latest", "date": date.fromtimestamp(when).isoformat() if when else None,
                 "yields": [q["price"] for _, q in got]},
                {"key": "prev", "date": None, "yields": [q.get("prev_close") for _, q in got]},
            ],
        }


def get_macro():
    """The app-wide macro data service (tests can inject one via config["MACRO"])."""
    app = current_app._get_current_object()
    if app.config.get("MACRO") is not None:
        return app.config["MACRO"]
    macro = app.extensions.get("macro")
    if macro is None:
        macro = app.extensions["macro"] = MacroData(get_market())
    return macro
