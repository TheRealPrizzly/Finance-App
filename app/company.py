"""The Research tab's Company section: financial statements, margins and valuation multiples,
ownership, and capital returns (share count, dividends and returns on capital), worked out from
the Yahoo Finance figures market.py fetches (pure functions).

Yahoo gives four fiscal years, five quarters and the trailing twelve months (TTM). Statement
figures are in the company's reporting currency, which can differ from the currency its shares
trade in (Shopify reports in USD but trades in CAD on the TSX); valuations convert them at the
exchange rate of each period's end."""
import bisect
from datetime import date

EQUITY_RISK_PREMIUM = 0.05  # assumed for the cost of equity in the WACC
INCOME_PERIODS = {"annual": 10, "quarterly": 40}  # the income statement's history: 10 years either way

# Statement lines: (Yahoo line item, label, format, style). Formats: "money" (reporting currency),
# "eps" (per share), "shares", "pct". Styles: "total" for subtotals, "sub" for the items under them.
INCOME_STATEMENT = (
    ("TotalRevenue", "Total revenue", "money", "total"),
    ("CostOfRevenue", "Cost of revenue", "money", "sub"),
    ("GrossProfit", "Gross profit", "money", "total"),
    ("ResearchAndDevelopment", "Research & development", "money", "sub"),
    ("SellingGeneralAndAdministration", "Selling, general & administrative", "money", "sub"),
    ("OperatingExpense", "Operating expenses", "money", "sub"),
    ("OperatingIncome", "Operating income", "money", "total"),
    ("InterestIncome", "Interest income", "money", "sub"),
    ("InterestExpense", "Interest expense", "money", "sub"),
    ("OtherIncomeExpense", "Other income (expense)", "money", "sub"),
    ("PretaxIncome", "Pretax income", "money", "total"),
    ("TaxProvision", "Income tax", "money", "sub"),
    ("TaxRateForCalcs", "Effective tax rate", "pct", "sub"),
    ("NetIncome", "Net income", "money", "total"),
    ("NetIncomeCommonStockholders", "Net income to common shareholders", "money", ""),
    ("EBIT", "EBIT", "money", ""),
    ("EBITDA", "EBITDA", "money", ""),
    ("BasicEPS", "EPS (basic)", "eps", ""),
    ("DilutedEPS", "EPS (diluted)", "eps", ""),
    ("BasicAverageShares", "Average shares (basic)", "shares", ""),
    ("DilutedAverageShares", "Average shares (diluted)", "shares", ""),
)
BALANCE_SHEET = (
    ("CashAndCashEquivalents", "Cash & equivalents", "money", "sub"),
    ("CashCashEquivalentsAndShortTermInvestments", "Cash & short-term investments", "money", "sub"),
    ("Receivables", "Receivables", "money", "sub"),
    ("Inventory", "Inventory", "money", "sub"),
    ("CurrentAssets", "Current assets", "money", "total"),
    ("NetPPE", "Property, plant & equipment", "money", "sub"),
    ("GoodwillAndOtherIntangibleAssets", "Goodwill & intangibles", "money", "sub"),
    ("TotalNonCurrentAssets", "Non-current assets", "money", "total"),
    ("TotalAssets", "Total assets", "money", "total"),
    ("PayablesAndAccruedExpenses", "Payables & accrued expenses", "money", "sub"),
    ("CurrentDebt", "Current debt", "money", "sub"),
    ("CurrentLiabilities", "Current liabilities", "money", "total"),
    ("LongTermDebt", "Long-term debt", "money", "sub"),
    ("TotalLiabilitiesNetMinorityInterest", "Total liabilities", "money", "total"),
    ("RetainedEarnings", "Retained earnings", "money", "sub"),
    ("StockholdersEquity", "Shareholders' equity", "money", "total"),
    ("MinorityInterest", "Minority interest", "money", ""),
    ("TotalDebt", "Total debt", "money", ""),
    ("NetDebt", "Net debt", "money", ""),
    ("WorkingCapital", "Working capital", "money", ""),
    ("InvestedCapital", "Invested capital", "money", ""),
    ("OrdinarySharesNumber", "Shares outstanding", "shares", ""),
)
CASH_FLOW = (
    ("DepreciationAndAmortization", "Depreciation & amortization", "money", "sub"),
    ("StockBasedCompensation", "Stock-based compensation", "money", "sub"),
    ("OperatingCashFlow", "Operating cash flow", "money", "total"),
    ("CapitalExpenditure", "Capital expenditure", "money", "sub"),
    ("InvestingCashFlow", "Investing cash flow", "money", "total"),
    ("CashDividendsPaid", "Dividends paid", "money", "sub"),
    ("RepurchaseOfCapitalStock", "Share buybacks", "money", "sub"),
    ("IssuanceOfCapitalStock", "Shares issued", "money", "sub"),
    ("IssuanceOfDebt", "Debt issued", "money", "sub"),
    ("RepaymentOfDebt", "Debt repaid", "money", "sub"),
    ("FinancingCashFlow", "Financing cash flow", "money", "total"),
    ("ChangesInCash", "Change in cash", "money", ""),
    ("EndCashPosition", "Cash at end of period", "money", ""),
    ("FreeCashFlow", "Free cash flow", "money", "total"),
)
FUNDAMENTAL_KEYS = tuple(key for rows in (INCOME_STATEMENT, BALANCE_SHEET, CASH_FLOW) for key, *_ in rows)

