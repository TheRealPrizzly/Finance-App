"""Check that this machine can reach the services the app depends on.

Run it from a PythonAnywhere Bash console before deploying:
    python scripts/check_connectivity.py
"""
from datetime import date

import requests

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
    print("\nAll services reachable." if ok else "\nSome services are unreachable; the app will not work fully here.")


if __name__ == "__main__":
    main()
