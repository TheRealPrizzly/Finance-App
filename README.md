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
| `app/market.py` | Yahoo Finance client and caching |
| `app/csv_io.py` | CSV import and export |
| `app/views.py` | Pages and JSON API |
| `app/auth.py` | Google sign-in, sessions and CSRF |
| `app/templates/`, `app/static/` | User interface |