# Margins and valuation multiples: (key, label, format). "x" is a multiple; "mcap" is money in the
# currency the shares trade in.
MARGINS = (
    ("net", "Net income margin", "pct"),
    ("ebitda", "EBITDA margin", "pct"),
    ("ebit", "EBIT margin", "pct"),
    ("fcf", "FCF margin", "pct"),
)
VALUATION = (
    ("market_cap", "Market cap", "mcap"),
    ("ev", "Enterprise value", "mcap"),
    ("ps", "P/S", "x"),
    ("pe", "P/E", "x"),
    ("earnings_yield", "Earnings yield", "pct"),
    ("pocf", "P/OCF", "x"),
    ("pfcf", "P/FCF", "x"),
    ("fcf_yield", "FCF yield", "pct"),
    ("ev_sales", "EV/Sales", "x"),
    ("ev_ebitda", "EV/EBITDA", "x"),
    ("ev_ebit", "EV/EBIT", "x"),
    ("ev_ocf", "EV/OCF", "x"),
    ("ev_fcf", "EV/FCF", "x"),
)


def _ratio(num, den):
    """num / den, or None when either is missing or den isn't positive (a P/E on a loss means nothing)."""
    return None if num is None or den is None or den <= 0 else num / den


def _on_or_before(points, day, within=10):
    """The value in a sorted [(date, value)] list on ``day`` or the last one before it, at most
    ``within`` days earlier (a period can end on a weekend or holiday)."""
    i = bisect.bisect_right(points, (day, float("inf"))) - 1
    return points[i][1] if i >= 0 and (day - points[i][0]).days <= within else None


class Figures:
    """Look-ups into parsed fundamentals (see market.parse_timeseries) for a column of a table:
    {"kind": "annual" | "quarterly" | "ttm" | "current", "date": ISO period end}."""

    def __init__(self, fundamentals):
        self.data = fundamentals

    def series(self, freq, key):
        return (self.data.get(freq) or {}).get(key) or {}

    def at(self, col, key):
        """The figure as reported for the column's period."""
        return self.series("trailing" if col["kind"] == "ttm" else col["kind"], key).get(col["date"])

    def flow(self, col, key):
        """Twelve months of an income or cash flow figure ending at the column's date: the fiscal
        year, the TTM, or for a quarter the TTM to its end (the last four quarters added up)."""
        kind, day = col["kind"], col["date"]
        if kind == "annual":
            return self.series("annual", key).get(day)
        trailing = self.series("trailing", key)
        if kind == "current":
            return trailing[max(trailing)] if trailing else None
        if kind == "ttm" or day in trailing:
            return trailing.get(day)
        quarters = self.series("quarterly", key)
        ends = sorted(d for d in quarters if d <= day)[-4:]
        if len(ends) < 4 or ends[-1] != day:
            return None
        spans = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(ends, ends[1:])]
        return sum(quarters[d] for d in ends) if all(80 <= s <= 100 for s in spans) else None

    def balance(self, key):
        """A balance sheet item at each annual and quarterly period end."""
        return {**self.series("annual", key), **self.series("quarterly", key)}

    def stock(self, col, key):
        """A balance sheet item at the column's date; the latest one for the TTM and today."""
        values = self.balance(key)
        if col["kind"] in ("ttm", "current"):
            return values[max(values)] if values else None
        return values.get(col["date"])

    def columns(self, freq, keys):
        """The periods with figures for any of ``keys``, oldest first."""
        return [{"kind": freq, "date": d} for d in sorted({d for k in keys for d in self.series(freq, k)})]

    def ttm_column(self, keys, after):
        """The TTM column, if Yahoo has trailing figures for a period after the last fiscal year."""
        ends = [max(s) for s in (self.series("trailing", k) for k in keys) if s]
        latest = max(ends, default=None)
        return {"kind": "ttm", "date": latest} if latest and (not after or latest > after[-1]["date"]) else None


