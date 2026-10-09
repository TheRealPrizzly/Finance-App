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


def _major_unit(currency):
    major, divisor = MINOR_UNITS.get(currency, (currency, 1.0))
    return major, divisor


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

    def _get_json(self, url, params, crumb=False):
        for attempt in range(2):
            p = dict(params)
            if crumb:
                p["crumb"] = self._get_crumb(refresh=attempt > 0)
            r = self.session.get(url, params=p, timeout=self.timeout)
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
        """Daily closes between two dates. Returns (currency, {date: close})."""
        p1 = int(datetime.combine(start, dtime.min, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime.combine(end, dtime.min, tzinfo=timezone.utc).timestamp())
        data = self._get_json(
            f"{self.BASE1}/v8/finance/chart/{urlquote(symbol, safe='')}",
            {"period1": p1, "period2": p2, "interval": "1d"},
        )
        results = ((data or {}).get("chart") or {}).get("result") or []
        if not results:
            return None, {}
        res = results[0]
        meta = res.get("meta") or {}
        offset = meta.get("gmtoffset") or 0
        currency, divisor = _major_unit(meta.get("currency"))
        closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        out = {}
        for ts, close in zip(res.get("timestamp") or [], closes):
            if close is not None:
                out[datetime.fromtimestamp(ts + offset, tz=timezone.utc).date()] = close / divisor
        return currency, out

    def summary(self, symbol, modules):
        data = self._get_json(
            f"{self.BASE2}/v10/finance/quoteSummary/{urlquote(symbol, safe='')}",
            {"modules": ",".join(modules)},
            crumb=True,
        )
        results = ((data or {}).get("quoteSummary") or {}).get("result") or []
        return results[0] if results else {}


class MarketData:
    """Market data with caching. Prices are in each security's own currency;
    FX rates convert a currency into the base currency."""

    def __init__(self, store, client=None, base_currency="CAD"):
        self.store = store
        self.client = client or YahooClient()
        self.base = base_currency
        self._quotes = {}  # yahoo symbol -> (fetched_at, quote)
        self._history = {}  # yahoo symbol -> (fetched_at, start, currency, closes)
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
        price = q.get("regularMarketPrice")
        prev = q.get("regularMarketPreviousClose")
        data = {
            "symbol": q["symbol"].upper(),
            "price": None if price is None else price / divisor,
            "prev_close": None if prev is None else prev / divisor,
            "currency": currency,
            "name": q.get("longName") or q.get("shortName") or q["symbol"],
            "quote_type": q.get("quoteType"),
            "market_state": q.get("marketState"),
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

    def history(self, yahoo_symbol, start):
        """Daily closes since ``start``. Returns (currency, {date: close})."""
        cached = self._history.get(yahoo_symbol)
        if cached and cached[1] <= start and time.time() - cached[0] < HISTORY_TTL:
            return cached[2], cached[3]
        try:
            currency, closes = self.client.chart(yahoo_symbol, start, date.today() + timedelta(days=1))
        except requests.RequestException as exc:
            log.warning("History request for %s failed: %s", yahoo_symbol, exc)
            return (cached[2], cached[3]) if cached else (None, {})
        with self._lock:
            self._history[yahoo_symbol] = (time.time(), start, currency, closes)
        return currency, closes

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
