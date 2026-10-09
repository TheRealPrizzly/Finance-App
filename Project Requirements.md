# Project Requirements — Portfolio Manager

> Single source of truth for what this app must do. Update it as requirements are added or changed.
> Mark unresolved items with `TBD`.

**Last updated:** 2026-10-09

---

## 1. Overview

- **Purpose:** A Portfolio Manager for tracking and analyzing investments.
- **Target users:** Multiple users. Authentication using Google Authentication.
- **Problem it solves:** TBD

## 2. Core Features

| ID | Feature | Description | Priority | Status |
|----|---------|-------------|----------|--------|
| F1 | Transactions | Record buy, sell, dividend, deposit, withdrawal, fee, interest, FX and transfer activity, including costs | Must | Done |
| F2 | Holdings | Holdings derived from transactions (stocks, ETFs, crypto, cash); average-cost book value | Must | Done |
| F3 | Live prices | Live quotes, refreshed every 60 s on the dashboard | Must | Done |
| F4 | Performance | Gain/loss, time-weighted returns for 1M, 3M, 6M, YTD, 1Y and since inception | Must | Done |
| F5 | Portfolio growth over time | Daily value vs. net contributions chart | Must | Done |
| F6 | Returns vs VFV.TO | Return comparison, plus "same contributions invested in VFV.TO" | Must | Done |
| F7 | Daily P/L % | Today's P/L for the portfolio and each holding | Must | Done |
| F8 | Sector allocation | Breakdown by sector; ETFs looked through to their holdings | Must | Done |
| F9 | Multiple accounts | Each account works as a sub-portfolio; dashboard filters by account | Must | Done |
| F10 | Cash balances | Cash per account and currency, converted to CAD | Must | Done |
| F11 | Position breakdown | Per-symbol page: price chart with trades, cost basis per account, realized P/L, dividends, total return | Must | Done |
| F12 | Import / export | CSV in the format in section 3; duplicate rows are skipped on re-import | Must | Done |

## 3. Data & Integrations

- **Asset types supported:** Stocks, ETFs, crypto and anything else Yahoo Finance quotes; cash in any currency
- **Market data source / API:** Yahoo Finance (unofficial endpoints, no API key). Unofficial: may change or rate-limit without notice
- **Base currency & multi-currency support:** Base currency is CAD (Canadian dollars) and should allow for multi-currency support
- **Data import/export formats:** Transaction Date, Action,	Symbol, Quantity, Price, Costs, Net Amount, Currency, Account #, Activity Type, Account Type

## 4. Technical Requirements

- **Platform:** Web (responsive, works on phones)
- **Frontend:** Server-rendered Jinja templates, vanilla JavaScript, Chart.js 4.4.1 (vendored)
- **Backend:** Python 3 + Flask; Google sign-in via Authlib (OpenID Connect)
- **Database / storage:** SQLite (`instance/portfolio.db`)
- **Hosting / deployment:** PythonAnywhere free tier (`<username>.pythonanywhere.com`). Setup steps are in the README

## 5. Non-Functional Requirements

- **Security & privacy:** Google sign-in, optional email allow-list, per-user data isolation, CSRF protection, HTTP-only session cookies. Encryption of data at rest: TBD
- **Performance:** TBD
- **Accessibility / UI:** TBD

## 6. Out of Scope

- TBD

## 7. Open Questions

- Problem statement (section 1).
- If usage outgrows the PythonAnywhere free tier, move to a paid plan, or to Render + Postgres.
- Encryption of financial data at rest.
- Corporate actions (splits, mergers) and in-kind transfers with no price are not adjusted automatically.

## 8. Change Log

| Date | Change |
|------|--------|
| 2026-10-09 | File created |
| 2026-10-09 | First version of the web app built; features, data source and tech stack recorded |
| 2026-10-09 | Hosting decided: PythonAnywhere free tier |