def _rows(spec, columns, value):
    """Table rows for spec lines (key, label, format[, style]), leaving out lines with no figures."""
    rows = []
    for key, label, fmt, *style in spec:
        values = [value(col, key) for col in columns]
        if any(v is not None for v in values):
            rows.append({"key": key, "label": label, "fmt": fmt, "style": style[0] if style else "", "values": values})
    return rows


def margins(fig, col):
    revenue = fig.flow(col, "TotalRevenue") if col["kind"] != "quarterly" else fig.at(col, "TotalRevenue")
    get = (lambda key: fig.at(col, key)) if col["kind"] == "quarterly" else (lambda key: fig.flow(col, key))
    return {"net": _ratio(get("NetIncome"), revenue), "ebitda": _ratio(get("EBITDA"), revenue),
            "ebit": _ratio(get("EBIT"), revenue), "fcf": _ratio(get("FreeCashFlow"), revenue)}


class Market:
    """What valuations need beyond the statements: today's market cap and price, split-adjusted
    daily closes [(date, close)], and the rate converting the reporting currency into the trading
    currency, today and as daily closes."""

    def __init__(self, market_cap=None, price=None, closes=(), fx_now=1.0, fx_closes=()):
        self.market_cap, self.price, self.fx_now = market_cap, price, fx_now
        self.closes, self.fx_closes = sorted(closes), sorted(fx_closes)

    def fx(self, col):
        if col["kind"] in ("ttm", "current") or not self.fx_closes:
            return self.fx_now
        return _on_or_before(self.fx_closes, date.fromisoformat(col["date"])) or self.fx_now


def market_cap(fig, mkt, col):
    """The market cap at a period's end: today's, scaled by the price and the share count since.
    Scaling today's figure (rather than price × shares) holds for ADRs and share classes, whose
    reported share counts aren't in the units the price is quoted in."""
    if col["kind"] in ("ttm", "current"):
        return mkt.market_cap
    if not (mkt.market_cap and mkt.price):
        return None
    price = _on_or_before(mkt.closes, date.fromisoformat(col["date"]))
    for key in ("OrdinarySharesNumber", "DilutedAverageShares"):
        shares = fig.balance(key)
        if shares.get(col["date"]) and price:
            return mkt.market_cap * price / mkt.price * shares[col["date"]] / shares[max(shares)]
    return None


