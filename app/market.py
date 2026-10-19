"""Market data from Yahoo Finance: live quotes, daily price history, FX rates and sectors.

Results are cached in memory (quotes, history) and in SQLite (symbol resolution,
sector weights) so pages stay fast and Yahoo isn't hit on every request.
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import quote as urlquote

import requests
from flask import current_app

from .db import SecurityStore

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
QUOTE_TTL = 60  # seconds
HISTORY_TTL = 60 * 60
EARNINGS_TTL = 12 * 60 * 60
STATS_TTL = 15 * 60
FUNDAMENTALS_TTL = 12 * 60 * 60
INTRADAY_TTL = 2 * 60
RESOLVE_RETRY = timedelta(days=1)  # retry symbols Yahoo didn't recognise after this long
SECTOR_TTL = timedelta(days=7)

SECTOR_NAMES = {
    "realestate": "Real Estate",
    "consumer_cyclical": "Consumer Cyclical",
    "basic_materials": "Basic Materials",
    "consumer_defensive": "Consumer Defensive",
    "technology": "Technology",
    "communication_services": "Communication Services",
    "financial_services": "Financial Services",
    "utilities": "Utilities",
    "industrials": "Industrials",
    "energy": "Energy",
    "healthcare": "Healthcare",
}

# Some exchanges quote in minor units (pence, cents); convert to the major currency.
MINOR_UNITS = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

CAD_SUFFIXES = (".TO", ".V", ".NE", ".CN")

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_START = date(1900, 1, 1)  # asking from here gets every daily bar Yahoo has
# Intraday chart spans: (Yahoo range, bar size). The range is wider than the span shown so the
# moving averages have earlier bars to start from.
INTRADAY_SPANS = {"1D": ("5d", "5m"), "1W": ("1mo", "30m")}
FUNDAMENTALS_START = 493590046  # 1985: asking from here gets every period Yahoo has (4 years, 5 quarters)


def _utc(ts):
    """Unix seconds as a UTC datetime; unlike fromtimestamp, this works before 1970 on Windows."""
    return EPOCH + timedelta(seconds=ts)


def _major_unit(currency):
    major, divisor = MINOR_UNITS.get(currency, (currency, 1.0))
    return major, divisor


def unadjust_splits(closes, splits):
    """Undo Yahoo's split adjustment so each close is the price actually quoted that day.

    Yahoo rescales every close before a split (a 1:4 reverse split multiplies earlier
    closes by 4). Broker records hold the share counts of the day, so they need the
    as-traded prices. ``splits`` is a list of (effective date, numerator / denominator).
    """
    if not splits:
        return closes
    out = {}
    for d, close in closes.items():
        for split_date, ratio in splits:
            if d < split_date:
                close *= ratio
        out[d] = close
    return out


def next_earnings(quote, now=None):
    """The next earnings date in a Yahoo quote, or None if none is announced.

    Returns {"start": unix seconds, "end": unix seconds or None, "estimated": bool}. Yahoo
    gives a single time once a date is confirmed, and a start/end window while it's still
    an estimate. A date stays "upcoming" for a day so today's report is still listed."""
    now = time.time() if now is None else now
    times = sorted({t for t in (quote.get(k) for k in ("earningsTimestampStart", "earningsTimestamp",
                                                        "earningsTimestampEnd")) if t})
    upcoming = [t for t in times if t >= now - 86400]
    if not upcoming:
        return None
    start, end = upcoming[0], upcoming[-1]
    return {"start": start, "end": end if end - start >= 86400 else None,
            "estimated": bool(quote.get("isEarningsDateEstimate"))}


def _web_url(url):
    """A link Yahoo lists, if it's an ordinary web address."""
    url = (url or "").strip()
    return url if url.lower().startswith(("https://", "http://")) else None


