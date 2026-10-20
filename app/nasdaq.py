"""Analysts' consensus EPS forecasts from Nasdaq, for companies listed in the US.

Yahoo Finance has estimates for the current and next fiscal year only; Nasdaq's earnings forecast
goes up to four years ahead (EPS only, in US dollars). Fiscal periods come as "Sep 2026", the month
the period ends in.
"""
import logging
import re
import threading
import time
from datetime import datetime

import requests
from flask import current_app

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.nasdaq.com/api/analyst/{}/earnings-forecast"
FORECAST_TTL = 6 * 60 * 60
# Nasdaq's API turns away requests that don't look like they come from a browser.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36", "Accept": "application/json"}


def parse_forecast(data):
    """{"annual" | "quarterly": [{"month": "YYYY-MM" the period ends in, "eps", "analysts"}]}, oldest first."""
    out = {"annual": [], "quarterly": []}
    body = (data or {}).get("data") or {}
    for freq, key in (("annual", "yearlyForecast"), ("quarterly", "quarterlyForecast")):
        for row in (body.get(key) or {}).get("rows") or []:
            try:
                month = datetime.strptime(row.get("fiscalEnd") or "", "%b %Y").strftime("%Y-%m")
            except ValueError:
                continue
            if isinstance(row.get("consensusEPSForecast"), (int, float)):
                out[freq].append({"month": month, "eps": float(row["consensusEPSForecast"]), "analysts": row.get("noOfEstimates")})
        out[freq].sort(key=lambda r: r["month"])
    return out


class NasdaqData:
    """Nasdaq's earnings forecasts with caching."""

    def __init__(self, timeout=10):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout
        self._cache = {}  # symbol -> (fetched_at, forecast)
        self._lock = threading.Lock()

    def forecast(self, yahoo_symbol):
        """The forecast for a US-listed Yahoo symbol (AAPL; not SHOP.TO or BRK-B), or None."""
        if not re.fullmatch(r"[A-Z]{1,5}", yahoo_symbol or ""):
            return None
        cached = self._cache.get(yahoo_symbol)
        if cached and time.time() - cached[0] < FORECAST_TTL:
            return cached[1]
        try:
            r = self.session.get(FORECAST_URL.format(yahoo_symbol), timeout=self.timeout)
            r.raise_for_status()
            forecast = parse_forecast(r.json())
        except (requests.RequestException, ValueError) as exc:
            log.warning("Nasdaq forecast for %s failed: %s", yahoo_symbol, exc)
            return cached[1] if cached else None
        with self._lock:
            self._cache[yahoo_symbol] = (time.time(), forecast)
        return forecast


def get_nasdaq():
    """The app-wide Nasdaq service (tests can inject one, or None, via config["NASDAQ"])."""
    app = current_app._get_current_object()
    if "NASDAQ" in app.config:
        return app.config["NASDAQ"]
    return app.extensions.setdefault("nasdaq", NasdaqData())