def valuation(fig, mkt, col):
    """Price and enterprise value multiples at a period's end, on the twelve months to it."""
    mcap, fx = market_cap(fig, mkt, col), mkt.fx(col)
    if mcap is None or not fx:
        return {}
    debt = fig.stock(col, "TotalDebt")
    cash = fig.stock(col, "CashCashEquivalentsAndShortTermInvestments") or fig.stock(col, "CashAndCashEquivalents") or 0
    ev = mcap + (debt - cash + (fig.stock(col, "MinorityInterest") or 0)) * fx if debt is not None else None

    def flow(key):
        value = fig.flow(col, key)
        return None if value is None else value * fx

    revenue, ebitda, ebit = flow("TotalRevenue"), flow("EBITDA"), flow("EBIT")
    earnings = flow("NetIncomeCommonStockholders")
    earnings = flow("NetIncome") if earnings is None else earnings
    ocf, fcf = flow("OperatingCashFlow"), flow("FreeCashFlow")
    return {
        "market_cap": mcap, "ev": ev,
        "ps": _ratio(mcap, revenue), "pe": _ratio(mcap, earnings),
        "earnings_yield": None if earnings is None else earnings / mcap,
        "pocf": _ratio(mcap, ocf), "pfcf": _ratio(mcap, fcf), "fcf_yield": None if fcf is None else fcf / mcap,
        "ev_sales": _ratio(ev, revenue), "ev_ebitda": _ratio(ev, ebitda), "ev_ebit": _ratio(ev, ebit),
        "ev_ocf": _ratio(ev, ocf), "ev_fcf": _ratio(ev, fcf),
    }


def _same_period(a, b):
    """Whether two period ends are the same period (Apple's fiscal year ends on the last Saturday of
    September, which Yahoo calls September 30)."""
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= 10


def income_statement(fig, freq, filed=None):
    """The income statement table for the last INCOME_PERIODS periods, with the TTM after the years.

    With ``filed`` (SEC filings) going back further than Yahoo, the history comes from the filings,
    and periods Yahoo has that the SEC doesn't yet are added after it (each column's "source" says
    which). In those, a line is left blank where the two sources disagree on the periods they share:
    Yahoo's EPS for an ADR is per ADR, not per share, and its EBIT is operating income."""
    keys = [k for k, *_ in INCOME_STATEMENT]
    yahoo_cols = [{**c, "source": "yahoo"} for c in fig.columns(freq, keys)]
    sec = Figures(filed) if filed else None
    sec_cols = [{**c, "source": "sec"} for c in sec.columns(freq, keys)] if sec else []
    use_sec = len(sec_cols) > len(yahoo_cols)
    base = sec_cols if use_sec else yahoo_cols
    newer = [c for c in yahoo_cols if c["date"] > base[-1]["date"] and not _same_period(c["date"], base[-1]["date"])] if use_sec else []
    cols = (base + newer)[-INCOME_PERIODS[freq]:]
    if freq == "annual" and cols:
        options = [{**t, "source": src} for t, src in ((sec.ttm_column(keys, sec_cols) if use_sec else None, "sec"),
                                                        (fig.ttm_column(keys, yahoo_cols), "yahoo")) if t]
        latest = max(options, key=lambda t: t["date"], default=None)
        if latest and latest["date"] > cols[-1]["date"]:  # the filings' TTM when both run to the same quarter
            cols.append(next((t for t in options if t["source"] == "sec" and _same_period(t["date"], latest["date"])), latest))

    def agrees(key):
        shared = [(sec.at(a, key), fig.at(b, key)) for a in sec_cols for b in yahoo_cols if _same_period(a["date"], b["date"])]
        return all(abs(x - y) <= 0.02 * max(abs(x), abs(y)) for x, y in shared if x is not None and y is not None)

    trusted = {key: agrees(key) for key in keys} if use_sec else {}
    yahoo_ttm = fig.ttm_column(keys, yahoo_cols)

    def value(col, key):
        if col["source"] == "sec":
            found = sec.at(col, key)
            # A TTM through a fourth quarter has no per-share figures (they aren't filed for it): Yahoo's
            # TTM to the same quarter fills them in, where the sources agree.
            if found is None and col["kind"] == "ttm" and yahoo_ttm and _same_period(col["date"], yahoo_ttm["date"]) and trusted[key]:
                found = fig.at(yahoo_ttm, key)
            return found
        return fig.at(col, key) if not use_sec or trusted[key] else None

    return {"columns": cols, "rows": _rows(INCOME_STATEMENT, cols, value),
            "source": "sec" if use_sec else "yahoo", "currency": (filed if use_sec else fig.data).get("currency")}