def key_stats(summary):
    """The Research tab's key statistics and company overview from a Yahoo quote summary
    (summaryDetail, defaultKeyStatistics, financialData, calendarEvents, price, assetProfile and
    fundProfile modules). Missing figures are None.

    Yahoo gives the market cap and dividend in the major currency (pounds for a London stock) but
    prices and the target in the quote's own units (pence), so only those are converted. Funds
    have no beta, only a 3-year one, and a yield in place of a dividend rate."""
    def raw(module, key):
        value = (summary.get(module) or {}).get(key)
        return value.get("raw") if isinstance(value, dict) else value

    detail_currency = (summary.get("summaryDetail") or {}).get("currency")
    currency, divisor = _major_unit(detail_currency or (summary.get("price") or {}).get("currency"))
    price, target = raw("price", "regularMarketPrice"), raw("financialData", "targetMeanPrice")
    profile, fund = summary.get("assetProfile") or {}, summary.get("fundProfile") or {}
    times = sorted(t["raw"] for t in ((summary.get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate") or []
                   if isinstance(t, dict) and t.get("raw"))
    return {
        "currency": currency,
        "price": price / divisor if price is not None else None,
        "market_cap": raw("summaryDetail", "marketCap") or raw("price", "marketCap"),
        "beta": next((b for b in (raw("summaryDetail", "beta"), raw("defaultKeyStatistics", "beta"),
                                  raw("defaultKeyStatistics", "beta3Year")) if b is not None), None),
        "pe": raw("summaryDetail", "trailingPE"),
        "earnings": {"start": times[0], "end": times[-1] if times[-1] - times[0] >= 86400 else None,
                     "estimated": bool(((summary.get("calendarEvents") or {}).get("earnings") or {})
                                       .get("isEarningsDateEstimate"))} if times else None,
        "dividend": raw("summaryDetail", "dividendRate") or None,
        "yield": raw("summaryDetail", "dividendYield") or raw("summaryDetail", "yield") or None,
        "target": target / divisor if target is not None else None,
        "profile": {
            "name": (summary.get("price") or {}).get("longName") or (summary.get("price") or {}).get("shortName"),
            "sector": profile.get("sectorDisp") or profile.get("sector"),
            "industry": profile.get("industryDisp") or profile.get("industry"),
            "family": fund.get("family"),  # funds have a family and category instead
            "category": fund.get("categoryName"),
            "summary": profile.get("longBusinessSummary"),
            "website": _web_url(profile.get("website")),
            "ir_website": _web_url(profile.get("irWebsite")),
        },
    }


def _iso_day(ts):
    return _utc(ts).date().isoformat() if ts else None


def parse_timeseries(data):
    """Yahoo's fundamentals timeseries as {"currency": reporting currency, "annual" | "quarterly" |
    "trailing": {line item: {ISO period end: value}}}. "trailing" is the twelve months to a quarter's end.
    Share counts and per-share figures are restated for later splits, like Yahoo's prices."""
    out = {"currency": None, "annual": {}, "quarterly": {}, "trailing": {}}
    for result in ((data or {}).get("timeseries") or {}).get("result") or []:
        name = ((result.get("meta") or {}).get("type") or [""])[0]
        freq = next((f for f in ("annual", "quarterly", "trailing") if name.startswith(f)), None)
        if not freq:
            continue
        for point in result.get(name) or []:
            value = ((point or {}).get("reportedValue") or {}).get("raw")
            if value is None or not point.get("asOfDate"):
                continue
            out[freq].setdefault(name[len(freq):], {})[point["asOfDate"]] = value
            if name.endswith("TotalRevenue") or not out["currency"]:  # share counts carry a currency code too
                out["currency"] = point.get("currencyCode") or out["currency"]
    return out


def company_facts(summary):
    """Ownership and dividend figures for the Research tab's Company section from a Yahoo quote
    summary (majorHoldersBreakdown, institutionOwnership, fundOwnership, insiderHolders,
    summaryDetail, defaultKeyStatistics and financialData modules). Missing figures are None.
    Percentages are fractions, except Yahoo's 5-year average yield, which comes in percent."""
    def raw(value):
        return value.get("raw") if isinstance(value, dict) else value

    def get(module, key):
        return raw((summary.get(module) or {}).get(key))

    def holders(module):
        return [{"name": h["organization"], "pct": raw(h.get("pctHeld")), "shares": raw(h.get("position")),
                 "value": raw(h.get("value")), "change": raw(h.get("pctChange")), "date": _iso_day(raw(h.get("reportDate")))}
                for h in (summary.get(module) or {}).get("ownershipList") or [] if h.get("organization")]

    insiders = []
    for h in (summary.get("insiderHolders") or {}).get("holders") or []:
        direct, indirect = raw(h.get("positionDirect")), raw(h.get("positionIndirect"))
        if h.get("name") and (direct or indirect):
            insiders.append({
                "name": h["name"], "relation": h.get("relation"), "shares": (direct or 0) + (indirect or 0),
                "direct": direct, "indirect": indirect,
                "date": _iso_day(raw(h.get("positionDirectDate")) or raw(h.get("positionIndirectDate"))
                                 or raw(h.get("latestTransDate"))),
            })
    five_year = get("summaryDetail", "fiveYearAvgDividendYield")
    return {
        "financial_currency": get("financialData", "financialCurrency"),
        "shares_outstanding": get("defaultKeyStatistics", "sharesOutstanding"),
        "insiders": get("majorHoldersBreakdown", "insidersPercentHeld"),
        "institutions": get("majorHoldersBreakdown", "institutionsPercentHeld"),
        "institutions_float": get("majorHoldersBreakdown", "institutionsFloatPercentHeld"),
        "institutions_count": get("majorHoldersBreakdown", "institutionsCount"),
        "top_institutions": holders("institutionOwnership"),
        "top_funds": holders("fundOwnership"),
        "insider_holders": insiders,
        "dividend": {
            "trailing_yield": get("summaryDetail", "trailingAnnualDividendYield") or None,
            "five_year_yield": five_year / 100 if five_year else None,
            "payout_ratio": get("summaryDetail", "payoutRatio"),
            "ex_date": _iso_day(get("summaryDetail", "exDividendDate")),
        },
    }


class YahooClient:
    """Thin HTTP client for the unofficial Yahoo Finance endpoints."""

    BASE1 = "https://query1.finance.yahoo.com"
    BASE2 = "https://query2.finance.yahoo.com"

    def __init__(self, timeout=15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._crumb = None
        self._lock = threading.Lock()

    def _get_crumb(self, refresh=False):
        with self._lock:
            if self._crumb and not refresh:
                return self._crumb
            try:
                self.session.get("https://fc.yahoo.com", timeout=self.timeout)  # sets the session cookie
            except requests.RequestException:
                pass
            r = self.session.get(f"{self.BASE2}/v1/test/getcrumb", timeout=self.timeout)
            r.raise_for_status()
            self._crumb = r.text.strip()
            return self._crumb

    def _get_json(self, url, params, crumb=False, body=None):
        """GET a JSON response, or POST ``body`` as JSON when it's given."""
        for attempt in range(2):
            p = dict(params)
            if crumb:
                p["crumb"] = self._get_crumb(refresh=attempt > 0)
            if body is None:
                r = self.session.get(url, params=p, timeout=self.timeout)
            else:
                r = self.session.post(url, params=p, json=body, timeout=self.timeout)
            if crumb and attempt == 0 and r.status_code in (401, 403):
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        return None

    def quotes(self, symbols):
        data = self._get_json(f"{self.BASE2}/v7/finance/quote", {"symbols": ",".join(symbols)}, crumb=True)
        return ((data or {}).get("quoteResponse") or {}).get("result") or []

    def chart(self, symbol, start, end):
        """Split-adjusted daily closes between two dates.
        Returns (currency, {date: close}, [(split date, numerator / denominator)])."""
        p1 = int(datetime.combine(start, dtime.min, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime.combine(end, dtime.min, tzinfo=timezone.utc).timestamp())
        data = self._get_json(
            f"{self.BASE1}/v8/finance/chart/{urlquote(symbol, safe='')}",
            {"period1": p1, "period2": p2, "interval": "1d", "events": "split"},
        )
        results = ((data or {}).get("chart") or {}).get("result") or []
        if not results:
            return None, {}, []
        res = results[0]
        meta = res.get("meta") or {}
        offset = meta.get("gmtoffset") or 0
        currency, divisor = _major_unit(meta.get("currency"))
        closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        out = {}
        for ts, close in zip(res.get("timestamp") or [], closes):
            if close is not None:
                out[datetime.fromtimestamp(ts + offset, tz=timezone.utc).date()] = close / divisor
        splits = [
            (datetime.fromtimestamp(sp["date"] + offset, tz=timezone.utc).date(), sp["numerator"] / sp["denominator"])
            for sp in ((res.get("events") or {}).get("splits") or {}).values()
            if sp.get("date") and sp.get("numerator") and sp.get("denominator")
        ]
        return currency, out, splits

    def intraday(self, symbol, interval="5m"):
        """Today's prices at ``interval``. Returns (previous close, [(unix seconds, close)])."""
        data = self._get_json(
            f"{self.BASE1}/v8/finance/chart/{urlquote(symbol, safe='')}", {"range": "1d", "interval": interval}
        )
        results = ((data or {}).get("chart") or {}).get("result") or []
        if not results:
            return None, []
        res = results[0]
        meta = res.get("meta") or {}
        _, divisor = _major_unit(meta.get("currency"))
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        points = [(ts, close / divisor) for ts, close in zip(res.get("timestamp") or [], closes) if close is not None]
        return (prev / divisor if prev else None), points

    def candles(self, symbol, interval="1d", start=None, end=None, span=None):
        """Split-adjusted OHLC bars and volumes, either between two dates or over a Yahoo range like "5d".
        Returns (currency, [(exchange-local datetime, open, high, low, close, volume)], [(split date, ratio)],
        [(ex-dividend date, split-adjusted amount per share)])."""
        params = {"interval": interval, "events": "div,split"}
        if span:
            params["range"] = span
        else:
            params["period1"] = int((datetime.combine(start, dtime.min, tzinfo=timezone.utc) - EPOCH).total_seconds())
            params["period2"] = int((datetime.combine(end, dtime.min, tzinfo=timezone.utc) - EPOCH).total_seconds())
        data = self._get_json(f"{self.BASE1}/v8/finance/chart/{urlquote(symbol, safe='')}", params)
        results = ((data or {}).get("chart") or {}).get("result") or []
        if not results:
            return None, [], [], []
        res = results[0]
        meta = res.get("meta") or {}
        offset = meta.get("gmtoffset") or 0
        currency, divisor = _major_unit(meta.get("currency"))
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
        rows = []
        for i, ts in enumerate(res.get("timestamp") or []):
            o, h, l, close, volume = (col[i] if i < len(col) else None for col in cols)
            if close is None:
                continue
            o = close if o is None else o
            h = max(o, close) if h is None else h
            l = min(o, close) if l is None else l
            rows.append((_utc(ts + offset).replace(tzinfo=None), o / divisor, h / divisor, l / divisor, close / divisor,
                         int(volume or 0)))
        splits = [
            (_utc(sp["date"] + offset).date(), sp["numerator"] / sp["denominator"])
            for sp in ((res.get("events") or {}).get("splits") or {}).values()
            if sp.get("date") and sp.get("numerator") and sp.get("denominator")
        ]
        dividends = sorted(
            (_utc(dv["date"] + offset).date(), dv["amount"] / divisor)
            for dv in ((res.get("events") or {}).get("dividends") or {}).values()
            if dv.get("date") and dv.get("amount")
        )
        return currency, rows, splits, dividends

    def earnings_history(self, symbol):
        """Past earnings reports: [(report date, EPS estimate or None, reported EPS)]. Yahoo's
        history here goes back decades but can lag by a few quarters; see recent_earnings."""
        data = self._get_json(f"{self.BASE1}/v1/finance/visualization", {"lang": "en-US", "region": "US"}, crumb=True, body={
            "size": 250,
            "query": {"operator": "and", "operands": [
                {"operator": "eq", "operands": ["ticker", symbol]},
                {"operator": "eq", "operands": ["eventtype", "2"]},  # earnings reports
            ]},
            "sortField": "startdatetime",
            "sortType": "DESC",
            "entityIdType": "earnings",
            "includeFields": ["startdatetime", "epsestimate", "epsactual"],
        })
        results = ((data or {}).get("finance") or {}).get("result") or []
        docs = (results[0] or {}).get("documents") or [] if results else []
        if not docs:
            return []
        cols = [c.get("id") for c in docs[0].get("columns") or []]
        out = []
        for row in docs[0].get("rows") or []:
            r = dict(zip(cols, row))
            if r.get("startdatetime") and r.get("epsactual") is not None:
                out.append((date.fromisoformat(r["startdatetime"][:10]), r.get("epsestimate"), r["epsactual"]))
        return out

    def recent_earnings(self, symbol):
        """The last four earnings reports, in the same form as earnings_history."""
        chart = ((self.summary(symbol, ["earnings"]).get("earnings") or {}).get("earningsChart") or {})
        out = []
        for q in chart.get("quarterly") or []:
            reported = (q.get("reportedDate") or {}).get("raw")
            actual = (q.get("actual") or {}).get("raw")
            if reported and actual is not None:
                out.append((_utc(reported).date(), (q.get("estimate") or {}).get("raw"), actual))
        return out

    def summary(self, symbol, modules):
        data = self._get_json(
            f"{self.BASE2}/v10/finance/quoteSummary/{urlquote(symbol, safe='')}",
            {"modules": ",".join(modules)},
            crumb=True,
        )
        results = ((data or {}).get("quoteSummary") or {}).get("result") or []
        return results[0] if results else {}

    def fundamentals(self, symbol, keys):
        """Annual, quarterly and trailing figures for Yahoo line items like "TotalRevenue"; see parse_timeseries."""
        return parse_timeseries(self._get_json(
            f"{self.BASE1}/ws/fundamentals-timeseries/v1/finance/timeseries/{urlquote(symbol, safe='')}",
            {"symbol": symbol, "type": ",".join(f + k for f in ("annual", "quarterly", "trailing") for k in keys),
             "period1": FUNDAMENTALS_START, "period2": int(time.time()) + 86400},
            crumb=True,
        ))


class MarketData:
    """Market data with caching. Prices are in each security's own currency;
    FX rates convert a currency into the base currency."""

    def __init__(self, store, client=None, base_currency="CAD"):
        self.store = store
        self.client = client or YahooClient()
        self.base = base_currency
        self._quotes = {}  # yahoo symbol -> (fetched_at, quote)
        self._history = {}  # yahoo symbol -> (fetched_at, start, currency, closes, splits)
        self._intraday = {}  # yahoo symbol -> (fetched_at, prev_close, points)
        self._candles = {}  # yahoo symbol -> (fetched_at, start, currency, rows, splits, dividends); (symbol, span) for intraday
        self._earnings = {}  # yahoo symbol -> (fetched_at, reports)
        self._stats = {}  # yahoo symbol -> (fetched_at, key stats)
        self._fundamentals = {}  # yahoo symbol -> (fetched_at, parsed timeseries)
        self._facts = {}  # yahoo symbol -> (fetched_at, company facts)
        self._lock = threading.Lock()

    # -- symbols ---------------------------------------------------------------

    @staticmethod
    def candidates(symbol, currency):
        """Yahoo symbols to try for a broker symbol, most likely first."""
        if any(ch in symbol for ch in "=-^") or symbol.endswith(CAD_SUFFIXES):
            return [symbol]
        if "." in symbol:  # class shares: BRK.B -> BRK-B, RCI.B (CAD) -> RCI-B.TO
            dashed = symbol.replace(".", "-")
            return [f"{dashed}.TO", symbol, dashed] if currency == "CAD" else [symbol, dashed]
        if currency == "CAD":
            return [symbol + s for s in CAD_SUFFIXES] + [symbol]
        return [symbol]

    def resolve(self, symbol, currency):
        """Map a broker symbol to a Yahoo symbol, or None if Yahoo doesn't know it."""
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return None
        row = self.store.get_security(symbol, currency)
        if row and (row["yahoo_symbol"] or datetime.now() - row["resolved_at"] < RESOLVE_RETRY):
            return row["yahoo_symbol"]
        options = self.candidates(symbol, currency)
        try:
            found = {q["symbol"].upper(): q for q in self.client.quotes(options) if q.get("regularMarketPrice") is not None}
        except requests.RequestException as exc:
            log.warning("Could not resolve %s: %s", symbol, exc)
            return row["yahoo_symbol"] if row else None
        ysym = next((c for c in options if c in found), None)
        q = found.get(ysym) if ysym else None
        self.store.save_security(
            symbol, currency, ysym,
            name=q and (q.get("longName") or q.get("shortName")),
            quote_type=q and q.get("quoteType"),
        )
        if q:
            self._remember_quote(q)
        return ysym

    # -- quotes ----------------------------------------------------------------

    def _remember_quote(self, q):
        currency, divisor = _major_unit(q.get("currency"))

        def price(key):
            value = q.get(key)
            return None if value is None else value / divisor

        data = {
            "symbol": q["symbol"].upper(),
            "price": price("regularMarketPrice"),
            "prev_close": price("regularMarketPreviousClose"),
            "time": q.get("regularMarketTime"),
            "pre_price": price("preMarketPrice"),  # before the open (4:00–9:30 ET for US stocks)
            "pre_time": q.get("preMarketTime"),
            "post_price": price("postMarketPrice"),  # after the close
            "post_time": q.get("postMarketTime"),
            "currency": currency,
            "name": q.get("longName") or q.get("shortName") or q["symbol"],
            "short_name": q.get("shortName"),
            "quote_type": q.get("quoteType"),
            "market_state": q.get("marketState"),
            "earnings": next_earnings(q),
        }
        with self._lock:
            self._quotes[data["symbol"]] = (time.time(), data)
        return data

    def quotes(self, symbols):
        """Latest quotes keyed by Yahoo symbol. Missing symbols are left out."""
        now = time.time()
        out, missing = {}, []
        for s in {s.upper() for s in symbols if s}:
            cached = self._quotes.get(s)
            if cached and now - cached[0] < QUOTE_TTL:
                out[s] = cached[1]
            else:
                missing.append(s)
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            try:
                for q in self.client.quotes(chunk):
                    data = self._remember_quote(q)
                    out[data["symbol"]] = data
            except requests.RequestException as exc:
                log.warning("Quote request failed: %s", exc)
                for s in chunk:  # fall back to stale quotes
                    if s in self._quotes:
                        out[s] = self._quotes[s][1]
        return out

    # -- history ---------------------------------------------------------------

    def history(self, yahoo_symbol, start, as_traded=False):
        """Daily closes since ``start``. Returns (currency, {date: close}).

        Closes are split-adjusted, which suits return series like the benchmark. Pass
        ``as_traded=True`` to value share counts from transactions, which are not.
        """
        cached = self._history.get(yahoo_symbol)
        if not (cached and cached[1] <= start and time.time() - cached[0] < HISTORY_TTL):
            try:
                currency, closes, splits = self.client.chart(yahoo_symbol, start, date.today() + timedelta(days=1))
            except requests.RequestException as exc:
                log.warning("History request for %s failed: %s", yahoo_symbol, exc)
                if not cached:
                    return None, {}
            else:
                cached = (time.time(), start, currency, closes, splits)
                with self._lock:
                    self._history[yahoo_symbol] = cached
        _, _, currency, closes, splits = cached
        return currency, unadjust_splits(closes, splits) if as_traded else closes

    def intraday(self, yahoo_symbol):
        """Today's 5-minute prices. Returns (previous close, [(unix seconds, close)])."""
        cached = self._intraday.get(yahoo_symbol)
        if cached and time.time() - cached[0] < INTRADAY_TTL:
            return cached[1], cached[2]
        try:
            prev, points = self.client.intraday(yahoo_symbol)
        except requests.RequestException as exc:
            log.warning("Intraday request for %s failed: %s", yahoo_symbol, exc)
            return (cached[1], cached[2]) if cached else (None, [])
        with self._lock:
            self._intraday[yahoo_symbol] = (time.time(), prev, points)
        return prev, points

    def daily_candles(self, yahoo_symbol, start=None):
        """Split-adjusted daily OHLC bars and volumes since ``start`` (None: as far back as Yahoo goes).
        Returns (currency, [(date, open, high, low, close, volume)], splits, [(ex-date, adjusted dividend)])."""
        start = start or MAX_START
        cached = self._candles.get(yahoo_symbol)
        if not (cached and cached[1] <= start and time.time() - cached[0] < HISTORY_TTL):
            try:
                currency, rows, splits, dividends = self.client.candles(
                    yahoo_symbol, "1d", start=start, end=date.today() + timedelta(days=1))
            except requests.RequestException as exc:
                log.warning("Candle request for %s failed: %s", yahoo_symbol, exc)
                if not cached:
                    return None, [], [], []
            else:
                by_day = {dt.date(): (dt.date(), *ohlcv) for dt, *ohlcv in rows}  # Yahoo can repeat today's bar
                cached = (time.time(), start, currency, sorted(by_day.values()), splits, dividends)
                with self._lock:
                    self._candles[yahoo_symbol] = cached
        _, _, currency, rows, splits, dividends = cached
        return currency, [r for r in rows if r[0] >= start], splits, [dv for dv in dividends if dv[0] >= start]

    def intraday_candles(self, yahoo_symbol, span):
        """Intraday OHLC bars and volumes for a span in INTRADAY_SPANS.
        Returns (currency, [(exchange-local datetime, open, high, low, close, volume)])."""
        key = (yahoo_symbol, span)
        cached = self._candles.get(key)
        if cached and time.time() - cached[0] < INTRADAY_TTL:
            return cached[2], cached[3]
        yahoo_range, interval = INTRADAY_SPANS[span]
        try:
            currency, rows, *_ = self.client.candles(yahoo_symbol, interval, span=yahoo_range)
        except requests.RequestException as exc:
            log.warning("Intraday candle request for %s failed: %s", yahoo_symbol, exc)
            return (cached[2], cached[3]) if cached else (None, [])
        with self._lock:
            self._candles[key] = (time.time(), None, currency, rows, [], [])
        return currency, rows

    def earnings(self, yahoo_symbol):
        """Past earnings reports, oldest first: [(report date, EPS estimate or None, reported EPS)].
        Yahoo's long history can miss the latest quarters, so the last four come from the
        quote summary and win over history entries within a few days of them."""
        cached = self._earnings.get(yahoo_symbol)
        if cached and time.time() - cached[0] < EARNINGS_TTL:
            return cached[1]
        reports, failed = [], False
        for fetch in (self.client.recent_earnings, self.client.earnings_history):
            try:
                found = fetch(yahoo_symbol)
            except (requests.RequestException, ValueError) as exc:
                log.warning("Earnings request for %s failed: %s", yahoo_symbol, exc)
                failed = True
                continue
            reports += [r for r in found if all(abs((r[0] - kept[0]).days) > 5 for kept in reports)]
        if failed and not reports and cached:
            return cached[1]
        reports.sort()
        with self._lock:
            self._earnings[yahoo_symbol] = (time.time(), reports)
        return reports

    def key_stats(self, yahoo_symbol):
        """Market cap, beta, P/E, next earnings, dividend, analysts' target and a company
        profile; see key_stats()."""
        cached = self._stats.get(yahoo_symbol)
        if cached and time.time() - cached[0] < STATS_TTL:
            return cached[1]
        try:
            summary = self.client.summary(yahoo_symbol, [
                "summaryDetail", "defaultKeyStatistics", "financialData", "calendarEvents", "price",
                "assetProfile", "fundProfile"])
        except (requests.RequestException, ValueError) as exc:
            log.warning("Key statistics request for %s failed: %s", yahoo_symbol, exc)
            return cached[1] if cached else None
        stats = key_stats(summary) if summary else None
        with self._lock:
            self._stats[yahoo_symbol] = (time.time(), stats)
        return stats

    def fundamentals(self, yahoo_symbol, keys):
        """Financial statement line items; see parse_timeseries(). None if Yahoo couldn't be reached."""
        cached = self._fundamentals.get(yahoo_symbol)
        if cached and time.time() - cached[0] < FUNDAMENTALS_TTL:
            return cached[1]
        try:
            figures = self.client.fundamentals(yahoo_symbol, keys)
        except (requests.RequestException, ValueError) as exc:
            log.warning("Fundamentals request for %s failed: %s", yahoo_symbol, exc)
            return cached[1] if cached else None
        with self._lock:
            self._fundamentals[yahoo_symbol] = (time.time(), figures)
        return figures

    def company_facts(self, yahoo_symbol):
        """Holders and dividend figures; see company_facts(). None if Yahoo couldn't be reached."""
        cached = self._facts.get(yahoo_symbol)
        if cached and time.time() - cached[0] < STATS_TTL:
            return cached[1]
        try:
            summary = self.client.summary(yahoo_symbol, [
                "majorHoldersBreakdown", "institutionOwnership", "fundOwnership", "insiderHolders",
                "summaryDetail", "defaultKeyStatistics", "financialData"])
        except (requests.RequestException, ValueError) as exc:
            log.warning("Company facts request for %s failed: %s", yahoo_symbol, exc)
            return cached[1] if cached else None
        facts = company_facts(summary) if summary else None
        with self._lock:
            self._facts[yahoo_symbol] = (time.time(), facts)
        return facts

    # -- FX --------------------------------------------------------------------

    def fx_pair(self, currency):
        return f"{currency}{self.base}=X"

    def fx_rate(self, currency):
        """Current rate converting 1 unit of ``currency`` into the base currency."""
        if not currency or currency == self.base:
            return 1.0
        q = self.quotes([self.fx_pair(currency)]).get(self.fx_pair(currency))
        return q["price"] if q and q.get("price") else None

    def fx_history(self, currency, start):
        if not currency or currency == self.base:
            return {}
        return self.history(self.fx_pair(currency), start)[1]

    # -- sectors ---------------------------------------------------------------

    def sector_weights(self, yahoo_symbol, quote_type=None):
        """Sector exposure as {sector: weight}; ETFs are broken down by their holdings."""
        if quote_type == "CRYPTOCURRENCY":
            return {"Crypto": 1.0}
        cached = self.store.get_sector(yahoo_symbol)
        if cached and datetime.now() - cached[1] < SECTOR_TTL:
            return cached[0]
        try:
            summary = self.client.summary(yahoo_symbol, ["assetProfile", "topHoldings"])
        except requests.RequestException as exc:
            log.warning("Sector request for %s failed: %s", yahoo_symbol, exc)
            return cached[0] if cached else {"Unclassified": 1.0}

        holdings = summary.get("topHoldings") or {}
        weights = {}
        for item in holdings.get("sectorWeightings") or []:
            for key, value in item.items():
                raw = value.get("raw") if isinstance(value, dict) else value
                if raw:
                    weights[SECTOR_NAMES.get(key, key.replace("_", " ").title())] = float(raw)
        bonds = (holdings.get("bondPosition") or {}).get("raw") or 0
        if bonds > 0.5:
            weights = {"Fixed Income": 1.0}
        elif weights:
            total = sum(weights.values())
            if total > 1:
                weights = {k: v / total for k, v in weights.items()}
            elif total < 0.995:
                weights["Other"] = 1 - total
        else:
            sector = (summary.get("assetProfile") or {}).get("sector")
            weights = {sector or "Other": 1.0}
        self.store.save_sector(yahoo_symbol, weights)
        return weights


def get_market():
    """The app-wide market data service (tests can inject one via config["MARKET"])."""
    app = current_app._get_current_object()
    if app.config.get("MARKET") is not None:
        return app.config["MARKET"]
    market = app.extensions.get("market")
    if market is None:
        market = MarketData(SecurityStore(app.config["DATABASE"]), base_currency=app.config["BASE_CURRENCY"])
        app.extensions["market"] = market
    return market
