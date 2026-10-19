"""Check that this machine can reach the services the app depends on.

Run it from a PythonAnywhere Bash console before deploying:
    python scripts/check_connectivity.py
"""
import os
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
CHECKS = [
    ("Google sign-in metadata", "https://accounts.google.com/.well-known/openid-configuration"),
    ("Google token endpoint", "https://oauth2.googleapis.com/token"),
    ("Yahoo Finance chart (VFV.TO)", "https://query1.finance.yahoo.com/v8/finance/chart/VFV.TO?range=5d&interval=1d"),
    ("Yahoo Finance FX (USDCAD=X)", "https://query1.finance.yahoo.com/v8/finance/chart/USDCAD=X?range=5d&interval=1d"),
    ("Yahoo Finance crumb", "https://query2.finance.yahoo.com/v1/test/getcrumb"),
    # Markets tab. Without these it still works: visitors' browsers fetch Fear & Greed from CNN
    # themselves, and the yield curve falls back to the four Treasury yields Yahoo quotes.
    ("CNN Fear & Greed Index", "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"),
    ("US Treasury yield curve", "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                                f"daily-treasury-rates.csv/{date.today().year}/all?type=daily_treasury_yield_curve&_format=csv"),
]


def main():
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    try:
        session.get("https://fc.yahoo.com", timeout=15)  # sets the cookie the crumb check needs
    except requests.RequestException:
        pass
    ok = True
    for name, url in CHECKS:
        try:
            r = session.get(url, timeout=15)
            # The token endpoint only accepts POSTs; any answer at all proves it is reachable.
            good = r.status_code < 400 or url.endswith("/token")
            print(f"{'OK  ' if good else 'FAIL'} {name}: HTTP {r.status_code}")
            ok &= good
        except requests.RequestException as exc:
            print(f"FAIL {name}: {exc}")
            ok = False
    ok &= check_sec()
    print("\nAll services reachable." if ok else "\nSome services are unreachable; the app will not work fully here.")


def check_sec():
    """The SEC's EDGAR data, for 10-year income statements. It needs SEC_CONTACT (an email address) in .env."""
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if not contact:
        print("SKIP SEC EDGAR: SEC_CONTACT isn't set in .env, so income statements show Yahoo's 4 years")
        return True
    session = requests.Session()
    session.headers["User-Agent"] = f"Finance-App {contact}"
    good = True
    for name, url in (("SEC EDGAR tickers", "https://www.sec.gov/files/company_tickers.json"),
                      ("SEC EDGAR company facts (AAPL)", "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")):
        try:
            r = session.get(url, timeout=30)
            hint = " (EDGAR refused it: check SEC_CONTACT is an email address)" if r.status_code in (403, 429) else ""
            print(f"{'OK  ' if r.ok else 'FAIL'} {name}: HTTP {r.status_code}{hint}")
            good &= r.ok
        except requests.RequestException as exc:
            print(f"FAIL {name}: {exc}")
            good = False
    return good


if __name__ == "__main__":
    main()