def financials(fig, mkt, today, filed=None):
    """Each statement, the margins and the valuation multiples as tables for the annual and the
    quarterly view: {"annual" | "quarterly": {name: {"columns": [...], "rows": [...]}}}. The income
    statement comes from ``filed`` (SEC filings, see sec.parse_company_facts) when they go back further
    than Yahoo's figures; its "source" says which ("sec" or "yahoo") and "currency" what it's in."""
    flows = [k for k, *_ in INCOME_STATEMENT + CASH_FLOW]
    out = {}
    for freq in ("annual", "quarterly"):
        income, balance, cash = (fig.columns(freq, [k for k, *_ in spec]) for spec in (INCOME_STATEMENT, BALANCE_SHEET, CASH_FLOW))
        ttm = fig.ttm_column(flows, income) if freq == "annual" else None
        statement = income_statement(fig, freq, filed)
        with_ttm = lambda cols: cols + [ttm] if ttm else cols  # noqa: E731
        periods = sorted({c["date"] for c in income + cash})
        flow_cols = [{"kind": freq, "date": d} for d in periods]
        margin_cols = [c for c in with_ttm(flow_cols) if any(v is not None for v in margins(fig, c).values())]
        value_cols = [c for c in flow_cols if valuation(fig, mkt, c).get("market_cap") is not None]
        value_cols += [{"kind": "current", "date": today.isoformat()}] if mkt.market_cap else []
        margin_vals = [margins(fig, c) for c in margin_cols]
        value_vals = [valuation(fig, mkt, c) for c in value_cols]
        out[freq] = {
            "income": statement,
            "balance": {"columns": balance, "rows": _rows(BALANCE_SHEET, balance, fig.at)},
            "cash": {"columns": with_ttm(cash), "rows": _rows(CASH_FLOW, with_ttm(cash), fig.at)},
            "margins": {"columns": margin_cols,
                        "rows": _rows(MARGINS, margin_cols, lambda c, k: margin_vals[margin_cols.index(c)][k])},
            "valuation": {"columns": value_cols,
                          "rows": _rows(VALUATION, value_cols, lambda c, k: value_vals[value_cols.index(c)].get(k))},
        }
    return out


def share_counts(fig):
    """Average basic and diluted shares, with EPS, for each fiscal year and quarter."""
    keys = ("BasicAverageShares", "DilutedAverageShares", "BasicEPS", "DilutedEPS")
    out = {}
    for freq in ("annual", "quarterly"):
        cols = fig.columns(freq, keys[:2])
        out[freq] = [{"date": c["date"], "basic": fig.at(c, keys[0]), "diluted": fig.at(c, keys[1]),
                      "basic_eps": fig.at(c, keys[2]), "diluted_eps": fig.at(c, keys[3])} for c in cols]
    return out


def _cagr(first, last, years):
    return (last / first) ** (1 / years) - 1 if first and last and first > 0 and last > 0 else None


