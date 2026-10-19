# Finance-App: Portfolio Manager

A multi-user web app for tracking investments across brokerage accounts in Canadian dollars. Features:

- Live prices
- Daily P/L
- Growth over time
- Returns compared with VFV.TO
- Sector allocation
- Cash balances per account
- A breakdown for each position
- CSV import and export
- A Markets tab: S&P 500 and Nasdaq futures, pre-market moves by sector, CNN's Fear & Greed Index and the US Treasury yield curve
- A Research tab: a price chart for any symbol on Yahoo Finance, from 1 day to its whole history or between two dates. PA (price action) mode draws a line with your buys and sells; TA (technical analysis) mode draws candlesticks with moving averages. Either can switch to candlesticks, a line, a line coloured by direction, or a baseline delta. Badges mark dividends, earnings (beat or missed) and splits. Optional RSI and volume panes, 10/20 EMAs, 200-week average, and other symbols to compare against. Below the chart, a Company section: financial statements, margins and valuation multiples (annual or quarterly), ownership by holder type, holder and insider, and capital returns (share count with EPS, dividends, and ROIC, ROE and ROCE against an estimated WACC)
- A My Assets tab: net worth from your portfolios, accounts and other assets, minus your mortgages, and a payoff projection for your mortgages, one at a time, side by side or combined

Requirements are tracked in [Project Requirements.md](Project%20Requirements.md).

## Run it locally (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env      # then edit .env
.\.venv\Scripts\python.exe run.py
```

Open http://localhost:5000. When `DEV_LOGIN=true`, you can click **Continue as local dev user** to try the app before Google sign-in is set up. Then import [samples/sample_transactions.csv](samples/sample_transactions.csv) from **Import / Export**.

## Set up Google sign-in

1. In the [Google Cloud Console](https://console.cloud.google.com/apis/credentials), create an **OAuth client ID** of type **Web application**. You'll be asked to configure the OAuth consent screen first.
2. Add the authorized redirect URI `http://localhost:5000/auth/google/callback`. Add your production URL's equivalent later.
3. Put the client ID and secret in `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
4. Set a random `SECRET_KEY`, and set `DEV_LOGIN=false` before anyone else can reach the app.
5. Optional: restrict sign-in to particular people with `ALLOWED_EMAILS=a@gmail.com,b@gmail.com`.

## How the numbers are calculated

- **Cash**: each account's balance in each currency is the sum of the *Net Amount* column.
- **Book cost**: uses the average-cost method. Foreign purchases are converted to CAD at the exchange rate on the purchase date, so P/L includes currency gains.
- **Returns**: these are time-weighted, so deposits and withdrawals don't distort them. That makes them directly comparable with VFV.TO.
- **Holdings-only mode**: if you import trades but no deposits or withdrawals, buys are treated as contributions and cash is left out of the total.
- **Net worth**: a portfolio or a set of accounts counts at its dashboard total value, at live prices. Other assets, such as a home, count at the value you enter. Mortgage balances are subtracted.
- **Mortgages**: interest is compounded semi-annually (the Canadian standard for fixed rates) or monthly. Without a set payment, the payment is the one that pays the balance off over the amortization; accelerated bi-weekly and weekly payments are the monthly payment split in two or four. If you enter a balance from a statement, the projection starts from it. Estimates assume the current rate until the mortgage is paid off. With **Continued contributions** on the payback chart, once a mortgage is paid off its payments go to the remaining mortgage with the highest rate, and the chart shows how much sooner they are all paid off and the interest saved.
- **Company figures** (Research tab): the income statement covers the last 10 fiscal years or 40 quarters, as filed with the US SEC (EDGAR's XBRL data), for companies that file there; a fourth quarter is its year less the first three, and periods newer than the latest filing come from Yahoo Finance. The other statements are Yahoo Finance's standardized figures, four fiscal years and five quarters, as is the income statement for companies that don't file with the SEC. All are in the company's reporting currency. Past valuation multiples use the market cap at each period's end (today's, scaled by the price and share count since) and the twelve months to it, converted into the trading currency at that day's exchange rate. Returns on capital use capital at each period's end. The WACC is an estimate: equity costs the 10-year US Treasury yield plus beta × a 5% equity risk premium, and debt costs its interest expense over total debt, after tax.
- **Symbols**: CAD symbols without a suffix, such as `XEQT`, are matched to their TSX listing automatically (`XEQT.TO`). Symbols Yahoo doesn't know are valued at their last trade price.

## Deploy to PythonAnywhere (free)

The app runs at `https://<username>.pythonanywhere.com`. In the steps below, replace `<username>` with your PythonAnywhere username.

1. **Push the code to GitHub.** A private repository is fine.
2. **Create a free account** at [pythonanywhere.com](https://www.pythonanywhere.com/).
3. **Open a Bash console** (Consoles → Bash) and run:
   ```bash
   git clone https://github.com/TheRealPrizzly/Finance-App.git
   cd Finance-App
   python3.13 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python scripts/check_connectivity.py   # every line should say OK
   cp .env.example .env
   nano .env
   ```
   For a private repository, git asks for a password. Use a GitHub personal access token with read access to the repository.

   In `.env`, set:
   - `SECRET_KEY`: a long random value
   - `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
   - `DEV_LOGIN=false`
   - `SESSION_COOKIE_SECURE=true`
   - `ALLOWED_EMAILS` (optional)
   - `SEC_CONTACT`: your email address, which the SEC asks every EDGAR request to carry (optional; without it, income statements show 4 years instead of 10)
4. **Create the web app** in the **Web** tab: Add a new web app → **Manual configuration** → **Python 3.13**. Then set:
   - **Source code:** `/home/<username>/Finance-App`
   - **Virtualenv:** `/home/<username>/Finance-App/.venv`
   - **Static files:** URL `/static/` → directory `/home/<username>/Finance-App/app/static`
   - **Force HTTPS:** on
5. **Edit the WSGI configuration file** (linked on the Web tab). Replace its contents with:
   ```python
   import sys

   path = "/home/<username>/Finance-App"
   if path not in sys.path:
       sys.path.insert(0, path)

   from wsgi import application  # noqa: E402
   ```
6. **Set up Google sign-in.** In Google Cloud Console, add the authorized redirect URI `https://<username>.pythonanywhere.com/auth/google/callback`. If the OAuth consent screen is in *Testing*, also add each person's Google account as a test user, or publish the app.
7. Click **Reload** on the Web tab and open the site.

**To deploy updates**, run `cd ~/Finance-App && git pull && .venv/bin/pip install -r requirements.txt` in a Bash console, then click **Reload**.

**Free-tier notes:**
- The web app is disabled after 3 months unless you click **Run until 3 months from today** on the Web tab.
- The database is `~/Finance-App/instance/portfolio.db`. To back it up, download it from the **Files** tab.
- Free web apps can't use threads, so market data is fetched one request at a time. The first dashboard load after adding new symbols takes a few seconds; after that, results are cached.
- If something fails, check the error log linked on the Web tab.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Project layout

| Path | Purpose |
|------|---------|
| `app/portfolio.py` | Calculation engine (pure functions) |
| `app/mortgage.py` | Mortgage payments and payoff projections (pure functions) |
| `app/market.py` | Yahoo Finance client and caching |
| `app/sec.py` | SEC EDGAR income statements and caching |
| `app/company.py` | Research tab's Company figures (pure functions) |
| `app/csv_io.py` | CSV import and export |
| `app/views.py` | Pages and JSON API |
| `app/auth.py` | Google sign-in, sessions and CSRF |
| `app/templates/`, `app/static/` | User interface |