def dividends(fig, facts, paid, forward_rate, forward_yield, today):
    """Dividend yields, payout and coverage, and growth. ``paid`` is [(ex-dividend date, amount per
    share)], adjusted for splits; years are calendar years, and this year's is still partial."""
    by_year, last_paid = {}, {}
    for day, amount in sorted(paid):
        by_year[day.year] = by_year.get(day.year, 0) + amount
        last_paid[day.year] = amount
    # Growth needs two years before it: a stock's first year of dividends is usually partial.
    years = [{"year": y, "amount": by_year[y], "partial": y == today.year,
              "growth": by_year[y] / by_year[y - 1] - 1
              if y != today.year and by_year.get(y - 1) and by_year.get(y - 2) else None}
             for y in sorted(by_year)]
    full = [y for y in years if not y["partial"]]
    last = full[-1] if full and today.year - full[-1]["year"] <= 1 else None
    growth = {}
    streak = 0
    if last:
        for n in (1, 3, 5, 10):
            growth[f"{n}Y"] = _cagr(by_year.get(last["year"] - n), last["amount"], n) if by_year.get(last["year"] - n - 1) else None
        # Raises are counted on each year's last payment, the regular rate: totals by calendar year
        # jump about when a payment's ex-date slips into the next year.
        for y in reversed(full):
            if not last_paid.get(y["year"] - 1) or last_paid[y["year"]] <= last_paid[y["year"] - 1] * 1.001:
                break
            streak += 1
    ttm_dividends = sum(a for d, a in paid if (today - d).days < 365)

    ttm = {"kind": "current", "date": today.isoformat()}
    payout = (facts or {}).get("dividend", {}).get("payout_ratio")
    dividends_paid, fcf = fig.flow(ttm, "CashDividendsPaid"), fig.flow(ttm, "FreeCashFlow")
    fcf_payout = _ratio(abs(dividends_paid), fcf) if dividends_paid else None
    history_cols = fig.columns("annual", ["CashDividendsPaid"])
    history = []
    for col in history_cols:
        cash_paid = abs(fig.at(col, "CashDividendsPaid") or 0)
        earnings = fig.at(col, "NetIncomeCommonStockholders") or fig.at(col, "NetIncome")
        history.append({"date": col["date"], "paid": cash_paid, "net_income": earnings, "fcf": fig.at(col, "FreeCashFlow"),
                        "payout": _ratio(cash_paid, earnings) if cash_paid else None,
                        "fcf_payout": _ratio(cash_paid, fig.at(col, "FreeCashFlow")) if cash_paid else None})
    return {
        "forward_rate": forward_rate,
        "forward_yield": forward_yield if forward_rate else None,  # a fund's "yield" is its trailing one
        "ttm_amount": ttm_dividends or None,
        "trailing_yield": (facts or {}).get("dividend", {}).get("trailing_yield"),
        "five_year_yield": (facts or {}).get("dividend", {}).get("five_year_yield"),
        "ex_date": (facts or {}).get("dividend", {}).get("ex_date"),
        "payout_ratio": payout or None,
        "coverage": 1 / payout if payout else None,
        "fcf_payout": fcf_payout,
        "fcf_coverage": 1 / fcf_payout if fcf_payout else None,
        "growth": growth,
        "streak": streak,
        "years": years,
        "history": history,
    }


def _tax_rate(fig, col):
    rate = fig.at(col, "TaxRateForCalcs") if col["kind"] == "annual" else None
    if rate is None:
        rates = fig.series("trailing" if col["kind"] == "ttm" else "annual", "TaxRateForCalcs") or fig.series("annual", "TaxRateForCalcs")
        rate = rates[max(rates)] if rates else None
    if rate is None:
        rate = _ratio(fig.flow(col, "TaxProvision"), fig.flow(col, "PretaxIncome"))
    return min(max(rate, 0.0), 1.0) if rate is not None else None


def returns_on_capital(fig, col):
    """ROIC (EBIT after tax over equity plus debt), ROE (earnings to common shareholders over
    their equity) and ROCE (EBIT over total assets less current liabilities), on capital at the
    period's end."""
    ebit, tax = fig.flow(col, "EBIT"), _tax_rate(fig, col)
    earnings = fig.flow(col, "NetIncomeCommonStockholders")
    earnings = fig.flow(col, "NetIncome") if earnings is None else earnings
    assets, current = fig.stock(col, "TotalAssets"), fig.stock(col, "CurrentLiabilities")
    return {
        "roic": _ratio(ebit * (1 - tax), fig.stock(col, "InvestedCapital")) if ebit is not None and tax is not None else None,
        "roe": _ratio(earnings, fig.stock(col, "StockholdersEquity")),
        "roce": _ratio(ebit, assets - current) if assets is not None and current is not None else None,
    }


def wacc(fig, mkt, beta, risk_free, today):
    """The weighted average cost of capital, estimated: equity at today's market cap costs the risk-free
    rate plus beta times EQUITY_RISK_PREMIUM; debt costs its interest expense over the debt (the
    risk-free rate if no interest is reported), less tax."""
    if beta is None or risk_free is None or not mkt.market_cap or not mkt.fx_now:
        return None
    now = {"kind": "current", "date": today.isoformat()}
    debt = (fig.stock(now, "TotalDebt") or 0) * mkt.fx_now
    interest = fig.flow(now, "InterestExpense")
    if interest is None:
        interest = (lambda s: s[max(s)] if s else None)(fig.series("annual", "InterestExpense"))
    reported = bool(interest and debt)
    cost_of_debt = abs(interest) * mkt.fx_now / debt if reported else risk_free
    tax = _tax_rate(fig, {"kind": "ttm", "date": now["date"]}) or 0.0
    cost_of_equity = risk_free + beta * EQUITY_RISK_PREMIUM
    weight_equity = mkt.market_cap / (mkt.market_cap + debt)
    return {
        "wacc": weight_equity * cost_of_equity + (1 - weight_equity) * cost_of_debt * (1 - tax),
        "cost_of_equity": cost_of_equity, "cost_of_debt": cost_of_debt, "debt_cost_reported": reported,
        "tax_rate": tax, "weight_equity": weight_equity, "beta": beta, "risk_free": risk_free,
        "risk_premium": EQUITY_RISK_PREMIUM,
    }


def capital_returns(fig, mkt, beta, risk_free, today):
    keys = ("EBIT", "NetIncome", "NetIncomeCommonStockholders")
    cols = fig.columns("annual", keys)
    ttm = fig.ttm_column(keys, cols)
    cols = cols + [ttm] if ttm else cols
    points = [{"date": c["date"], "kind": c["kind"], **returns_on_capital(fig, c)} for c in cols]
    points = [p for p in points if any(p[k] is not None for k in ("roic", "roe", "roce"))]
    # Without EBIT (banks and insurers) there's no ROIC to set against the WACC, and a bank's "debt"
    # includes its deposits, which would make the WACC meaningless anyway.
    cost = wacc(fig, mkt, beta, risk_free, today) if any(p["roic"] is not None for p in points) else None
    latest_roic = next((p["roic"] for p in reversed(points) if p["roic"] is not None), None)
    return {"points": points, "wacc": cost,
            "spread": latest_roic - cost["wacc"] if cost and latest_roic is not None else None}


def ownership(facts):
    """Holdings by type (insiders, institutions, everyone else), the largest institutions and
    funds, and the insiders Yahoo lists with their share of the shares outstanding."""
    if not facts:
        return None
    outstanding = facts.get("shares_outstanding")
    insiders, institutions = facts.get("insiders"), facts.get("institutions")
    held = (insiders or 0) + (institutions or 0)
    return {
        "insiders": insiders, "institutions": institutions,
        # Yahoo's institutional figure can top 100% (shares lent to short sellers count twice).
        "public": max(0.0, 1 - held) if insiders is not None or institutions is not None else None,
        "institutions_float": facts.get("institutions_float"),
        "institutions_count": facts.get("institutions_count"),
        "shares_outstanding": outstanding,
        "top_institutions": facts.get("top_institutions") or [],
        "top_funds": facts.get("top_funds") or [],
        "insider_holders": sorted(({**h, "pct": h["shares"] / outstanding if outstanding else None}
                                   for h in facts.get("insider_holders") or []), key=lambda h: -h["shares"]),
    }


def company_report(fundamentals, facts, stats, closes, paid_dividends, fx_now, fx_closes, risk_free, today=None, filed=None):
    """Everything the Company section shows. ``stats`` is market.key_stats() (market cap, price,
    beta, forward dividend), ``closes`` and ``paid_dividends`` the split-adjusted daily closes and
    dividends [(date, value)], ``fx_now``/``fx_closes`` the reporting-to-trading currency rate, and
    ``filed`` the income statement from SEC filings, if the company files there."""
    today = today or date.today()
    stats = stats or {}
    fig = Figures(fundamentals or {})
    mkt = Market(stats.get("market_cap"), stats.get("price"), closes, fx_now, fx_closes)
    has_statements = any((fundamentals or {}).get(f) for f in ("annual", "quarterly"))
    return {
        "currency": stats.get("currency"),
        "financial_currency": (facts or {}).get("financial_currency") or (fundamentals or {}).get("currency"),
        "financials": financials(fig, mkt, today, filed) if has_statements else None,
        "ownership": ownership(facts),
        "shares": share_counts(fig) if has_statements else None,
        "dividends": dividends(fig, facts, paid_dividends, stats.get("dividend"), stats.get("yield"), today),
        "returns": capital_returns(fig, mkt, stats.get("beta"), risk_free, today) if has_statements else None,
    }
