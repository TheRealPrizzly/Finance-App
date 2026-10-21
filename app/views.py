"""Pages and JSON endpoints for the signed-in user's portfolio.

Every query is scoped to ``g.user`` so users only ever see their own data.
"""
import re
from datetime import date, datetime, timedelta
from fractions import Fraction
from urllib.parse import urlsplit

from flask import (
    Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for,
)

from . import budget as bg
from . import company as co
from . import csv_io
from . import mortgage as mg
from . import portfolio as pf
from .auth import login_required
from .db import get_db
from .macro import (
    FEAR_GREED_COMPARE, FEAR_GREED_INDICATORS, FEAR_GREED_PAGE, FEAR_GREED_URL, Unavailable, get_macro,
)
from .market import INTRADAY_SPANS, MarketData, get_market
from .nasdaq import get_nasdaq
from .sec import get_sec

bp = Blueprint("views", __name__)
PAGE_SIZE = 100
TXN_PREVIEW = 10  # transactions shown on the Accounts page until "Show all"
# Colour themes: key -> (name, main colours). The colours mirror the theme's --bg, --text and
# --accent tokens in style.css, and the theme picker shows them as swatches.
THEMES = {
    "white": ("White - Standard", ("#f5f6f8", "#15181e", "#2457d6")),
    "dark-grey": ("Dark grey", ("#26282c", "#ececee", "#7aa2ff")),
}
CASH_KINDS = (pf.DIVIDEND, pf.DEPOSIT, pf.WITHDRAWAL, pf.TRANSFER, pf.FEE, pf.INTEREST, pf.FX, pf.OTHER)


# -- template helpers -----------------------------------------------------------


@bp.app_template_filter("money")
def money_filter(value, currency="CAD"):
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f} {currency}"


@bp.app_template_filter("amount")
def amount_filter(value):
    """Money in the base currency without the currency code, for pages that say which it is."""
    if value is None:
        return "—"
    return f"{'-' if value < -0.005 else ''}${abs(value):,.2f}"


@bp.app_template_filter("long_date")
def long_date_filter(value, day=True):
    if not value:
        return "—"
    d = date.fromisoformat(str(value)[:10])
    return f"{d:%b} {d.day}, {d.year}" if day else f"{d:%B %Y}"


@bp.app_template_filter("qty")
def qty_filter(value):
    if not value:
        return "—"
    return f"{value:,.6f}".rstrip("0").rstrip(".")


@bp.app_context_processor
def inject_globals():
    return {"kind_labels": pf.KIND_LABELS, "base_currency": current_app.config["BASE_CURRENCY"], "themes": THEMES,
            # the account / portfolio picked on this page, for links that keep it
            "scope_args": {k: request.args[k] for k in ("account", "portfolio", "all") if request.args.get(k)}}


# -- data access ------------------------------------------------------------------


def account_label(row):
    return f"{row['name'] or row['type'] or 'Account'} · {row['number']}"


def user_accounts():
    return get_db().execute(
        "SELECT * FROM accounts WHERE user_id = ? ORDER BY type, number", (g.user["id"],)
    ).fetchall()


def account_labels():
    return {a["id"]: account_label(a) for a in user_accounts()}


def get_account_or_404(account_id):
    row = get_db().execute(
        "SELECT * FROM accounts WHERE id = ? AND user_id = ?", (account_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def selected_account_id():
    raw = request.args.get("account", "")
    if not raw:
        return None
    if not raw.isdigit():
        abort(400)
    return get_account_or_404(int(raw))["id"]


def user_portfolios():
    return get_db().execute("SELECT * FROM portfolios WHERE user_id = ? ORDER BY name", (g.user["id"],)).fetchall()


def selected_portfolio_id():
    raw = request.args.get("portfolio", "")
    if not raw:
        return None
    if not raw.isdigit():
        abort(400)
    return get_portfolio_or_404(int(raw))["id"]


def selected_account_ids():
    """Accounts in view: the selected account, the selected portfolio's accounts, or None for all."""
    account_id = selected_account_id()
    if account_id:
        return [account_id]
    portfolio_id = selected_portfolio_id()
    if portfolio_id:
        return scope_account_ids("portfolio", portfolio_id)
    return None


def scope_account_ids(kind, scope_id):
    """The accounts in an account ("account", id) or portfolio ("portfolio", id) scope."""
    if kind == "account":
        return [scope_id]
    return [r["account_id"] for r in get_db().execute(
        "SELECT account_id FROM portfolio_accounts WHERE portfolio_id = ?", (scope_id,)
    )]


def default_scope():
    """The user's saved default view as ("account" | "portfolio", id), or None for all
    accounts. A default whose account or portfolio was deleted counts as none."""
    kind, _, raw_id = (g.user["default_scope"] or "").partition(":")
    if kind not in ("account", "portfolio") or not raw_id.isdigit():
        return None
    table = "accounts" if kind == "account" else "portfolios"
    row = get_db().execute(f"SELECT id FROM {table} WHERE id = ? AND user_id = ?", (int(raw_id), g.user["id"])).fetchone()
    return (kind, row["id"]) if row else None


def scope_template_args():
    """What the dashboard and position pages need to render the account/portfolio picker.
    "all" marks an explicit All accounts choice, so links keep it instead of the default."""
    default = default_scope()
    return {"accounts": user_accounts(), "portfolios": user_portfolios(),
            "account_id": selected_account_id(), "portfolio_id": selected_portfolio_id(),
            "all_selected": bool(request.args.get("all")),
            "default_scope": f"{default[0]}:{default[1]}" if default else ""}


def load_txns(account_ids=None, symbol=None):
    if account_ids is not None and not account_ids:
        return []
    sql, params = "SELECT * FROM transactions WHERE user_id = ?", [g.user["id"]]
    if account_ids is not None:
        sql += f" AND account_id IN ({', '.join('?' * len(account_ids))})"
        params.extend(account_ids)
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    return [pf.Txn.from_row(r) for r in get_db().execute(sql + " ORDER BY date, id", params)]


def has_transactions():
    return get_db().execute("SELECT 1 FROM transactions WHERE user_id = ? LIMIT 1", (g.user["id"],)).fetchone() is not None


# -- dashboard & positions ------------------------------------------------------------


def redirect_to_default_view(endpoint):
    """With no account or portfolio picked, a redirect to the user's default view, if any."""
    if not any(request.args.get(k) for k in ("account", "portfolio", "all")):
        default = default_scope()
        if default:
            return redirect(url_for(endpoint, **{default[0]: default[1]}))
    return None


@bp.get("/")
@login_required
def dashboard():
    if (resp := redirect_to_default_view("views.dashboard")) is not None:
        return resp
    return render_template(
        "dashboard.html",
        **scope_template_args(),
        has_transactions=has_transactions(),
        has_assets=has_assets(),
        benchmark=current_app.config["BENCHMARK_SYMBOL"],
    )


@bp.get("/performance")
@login_required
def performance():
    if (resp := redirect_to_default_view("views.performance")) is not None:
        return resp
    return render_template(
        "performance.html",
        **scope_template_args(),
        has_transactions=has_transactions(),
        benchmark=current_app.config["BENCHMARK_SYMBOL"],
    )


@bp.get("/markets")
@login_required
def markets():
    # The browser reads CNN's feed itself (see app.js), so it needs the feed URL and labels.
    return render_template(
        "markets.html", fear_greed_url=FEAR_GREED_PAGE, fear_greed_feed=FEAR_GREED_URL,
        fear_greed_meta={"indicators": FEAR_GREED_INDICATORS, "compare": FEAR_GREED_COMPARE},
    )


@bp.get("/macros")
def old_markets_address():
    """The Markets tab was called Macros; keep old bookmarks working."""
    return redirect(url_for("views.markets"), 301)


def macro_json(fetch):
    try:
        return jsonify(fetch())
    except Unavailable as exc:
        return jsonify(error=str(exc)), 502


@bp.get("/api/markets/premarket")
@login_required
def api_premarket():
    return macro_json(get_macro().premarket)


@bp.get("/api/markets/sentiment")
@login_required
def api_sentiment():
    return macro_json(get_macro().sentiment)


@bp.get("/api/markets/currencies")
@login_required
def api_currencies():
    return macro_json(get_macro().currencies)


@bp.get("/api/markets/rates")
@login_required
def api_rates():
    return macro_json(get_macro().rates)


@bp.get("/position/<symbol>")
@login_required
def position(symbol):
    symbol = symbol.upper()
    exists = get_db().execute(
        "SELECT 1 FROM transactions WHERE user_id = ? AND symbol = ? LIMIT 1", (g.user["id"], symbol)
    ).fetchone()
    if not exists:
        abort(404)
    return render_template("position.html", symbol=symbol, **scope_template_args())


def visible_warnings(warnings):
    """Warnings the user hasn't dismissed."""
    dismissed = {r["message"] for r in get_db().execute(
        "SELECT message FROM dismissed_warnings WHERE user_id = ?", (g.user["id"],))}
    return [w for w in warnings if w not in dismissed]


@bp.post("/api/warnings/dismiss")
@login_required
def dismiss_warning():
    message = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not message:
        return jsonify(error="No message given."), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO dismissed_warnings (user_id, message) VALUES (?, ?)", (g.user["id"], message))
    db.commit()
    return jsonify(ok=True)


@bp.get("/api/portfolio")
@login_required
def api_portfolio():
    data = pf.snapshot(
        load_txns(selected_account_ids()), account_labels(), get_market(), current_app.config["BASE_CURRENCY"]
    )
    data["warnings"] = visible_warnings(data["warnings"])
    data["as_of"] = datetime.now().isoformat(timespec="seconds")
    return jsonify(data)


@bp.get("/api/history")
@login_required
def api_history():
    return jsonify(pf.history(
        load_txns(selected_account_ids()), get_market(),
        current_app.config["BASE_CURRENCY"], current_app.config["BENCHMARK_SYMBOL"],
        account_labels=account_labels(),
    ))


@bp.get("/api/position/<symbol>")
@login_required
def api_position(symbol):
    symbol = symbol.upper()
    data = pf.position_detail(
        load_txns(selected_account_ids()), symbol, account_labels(), get_market(),
        current_app.config["BASE_CURRENCY"],
    )
    if data is None:
        return jsonify(error="No transactions for this symbol in the selected account."), 404
    data["warnings"] = visible_warnings(data["warnings"])
    rows = get_db().execute(
        """SELECT t.*, a.number, a.name, a.type FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.user_id = ? AND t.symbol = ? ORDER BY t.date DESC, t.id DESC""",
        (g.user["id"], symbol),
    ).fetchall()
    account_ids = selected_account_ids()
    data["transactions"] = [
        {
            "id": r["id"], "date": r["date"], "kind": pf.KIND_LABELS.get(r["kind"], r["kind"]), "action": r["action"],
            "quantity": r["quantity"], "price": r["price"], "costs": r["costs"], "net_amount": r["net_amount"],
            "currency": r["currency"], "account": f"{r['name'] or r['type'] or 'Account'} · {r['number']}",
            "edit_url": url_for("views.edit_transaction", txn_id=r["id"]),
        }
        for r in rows if account_ids is None or r["account_id"] in account_ids
    ]
    return jsonify(data)


# -- research ------------------------------------------------------------------------------

# Daily bars sent before the first day shown, so every indicator is defined from that day:
# 200 weeks for the 200-week average, which covers the 200-day one too.
RESEARCH_LOOKBACK = timedelta(days=200 * 7 + 30)
SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]{1,20}$")


def traded_symbols():
    return [r["symbol"] for r in get_db().execute(
        "SELECT DISTINCT symbol FROM transactions WHERE user_id = ? AND kind IN (?, ?) AND symbol <> '' ORDER BY symbol",
        (g.user["id"], pf.BUY, pf.SELL))]


@bp.get("/research")
@login_required
def research():
    symbol = request.args.get("symbol", "").strip().upper() or research_default_symbol()
    return render_template("research.html", symbol=symbol, traded=traded_symbols())


def research_default_symbol():
    """The biggest position in the account or portfolio picked on the Dashboard (the nav link
    carries it), else in the saved default view. If nothing is held there, the latest symbol
    bought or sold there; for a new user, the benchmark."""
    account_ids = selected_account_ids()
    if account_ids is None and not request.args.get("all") and (default := default_scope()):
        account_ids = scope_account_ids(*default)
    txns = load_txns(account_ids)
    if txns:
        holdings = pf.snapshot(txns, account_labels(), get_market(), current_app.config["BASE_CURRENCY"])["holdings"]
        if holdings:
            return holdings[0]["symbol"]  # sorted by market value
        trades = [t for t in txns if t.kind in (pf.BUY, pf.SELL) and t.symbol]
        if trades:
            return trades[-1].symbol
    return current_app.config["BENCHMARK_SYMBOL"]


def research_yahoo_symbol(symbol):
    """The Yahoo symbol for what was typed. A symbol the user trades maps the way the dashboard
    maps it (VFV in a CAD account is VFV.TO); anything else counts if Yahoo has a quote for it."""
    market = get_market()
    for r in get_db().execute(
        "SELECT DISTINCT currency FROM transactions WHERE user_id = ? AND symbol = ?", (g.user["id"], symbol)
    ):
        if ysym := market.resolve(symbol, r["currency"]):
            return ysym
    return symbol if market.quotes([symbol]).get(symbol) else None


def research_trades(ysym, currency, splits):
    """The user's buys and sells of a Yahoo symbol, priced like the split-adjusted chart.
    Trades in another currency (the same company on another exchange) are left out."""
    market = get_market()
    matches = {}  # (broker symbol, currency) -> trades this Yahoo symbol?
    out = []
    for r in get_db().execute(
        """SELECT t.date, t.kind, t.symbol, t.quantity, t.price, t.currency, a.number, a.name, a.type
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.user_id = ? AND t.kind IN (?, ?) AND t.price > 0 AND t.currency = ? ORDER BY t.date, t.id""",
        (g.user["id"], pf.BUY, pf.SELL, currency),
    ):
        key = (r["symbol"], r["currency"])
        if key not in matches:  # candidates() rules most symbols out without asking Yahoo
            matches[key] = ysym in MarketData.candidates(*key) and market.resolve(*key) == ysym
        if not matches[key]:
            continue
        d = date.fromisoformat(r["date"])
        ratio = 1.0
        for split_date, split_ratio in splits:
            if d < split_date:
                ratio *= split_ratio
        out.append({"date": r["date"], "kind": r["kind"], "quantity": abs(r["quantity"]) * ratio,
                    "price": r["price"] / ratio, "account": f"{r['name'] or r['type'] or 'Account'} · {r['number']}"})
    return out


def _price(v):
    return float(f"{v:.7g}")  # 7 significant digits keeps long histories small


def research_events(ysym, splits, dividends):
    """Dividends, earnings reports and splits for the Research chart. Yahoo's dividends are
    adjusted for later splits like its prices; "paid" is the amount per share at the time."""
    def paid(d, amount):
        for split_date, ratio in splits:
            if d < split_date:
                amount *= ratio
        return amount

    def ratio_text(ratio):
        f = Fraction(ratio).limit_denominator(1000)
        return f"{f.numerator}:{f.denominator}"

    return {
        "dividends": [{"date": d.isoformat(), "amount": _price(amount), "paid": _price(paid(d, amount))}
                      for d, amount in dividends],
        "earnings": [{"date": d.isoformat(), "estimate": estimate, "actual": actual}
                     for d, estimate, actual in get_market().earnings(ysym)],
        "splits": [{"date": d.isoformat(), "ratio": ratio_text(ratio)} for d, ratio in sorted(splits)],
    }


@bp.get("/api/research/<symbol>")
@login_required
def api_research(symbol):
    """Bars for the Research chart, as [time, open, high, low, close, volume], with the symbol's
    dividends, earnings and splits. ?span=1D or 1W gives intraday bars (plus daily bars for the
    200-day and 200-week averages); otherwise daily bars from RESEARCH_LOOKBACK before ?start
    (an ISO date, or "max" for the whole history)."""
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        return jsonify(error="That doesn't look like a ticker symbol."), 400
    span = request.args.get("span", "")
    start = request.args.get("start", "max")
    if span and span not in INTRADAY_SPANS:
        return jsonify(error="Unknown time span."), 400
    try:
        first = date.today() - timedelta(days=10) if span else None if start == "max" else date.fromisoformat(start)
    except ValueError:
        return jsonify(error="Invalid start date."), 400

    market = get_market()
    ysym = research_yahoo_symbol(symbol)
    if not ysym:
        return jsonify(error=f"Yahoo Finance has no data for {symbol}."), 404
    currency, daily, splits, dividends = market.daily_candles(ysym, first and first - RESEARCH_LOOKBACK)
    quote = market.quotes([ysym]).get(ysym)
    data = {
        "symbol": ysym,
        "name": (quote or {}).get("name") or ysym,
        "currency": currency or (quote or {}).get("currency"),
        "quote": quote,
        "interval": "1d",
        "daily": [[d.isoformat(), *map(_price, ohlc), volume] for d, *ohlc, volume in daily],
        "trades": research_trades(ysym, currency, splits) if currency else [],
        "events": research_events(ysym, splits, dividends),
    }
    if span:
        _, bars = market.intraday_candles(ysym, span)
        data["interval"] = INTRADAY_SPANS[span][1]
        data["bars"] = [[t.isoformat(timespec="minutes"), *map(_price, ohlc), volume] for t, *ohlc, volume in bars]
    return jsonify(data)


@bp.get("/api/research/<symbol>/stats")
@login_required
def api_research_stats(symbol):
    """Key statistics for the Research tab: market cap, beta, P/E, the next earnings date,
    the forward dividend and yield, and analysts' 1-year target; plus the company overview."""
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        return jsonify(error="That doesn't look like a ticker symbol."), 400
    ysym = research_yahoo_symbol(symbol)
    if not ysym:
        return jsonify(error=f"Yahoo Finance has no data for {symbol}."), 404
    stats = get_market().key_stats(ysym)
    if stats is None:
        return jsonify(error=f"Couldn't load the key statistics for {ysym}."), 502
    return jsonify(symbol=ysym, **stats)


RISK_FREE_SYMBOL = "^TNX"  # the 10-year US Treasury yield, in percent
FX_HISTORY_YEARS = 6  # back past the oldest of the four fiscal years Yahoo gives


@bp.get("/api/research/<symbol>/company")
@login_required
def api_research_company(symbol):
    """The Research tab's Company section: financial statements, margins and valuation multiples;
    ownership; and capital returns (share count, dividends, returns on capital against the WACC)."""
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        return jsonify(error="That doesn't look like a ticker symbol."), 400
    ysym = research_yahoo_symbol(symbol)
    if not ysym:
        return jsonify(error=f"Yahoo Finance has no data for {symbol}."), 404
    market = get_market()
    fundamentals = market.fundamentals(ysym, co.FUNDAMENTAL_KEYS)
    facts = market.company_facts(ysym)
    if fundamentals is None and facts is None:
        return jsonify(error=f"Couldn't load the company figures for {ysym}."), 502
    stats = dict(market.key_stats(ysym) or {})
    currency, daily, splits, dividends = market.daily_candles(ysym)
    stats["currency"] = stats.get("currency") or currency
    reporting = (facts or {}).get("financial_currency") or (fundamentals or {}).get("currency")
    fx_now, fx_closes = 1.0, {}
    if reporting and stats["currency"] and reporting != stats["currency"]:
        pair = f"{reporting}{stats['currency']}=X"
        fx_now = (market.quotes([pair]).get(pair) or {}).get("price")
        fx_closes = market.history(pair, date.today() - timedelta(days=FX_HISTORY_YEARS * 366))[1]
    rate = (market.quotes([RISK_FREE_SYMBOL]).get(RISK_FREE_SYMBOL) or {}).get("price")
    # Ten years of income statements from SEC filings; "filings" tells the page why not, if not.
    filed, filings = None, {"status": "off"}
    if fundamentals and any(fundamentals.get(f) for f in ("annual", "quarterly")):
        sec = get_sec()
        if sec:
            filings = sec.income_statement(ysym, (stats.get("profile") or {}).get("name"), splits)
            filed = filings.pop("statement", None)
    # Analysts' estimates: Yahoo's for this year and next, Nasdaq's EPS further ahead for US listings.
    analysts = market.estimates(ysym)
    nasdaq = get_nasdaq() if analysts and (analysts["annual"] or analysts["quarterly"]) else None
    report = co.company_report(
        fundamentals, facts, stats, [(d, c) for d, _, _, _, c, _ in daily], dividends,
        fx_now, list(fx_closes.items()), rate / 100 if rate else None, filed=filed,
        analysts=analysts, forecast=nasdaq.forecast(ysym) if nasdaq else None,
    )
    return jsonify(symbol=ysym, risk_free_symbol=RISK_FREE_SYMBOL, filings=filings, **report)


# -- transactions -------------------------------------------------------------------------


def transactions_section_url(**args):
    """The Transactions section of the Accounts page."""
    return url_for("views.portfolios", _anchor="transactions", **args)


def transaction_listing():
    """The user's transactions, filtered by the account / symbol / kind query args: the latest
    TXN_PREVIEW of them, or with ?show=all every one, PAGE_SIZE per page."""
    account_id = selected_account_id()
    symbol = request.args.get("symbol", "").strip().upper()
    kind = request.args.get("kind", "")
    page = max(int(request.args.get("page", "1")) if request.args.get("page", "1").isdigit() else 1, 1)
    expanded = request.args.get("show") == "all"
    if not expanded:
        page = 1
    limit = PAGE_SIZE if expanded else TXN_PREVIEW

    where, params = ["t.user_id = ?"], [g.user["id"]]
    if account_id:
        where.append("t.account_id = ?")
        params.append(account_id)
    if symbol:
        where.append("t.symbol = ?")
        params.append(symbol)
    if kind in pf.KIND_LABELS:
        where.append("t.kind = ?")
        params.append(kind)
    clause = " AND ".join(where)
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM transactions t WHERE {clause}", params).fetchone()[0]
    rows = db.execute(
        f"""SELECT t.*, a.number, a.name AS account_name, a.type AS account_type
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE {clause} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?""",
        params + [limit, (page - 1) * limit],
    ).fetchall()
    return {"txn_rows": rows, "txn_account_id": account_id, "txn_symbol": symbol, "txn_kind": kind,
            "txn_page": page, "txn_pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1) if expanded else 1,
            "txn_total": total, "txn_expanded": expanded, "txn_preview": TXN_PREVIEW}


@bp.get("/transactions")
@login_required
def transactions():
    """The list now lives on the Accounts page; keep old links and filters working."""
    return redirect(transactions_section_url(**request.args.to_dict()))


def _parse_txn_form(form, accounts):
    """Validate the transaction form. Returns (values, errors)."""
    errors, v = [], {}
    try:
        v["date"] = date.fromisoformat(form.get("date", "")).isoformat()
    except ValueError:
        errors.append("Enter a valid date.")
    kind = form.get("kind", "")
    if kind not in pf.KIND_LABELS:
        errors.append("Choose a transaction type.")
    v["kind"] = kind
    account_ids = {a["id"] for a in accounts}
    account_id = form.get("account_id", "")
    if not account_id.isdigit() or int(account_id) not in account_ids:
        errors.append("Choose an account.")
    else:
        v["account_id"] = int(account_id)
    v["symbol"] = form.get("symbol", "").strip().upper()
    v["currency"] = form.get("currency", "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", v["currency"]):
        errors.append("Currency must be a 3-letter code such as CAD or USD.")

    numbers = {}
    for field in ("quantity", "price", "costs", "net_amount"):
        try:
            numbers[field] = csv_io.parse_number(form.get(field, ""))
        except ValueError:
            errors.append(f"{field.replace('_', ' ').capitalize()} must be a number.")
            numbers[field] = 0.0
    net_blank = not form.get("net_amount", "").strip()

    if kind in (pf.BUY, pf.SELL):
        if not v["symbol"]:
            errors.append("Enter the symbol that was traded.")
        if numbers["quantity"] <= 0:
            errors.append("Quantity must be greater than zero.")
        if numbers["price"] <= 0:
            errors.append("Price must be greater than zero.")
        computed = pf.compute_net_amount(kind, numbers["quantity"], numbers["price"], numbers["costs"])
        net = computed if net_blank else (-abs(numbers["net_amount"]) if kind == pf.BUY else abs(numbers["net_amount"]))
    else:
        if net_blank:
            errors.append("Enter the net amount.")
        net = pf.compute_net_amount(kind, amount=numbers["net_amount"])
    v.update(
        quantity=abs(numbers["quantity"]) if kind in (pf.BUY, pf.SELL) else numbers["quantity"],
        price=numbers["price"],
        costs=abs(numbers["costs"]),
        net_amount=round(net, 6),
    )
    return v, errors


@bp.route("/transactions/new", methods=["GET", "POST"])
@login_required
def new_transaction():
    accounts = user_accounts()
    if not accounts:
        flash("Add an account first, then record transactions in it.", "info")
        return redirect(accounts_section_url())
    if request.method == "POST":
        values, errors = _parse_txn_form(request.form, accounts)
        if not errors:
            db = get_db()
            db.execute(
                """INSERT INTO transactions (user_id, account_id, date, action, kind, symbol, quantity, price,
                   costs, net_amount, currency, activity_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (g.user["id"], values["account_id"], values["date"], pf.KIND_LABELS[values["kind"]], values["kind"],
                 values["symbol"], values["quantity"], values["price"], values["costs"], values["net_amount"],
                 values["currency"], pf.ACTIVITY_TYPES[values["kind"]]),
            )
            db.commit()
            flash("Transaction added.", "success")
            return redirect(transactions_section_url())
        for e in errors:
            flash(e, "error")
        form = request.form
    else:
        form = {"date": date.today().isoformat(), "kind": pf.BUY, "currency": current_app.config["BASE_CURRENCY"],
                "account_id": request.args.get("account", "")}
    return render_template("transaction_form.html", form=form, accounts=accounts, txn=None)


@bp.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
@login_required
def edit_transaction(txn_id):
    db = get_db()
    txn = db.execute("SELECT * FROM transactions WHERE id = ? AND user_id = ?", (txn_id, g.user["id"])).fetchone()
    if txn is None:
        abort(404)
    accounts = user_accounts()
    if request.method == "POST":
        values, errors = _parse_txn_form(request.form, accounts)
        if not errors:
            changed_kind = values["kind"] != txn["kind"]
            db.execute(
                """UPDATE transactions SET account_id = ?, date = ?, action = ?, kind = ?, symbol = ?, quantity = ?,
                   price = ?, costs = ?, net_amount = ?, currency = ?, activity_type = ?
                   WHERE id = ? AND user_id = ?""",
                (values["account_id"], values["date"],
                 pf.KIND_LABELS[values["kind"]] if changed_kind else txn["action"], values["kind"], values["symbol"],
                 values["quantity"], values["price"], values["costs"], values["net_amount"], values["currency"],
                 pf.ACTIVITY_TYPES[values["kind"]] if changed_kind else txn["activity_type"], txn_id, g.user["id"]),
            )
            db.commit()
            flash("Transaction updated.", "success")
            return redirect(transactions_section_url())
        for e in errors:
            flash(e, "error")
        form = request.form
    else:
        form = {k: txn[k] for k in txn.keys()}
        form["quantity"] = abs(txn["quantity"]) if txn["kind"] in (pf.BUY, pf.SELL) else txn["quantity"]
    return render_template("transaction_form.html", form=form, accounts=accounts, txn=txn)


@bp.post("/transactions/<int:txn_id>/delete")
@login_required
def delete_transaction(txn_id):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (txn_id, g.user["id"]))
    db.commit()
    flash("Transaction deleted.", "success")
    back = request.referrer
    if not back or "/edit" in back:
        return redirect(transactions_section_url())
    if urlsplit(back).path == url_for("views.portfolios"):
        back = back.split("#")[0] + "#transactions"  # browsers drop the #section from the referrer
    return redirect(back)


# -- accounts ---------------------------------------------------------------------------


def accounts_section_url():
    """The Accounts section of the Accounts page."""
    return url_for("views.portfolios", _anchor="accounts")


def account_overview():
    """Accounts with their transaction counts, and cash balances by account."""
    db = get_db()
    rows = db.execute(
        """SELECT a.*, COUNT(t.id) AS txn_count FROM accounts a
           LEFT JOIN transactions t ON t.account_id = a.id
           WHERE a.user_id = ? GROUP BY a.id ORDER BY a.type, a.number""",
        (g.user["id"],),
    ).fetchall()
    balances = db.execute(
        """SELECT account_id, currency, SUM(net_amount) AS amount FROM transactions
           WHERE user_id = ? GROUP BY account_id, currency HAVING ABS(SUM(net_amount)) >= 0.005
           ORDER BY currency""",
        (g.user["id"],),
    ).fetchall()
    cash = {}
    for b in balances:
        cash.setdefault(b["account_id"], []).append(b)
    return rows, cash


@bp.route("/accounts", methods=["GET", "POST"])
@login_required
def accounts():
    """Adds an account. The account list lives on the Accounts page."""
    if request.method != "POST":
        return redirect(accounts_section_url())
    db = get_db()
    number = request.form.get("number", "").strip()
    if not number:
        flash("Enter the account number.", "error")
    elif db.execute("SELECT 1 FROM accounts WHERE user_id = ? AND number = ?", (g.user["id"], number)).fetchone():
        flash(f"You already have an account numbered {number}.", "error")
    else:
        db.execute(
            "INSERT INTO accounts (user_id, number, type, name) VALUES (?, ?, ?, ?)",
            (g.user["id"], number, request.form.get("type", "").strip(), request.form.get("name", "").strip()),
        )
        db.commit()
        flash("Account added.", "success")
        return redirect(accounts_section_url())
    return redirect(url_for("views.portfolios"))  # top of the page, where the error message shows


@bp.route("/accounts/<int:account_id>/edit", methods=["GET", "POST"])
@login_required
def edit_account(account_id):
    account = get_account_or_404(account_id)
    if request.method == "POST":
        number = request.form.get("number", "").strip()
        db = get_db()
        clash = db.execute(
            "SELECT 1 FROM accounts WHERE user_id = ? AND number = ? AND id != ?", (g.user["id"], number, account_id)
        ).fetchone()
        if not number:
            flash("Enter the account number.", "error")
        elif clash:
            flash(f"You already have an account numbered {number}.", "error")
        else:
            db.execute(
                "UPDATE accounts SET number = ?, type = ?, name = ? WHERE id = ? AND user_id = ?",
                (number, request.form.get("type", "").strip(), request.form.get("name", "").strip(),
                 account_id, g.user["id"]),
            )
            db.commit()
            flash("Account updated.", "success")
            return redirect(accounts_section_url())
    return render_template("account_form.html", account=account)


@bp.post("/accounts/<int:account_id>/delete")
@login_required
def delete_account(account_id):
    account = get_account_or_404(account_id)
    db = get_db()
    db.execute("DELETE FROM accounts WHERE id = ? AND user_id = ?", (account_id, g.user["id"]))
    db.commit()
    flash(f"Deleted {account_label(account)} and its transactions.", "success")
    return redirect(accounts_section_url())


# -- settings -------------------------------------------------------------------------------


@bp.post("/settings/theme")
@login_required
def set_theme():
    theme = request.form.get("theme", "")
    if theme not in THEMES:
        abort(400)
    db = get_db()
    db.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, g.user["id"]))
    db.commit()
    back = request.form.get("next", "")
    if not back.startswith("/") or back.startswith(("//", "/\\")):
        back = url_for("views.dashboard")
    return redirect(back)


@bp.post("/settings/default-view")
@login_required
def set_default_view():
    scope = request.form.get("scope", "")
    kind, _, raw_id = scope.partition(":")
    if scope:
        if kind not in ("account", "portfolio") or not raw_id.isdigit():
            abort(400)
        (get_account_or_404 if kind == "account" else get_portfolio_or_404)(int(raw_id))
    db = get_db()
    db.execute("UPDATE users SET default_scope = ? WHERE id = ?", (scope, g.user["id"]))
    db.commit()
    flash("The dashboard will now open with this view.", "success")
    back = request.form.get("next", "")
    if not back.startswith("/") or back.startswith(("//", "/\\")):
        back = url_for("views.dashboard")
    return redirect(back)


# -- portfolios -------------------------------------------------------------------------------


def get_portfolio_or_404(portfolio_id):
    row = get_db().execute(
        "SELECT * FROM portfolios WHERE id = ? AND user_id = ?", (portfolio_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def _parse_portfolio_form(form, accounts, portfolio_id=None):
    """Validated (name, account_ids), or None after flashing the problem."""
    name = form.get("name", "").strip()
    owned = {a["id"] for a in accounts}
    account_ids = sorted({int(v) for v in form.getlist("account_ids") if v.isdigit() and int(v) in owned})
    clash = get_db().execute(
        "SELECT 1 FROM portfolios WHERE user_id = ? AND name = ? AND id != ?", (g.user["id"], name, portfolio_id or 0)
    ).fetchone()
    if not name:
        flash("Enter a portfolio name.", "error")
    elif clash:
        flash(f"You already have a portfolio named {name}.", "error")
    else:
        return name, account_ids
    return None


def _set_portfolio_accounts(db, portfolio_id, account_ids):
    db.execute("DELETE FROM portfolio_accounts WHERE portfolio_id = ?", (portfolio_id,))
    db.executemany(
        "INSERT INTO portfolio_accounts (portfolio_id, account_id) VALUES (?, ?)",
        [(portfolio_id, a) for a in account_ids],
    )


@bp.route("/portfolios", methods=["GET", "POST"])
@login_required
def portfolios():
    db = get_db()
    accounts = user_accounts()
    if request.method == "POST":
        parsed = _parse_portfolio_form(request.form, accounts)
        if parsed:
            name, account_ids = parsed
            cur = db.execute("INSERT INTO portfolios (user_id, name) VALUES (?, ?)", (g.user["id"], name))
            _set_portfolio_accounts(db, cur.lastrowid, account_ids)
            db.commit()
            flash(f"Portfolio {name} created.", "success")
            return redirect(url_for("views.portfolios"))
    return render_portfolio_manager()


def render_portfolio_manager(**extra):
    """The Accounts page: portfolios, import / export, accounts and transactions."""
    db = get_db()
    accounts = user_accounts()
    rows = db.execute("SELECT * FROM portfolios WHERE user_id = ? ORDER BY name", (g.user["id"],)).fetchall()
    labels = {a["id"]: account_label(a) for a in accounts}
    members = {}
    for m in db.execute(
        """SELECT pa.portfolio_id, pa.account_id FROM portfolio_accounts pa
           JOIN portfolios p ON p.id = pa.portfolio_id WHERE p.user_id = ?""",
        (g.user["id"],),
    ):
        members.setdefault(m["portfolio_id"], []).append(labels[m["account_id"]])
    account_rows, cash = account_overview()
    return render_template("portfolios.html", rows=rows, members=members, accounts=accounts,
                           selected=set(), label=account_label, account_rows=account_rows, cash=cash,
                           import_columns=csv_io.COLUMNS, **transaction_listing(), **extra)


@bp.route("/portfolios/<int:portfolio_id>/edit", methods=["GET", "POST"])
@login_required
def edit_portfolio(portfolio_id):
    portfolio = get_portfolio_or_404(portfolio_id)
    accounts = user_accounts()
    db = get_db()
    if request.method == "POST":
        parsed = _parse_portfolio_form(request.form, accounts, portfolio_id)
        if parsed:
            name, account_ids = parsed
            db.execute("UPDATE portfolios SET name = ? WHERE id = ? AND user_id = ?", (name, portfolio_id, g.user["id"]))
            _set_portfolio_accounts(db, portfolio_id, account_ids)
            db.commit()
            flash("Portfolio updated.", "success")
            return redirect(url_for("views.portfolios"))
    selected = {r["account_id"] for r in db.execute(
        "SELECT account_id FROM portfolio_accounts WHERE portfolio_id = ?", (portfolio_id,)
    )}
    return render_template("portfolio_form.html", portfolio=portfolio, accounts=accounts, selected=selected,
                           label=account_label)


@bp.post("/portfolios/<int:portfolio_id>/delete")
@login_required
def delete_portfolio(portfolio_id):
    portfolio = get_portfolio_or_404(portfolio_id)
    db = get_db()
    db.execute("DELETE FROM portfolios WHERE id = ? AND user_id = ?", (portfolio_id, g.user["id"]))
    db.commit()
    flash(f"Deleted portfolio {portfolio['name']}. Its accounts and transactions are unchanged.", "success")
    return redirect(url_for("views.portfolios"))


# -- import / export -------------------------------------------------------------------------


@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_export():
    """Imports a CSV and shows the results on the Accounts page, where the
    Import / Export section now lives."""
    if request.method != "POST":
        return redirect(url_for("views.portfolios", _anchor="import"))
    result = None
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a CSV file to import.", "error")
    else:
        rows, errors = csv_io.parse(csv_io.decode(upload.read()))
        imported = duplicates = created = 0
        if rows:
            imported, duplicates, created = csv_io.import_rows(get_db(), g.user["id"], rows)
        result = {"filename": upload.filename, "imported": imported, "duplicates": duplicates,
                  "accounts_created": created, "errors": errors}
        if imported:
            flash(f"Imported {imported} transaction{'s' if imported != 1 else ''}.", "success")
    return render_portfolio_manager(import_result=result)


@bp.get("/export.csv")
@login_required
def export_csv():
    account_id = selected_account_id()
    sql = """SELECT t.*, a.number AS account_number, a.type AS account_type
             FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE t.user_id = ?"""
    params = [g.user["id"]]
    if account_id:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    rows = get_db().execute(sql + " ORDER BY t.date, t.id", params).fetchall()
    filename = f"portfolio-transactions-{date.today().isoformat()}.csv"
    return Response(csv_io.export(rows), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.get("/import/template.csv")
@login_required
def import_template():
    return Response(csv_io.export([]), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transactions-template.csv"})


# -- my assets: net worth ----------------------------------------------------------------------


def assets_url(anchor="net-worth"):
    """A section of the My Assets page."""
    return url_for("views.assets", _anchor=anchor)


def user_assets():
    return get_db().execute(
        "SELECT * FROM assets WHERE user_id = ? ORDER BY name COLLATE NOCASE, id", (g.user["id"],)
    ).fetchall()


def has_assets():
    """Whether the user has any assets or mortgages, so the dashboard shows their net worth."""
    return get_db().execute(
        """SELECT EXISTS (SELECT 1 FROM assets WHERE user_id = :u)
                  OR EXISTS (SELECT 1 FROM mortgages WHERE user_id = :u)""", {"u": g.user["id"]}
    ).fetchone()[0] == 1


def get_asset_or_404(asset_id):
    row = get_db().execute("SELECT * FROM assets WHERE id = ? AND user_id = ?", (asset_id, g.user["id"])).fetchone()
    if row is None:
        abort(404)
    return row


def portfolio_members():
    """{portfolio id: [account ids]} for the user's portfolios."""
    out = {}
    for r in get_db().execute(
        """SELECT pa.portfolio_id, pa.account_id FROM portfolio_accounts pa
           JOIN portfolios p ON p.id = pa.portfolio_id WHERE p.user_id = ? ORDER BY pa.account_id""",
        (g.user["id"],),
    ):
        out.setdefault(r["portfolio_id"], []).append(r["account_id"])
    return out


def asset_members():
    """{asset id: [account ids]} for assets made of chosen accounts."""
    out = {}
    for r in get_db().execute(
        """SELECT aa.asset_id, aa.account_id FROM asset_accounts aa
           JOIN assets s ON s.id = aa.asset_id WHERE s.user_id = ? ORDER BY aa.account_id""",
        (g.user["id"],),
    ):
        out.setdefault(r["asset_id"], []).append(r["account_id"])
    return out


def asset_account_ids(asset, portfolios, members):
    """The accounts whose value makes up `asset`, or None for a value entered by hand."""
    if asset["kind"] == "manual":
        return None
    if asset["kind"] == "portfolio":
        return portfolios.get(asset["portfolio_id"], [])
    return members.get(asset["id"], [])


def _parse_asset_form(form, accounts, portfolios):
    """Validate the asset form. Returns (values, errors)."""
    errors = []
    v = {"name": form.get("name", "").strip(), "kind": form.get("kind", ""), "portfolio_id": None,
         "account_ids": [], "value": 0.0}
    if v["kind"] == "portfolio":
        names = {p["id"]: p["name"] for p in portfolios}
        raw = form.get("portfolio_id", "")
        if raw.isdigit() and int(raw) in names:
            v["portfolio_id"] = int(raw)
            v["name"] = v["name"] or names[v["portfolio_id"]]
        else:
            errors.append("Choose a portfolio.")
    elif v["kind"] == "accounts":
        owned = {a["id"] for a in accounts}
        v["account_ids"] = sorted({int(x) for x in form.getlist("account_ids") if x.isdigit() and int(x) in owned})
        if not v["account_ids"]:
            errors.append("Choose at least one account.")
    elif v["kind"] == "manual":
        raw = form.get("value", "").strip()
        try:
            v["value"] = csv_io.parse_number(raw)
            if not raw:
                errors.append("Enter what the asset is worth.")
            elif v["value"] < 0:
                errors.append("The value can't be negative. Enter debts as mortgages.")
        except ValueError:
            errors.append("The value must be a number.")
    else:
        errors.append("Choose what the asset is: a portfolio, accounts, or something else.")
    if not v["name"]:
        errors.insert(0, "Enter a name for the asset.")
    return v, errors


def _save_asset(asset_id, v):
    db = get_db()
    if asset_id is None:
        asset_id = db.execute(
            "INSERT INTO assets (user_id, name, kind, portfolio_id, value) VALUES (?, ?, ?, ?, ?)",
            (g.user["id"], v["name"], v["kind"], v["portfolio_id"], v["value"]),
        ).lastrowid
    else:
        db.execute(
            """UPDATE assets SET name = ?, kind = ?, portfolio_id = ?, value = ?, updated_at = datetime('now')
               WHERE id = ? AND user_id = ?""",
            (v["name"], v["kind"], v["portfolio_id"], v["value"], asset_id, g.user["id"]),
        )
    db.execute("DELETE FROM asset_accounts WHERE asset_id = ?", (asset_id,))
    db.executemany("INSERT INTO asset_accounts (asset_id, account_id) VALUES (?, ?)",
                   [(asset_id, a) for a in v["account_ids"]])
    db.commit()


def render_my_assets(**extra):
    """The My Assets page: net worth and mortgages."""
    accounts = user_accounts()
    mortgages = mortgage_projections(user_mortgages())
    # One payback chart for all the mortgages, which can show any of them, side by side or combined.
    mortgage_charts = [{"id": m["id"], "name": m["name"], **p["chart"]} for m, p, _ in mortgages if p]
    mortgage_links = [("mortgage-chart", "Payback chart")] if mortgage_charts else []
    mortgage_links += [(f"mortgage-{m['id']}", m["name"]) for m, _, _ in mortgages]
    return render_template(
        "assets.html", assets=user_assets(), accounts=accounts, portfolios=user_portfolios(),
        labels={a["id"]: account_label(a) for a in accounts}, members=asset_members(), label=account_label,
        mortgages=mortgages, mortgage_totals=mortgage_totals(mortgages), mortgage_charts=mortgage_charts,
        mortgage_links=mortgage_links, today=date.today().isoformat(),
        frequencies=mg.FREQUENCIES, compoundings=mg.COMPOUNDING, duration=mg.duration,
        **{"asset_form": None, "asset_errors": [], "mortgage_form": new_mortgage_form(), "mortgage_errors": [], **extra},
    )


@bp.route("/assets", methods=["GET", "POST"])
@login_required
def assets():
    """The My Assets page; a POST adds an asset."""
    if request.method == "POST":
        values, errors = _parse_asset_form(request.form, user_accounts(), user_portfolios())
        if errors:
            return render_my_assets(asset_form=request.form, asset_errors=errors)
        _save_asset(None, values)
        flash(f"Added {values['name']} to your assets.", "success")
        return redirect(assets_url())
    return render_my_assets()


@bp.route("/assets/<int:asset_id>/edit", methods=["GET", "POST"])
@login_required
def edit_asset(asset_id):
    asset = get_asset_or_404(asset_id)
    accounts, portfolios = user_accounts(), user_portfolios()
    if request.method == "POST":
        values, errors = _parse_asset_form(request.form, accounts, portfolios)
        if not errors:
            _save_asset(asset_id, values)
            flash(f"Updated {values['name']}.", "success")
            return redirect(assets_url())
        for e in errors:
            flash(e, "error")
        form = request.form
    else:
        form = None
    return render_template("asset_form.html", asset=asset, accounts=accounts, portfolios=portfolios,
                           asset_form=form, label=account_label, members=asset_members())


@bp.post("/assets/<int:asset_id>/delete")
@login_required
def delete_asset(asset_id):
    asset = get_asset_or_404(asset_id)
    db = get_db()
    db.execute("DELETE FROM assets WHERE id = ? AND user_id = ?", (asset_id, g.user["id"]))
    db.commit()
    flash(f"Removed {asset['name']} from your assets. Its accounts and transactions are unchanged.", "success")
    return redirect(assets_url())


def _and_list(names):
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


@bp.get("/api/assets")
@login_required
def api_assets():
    """Each asset's value (live for portfolios and accounts), mortgage balances and net worth."""
    labels = account_labels()
    portfolios, members = portfolio_members(), asset_members()
    portfolio_names = {p["id"]: p["name"] for p in user_portfolios()}
    market, base = get_market(), current_app.config["BASE_CURRENCY"]
    values, totals, counted, warnings = [], {}, {}, []
    for a in user_assets():
        ids = asset_account_ids(a, portfolios, members)
        if ids is None:
            values.append({"id": a["id"], "name": a["name"], "kind": a["kind"], "value": a["value"]})
            continue
        if a["kind"] == "portfolio" and a["portfolio_id"] is None:
            warnings.append(f"The portfolio behind {a['name']} was deleted, so it counts as $0. Edit it to choose another.")
        elif not ids:
            warnings.append(f"{a['name']} has no accounts, so it counts as $0.")
        key = tuple(ids)
        if key not in totals:  # the same accounts are only valued once
            totals[key] = pf.snapshot(load_txns(ids), labels, market, base)["totals"]["total_value"] if ids else 0.0
        for account_id in ids:
            counted.setdefault(account_id, []).append(a["name"])
        values.append({"id": a["id"], "name": a["name"], "kind": a["kind"], "value": totals[key],
                       "portfolio": portfolio_names.get(a["portfolio_id"])})
    for account_id, names in counted.items():
        if len(names) > 1:
            warnings.append(f"{labels[account_id]} is part of {_and_list(names)}, so your net worth counts it "
                            f"{len(names)} times.")
    debts = [{"id": m["id"], "name": m["name"], "balance": p["balance"]}
             for m, p, _ in mortgage_projections(user_mortgages()) if p]
    total_assets = sum(v["value"] for v in values)
    total_debts = sum(d["balance"] for d in debts)
    return jsonify(assets=values, mortgages=debts, total_assets=total_assets, total_liabilities=total_debts,
                   net_worth=total_assets - total_debts, warnings=visible_warnings(warnings),
                   as_of=datetime.now().isoformat(timespec="seconds"))


# -- my assets: mortgages ------------------------------------------------------------------------


def user_mortgages():
    return get_db().execute(
        "SELECT * FROM mortgages WHERE user_id = ? ORDER BY start_date, id", (g.user["id"],)
    ).fetchall()


def get_mortgage_or_404(mortgage_id):
    row = get_db().execute(
        "SELECT * FROM mortgages WHERE id = ? AND user_id = ?", (mortgage_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def mortgage_projections(rows, today=None):
    """[(mortgage, projection, None)], or (mortgage, None, error) for one that can't be projected."""
    out = []
    for m in rows:
        try:
            out.append((m, mg.project(m, today), None))
        except mg.NeverPaidOff as exc:
            out.append((m, None, str(exc)))
    return out


def mortgage_totals(projections):
    """What all the mortgages add up to, for the summary above them."""
    ok = [p for _, p, _ in projections if p]
    payoffs = [p["payoff_date"] for p in ok if p["payoff_date"]]
    return {"balance": sum(p["balance"] for p in ok),
            "monthly_cost": sum(p["monthly_cost"] for p in ok if p["balance"] > 0.005),
            "interest_left": sum(p["interest_left"] for p in ok), "payoff_date": max(payoffs) if payoffs else None}


def _num(value, places=2):
    """A number for a form field: blank for None, without trailing zeros."""
    if value is None:
        return ""
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def new_mortgage_form():
    return {"start_date": date.today().isoformat(), "compounding": "semi-annual", "frequency": "monthly",
            "amortization_years": "25", "amortization_months": "0"}


def mortgage_form_values(m):
    """The mortgage form, filled in from a saved mortgage."""
    return {"name": m["name"], "lender": m["lender"], "principal": _num(m["principal"]), "start_date": m["start_date"],
            "rate": _num(m["rate"], 4), "compounding": m["compounding"], "frequency": m["frequency"],
            "amortization_years": str(m["amortization_months"] // 12),
            "amortization_months": str(m["amortization_months"] % 12),
            "payment": _num(m["payment"]), "extra_payment": _num(m["extra_payment"] or None),
            "term_end": m["term_end"] or "", "balance": _num(m["balance"]), "balance_date": m["balance_date"] or ""}


def _parse_mortgage_form(form, today=None):
    """Validate the mortgage form. Returns (values, errors)."""
    today = today or date.today()
    errors = []

    def number(field, label, required=False):
        raw = form.get(field, "").strip()
        if not raw:
            if required:
                errors.append(f"Enter the {label}.")
            return None
        try:
            return csv_io.parse_number(raw)
        except ValueError:
            errors.append(f"The {label} must be a number.")
            return None

    def day(field, label, required=False):
        raw = form.get(field, "").strip()
        if not raw:
            if required:
                errors.append(f"Enter the {label}.")
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            errors.append(f"Enter a valid {label}.")
            return None

    name = form.get("name", "").strip()
    if not name:
        errors.append("Enter a name for the mortgage, such as Home.")
    principal = number("principal", "original amount", required=True)
    if principal is not None and principal <= 0:
        errors.append("The original amount must be greater than zero.")
    start = day("start_date", "start date", required=True)
    rate = number("rate", "interest rate", required=True)
    if rate is not None and not 0 <= rate <= 30:
        errors.append("The interest rate must be between 0% and 30%.")
    compounding = form.get("compounding", "")
    if compounding not in mg.COMPOUNDING:
        errors.append("Choose how often interest is compounded.")
    frequency = form.get("frequency", "")
    if frequency not in mg.FREQUENCIES:
        errors.append("Choose how often you make payments.")
    years = number("amortization_years", "amortization years") or 0
    months = number("amortization_months", "amortization months") or 0
    amortization = round(years * 12 + months)
    if not 1 <= amortization <= 600:
        errors.append("The amortization must be between 1 month and 50 years.")
    payment = number("payment", "payment amount")
    if payment is not None and payment <= 0:
        errors.append("The payment must be greater than zero, or left blank to calculate it.")
    extra = number("extra_payment", "extra payment") or 0.0
    if extra < 0:
        errors.append("The extra payment can't be negative.")
    term_end = day("term_end", "term end date")
    if start and term_end and term_end <= start:
        errors.append("The term must end after the mortgage starts.")
    balance = number("balance", "balance from your statement")
    balance_date = day("balance_date", "statement date")
    if (balance is None) != (balance_date is None):
        errors.append("Enter both the balance from your statement and its date, or leave both blank.")
    elif balance is not None:
        if balance < 0:
            errors.append("The balance from your statement can't be negative.")
        if start and balance_date < start:
            errors.append("The statement date can't be before the mortgage started.")
        if balance_date > today:
            errors.append("The statement date can't be in the future.")

    values = {"name": name, "lender": form.get("lender", "").strip(), "principal": principal,
              "start_date": start.isoformat() if start else None, "rate": rate, "compounding": compounding,
              "amortization_months": amortization, "frequency": frequency, "payment": payment,
              "extra_payment": extra, "term_end": term_end.isoformat() if term_end else None,
              "balance": balance, "balance_date": balance_date.isoformat() if balance_date else None}
    if not errors:
        try:
            mg.project(values, today)
        except mg.NeverPaidOff as exc:
            errors.append(str(exc))
    return values, errors


MORTGAGE_COLUMNS = ("name", "lender", "principal", "start_date", "rate", "compounding", "amortization_months",
                    "frequency", "payment", "extra_payment", "term_end", "balance", "balance_date")


@bp.post("/mortgages")
@login_required
def new_mortgage():
    values, errors = _parse_mortgage_form(request.form)
    if errors:
        return render_my_assets(mortgage_form=request.form, mortgage_errors=errors)
    db = get_db()
    cur = db.execute(
        f"INSERT INTO mortgages (user_id, {', '.join(MORTGAGE_COLUMNS)}) VALUES (?{', ?' * len(MORTGAGE_COLUMNS)})",
        (g.user["id"], *(values[c] for c in MORTGAGE_COLUMNS)),
    )
    db.commit()
    flash(f"Added the {values['name']} mortgage.", "success")
    return redirect(assets_url(f"mortgage-{cur.lastrowid}"))


@bp.route("/mortgages/<int:mortgage_id>/edit", methods=["GET", "POST"])
@login_required
def edit_mortgage(mortgage_id):
    mortgage = get_mortgage_or_404(mortgage_id)
    if request.method == "POST":
        values, errors = _parse_mortgage_form(request.form)
        if not errors:
            db = get_db()
            db.execute(
                f"UPDATE mortgages SET {', '.join(f'{c} = ?' for c in MORTGAGE_COLUMNS)} WHERE id = ? AND user_id = ?",
                (*(values[c] for c in MORTGAGE_COLUMNS), mortgage_id, g.user["id"]),
            )
            db.commit()
            flash(f"Updated the {values['name']} mortgage.", "success")
            return redirect(assets_url(f"mortgage-{mortgage_id}"))
        for e in errors:
            flash(e, "error")
        form = request.form
    else:
        form = mortgage_form_values(mortgage)
    return render_template("mortgage_form.html", mortgage=mortgage, mortgage_form=form,
                           frequencies=mg.FREQUENCIES, compoundings=mg.COMPOUNDING)


@bp.post("/mortgages/<int:mortgage_id>/delete")
@login_required
def delete_mortgage(mortgage_id):
    mortgage = get_mortgage_or_404(mortgage_id)
    db = get_db()
    db.execute("DELETE FROM mortgages WHERE id = ? AND user_id = ?", (mortgage_id, g.user["id"]))
    db.commit()
    flash(f"Deleted the {mortgage['name']} mortgage.", "success")
    return redirect(assets_url("mortgages"))


@bp.get("/api/mortgages/continued")
@login_required
def api_mortgages_continued():
    """The payback of the chosen mortgages (?ids=1,2) when each one's payments carry on to the
    others once it is paid off."""
    ids = {int(i) for i in request.args.get("ids", "").split(",") if i.strip().isdigit()}
    chosen = [m for m, p, _ in mortgage_projections(user_mortgages()) if p and m["id"] in ids]
    if len(chosen) < 2:
        return jsonify(error="Choose at least two mortgages."), 400
    return jsonify(mg.project_continued(chosen))


# -- budget ---------------------------------------------------------------------------------------


def user_budget_lines():
    return get_db().execute(
        """SELECT * FROM budget_lines WHERE user_id = ?
           ORDER BY CASE kind WHEN 'revenue' THEN 0 ELSE 1 END, id""", (g.user["id"],)
    ).fetchall()


def get_budget_line_or_404(line_id):
    row = get_db().execute(
        "SELECT * FROM budget_lines WHERE id = ? AND user_id = ?", (line_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def budget_plan(today=None):
    """The next year of the budget by pay day, or None until the user has set a pay day."""
    if not g.user["pay_day"]:
        return None
    entries = {(r["line_id"], r["pay_day"]): r["amount"] for r in get_db().execute(
        "SELECT e.* FROM budget_entries e JOIN budget_lines l ON l.id = e.line_id WHERE l.user_id = ?",
        (g.user["id"],),
    )}
    return bg.plan(user_budget_lines(), entries, date.fromisoformat(g.user["pay_day"]), today or date.today())


def budget_line_form_values(line):
    """The budget line form, filled in from a saved line."""
    return {"kind": line["kind"], "name": line["name"], "group_name": line["group_name"], "frequency": line["frequency"],
            "amount": _num(line["amount"]), "start_date": line["start_date"] or "", "end_date": line["end_date"] or ""}


def budget_groups():
    """The user's expense groups, in alphabetical order."""
    return [r["group_name"] for r in get_db().execute(
        """SELECT DISTINCT group_name FROM budget_lines WHERE user_id = ? AND kind = 'expense' AND group_name <> ''
           ORDER BY group_name COLLATE NOCASE""", (g.user["id"],))]


def group_choices():
    """Groups to suggest: the user's, then common ones they haven't used."""
    used = budget_groups()
    return used + [s for s in bg.SUGGESTED_GROUPS if s.casefold() not in {u.casefold() for u in used}]


def _parse_budget_line_form(form):
    """Validate the budget line form. Returns (values, errors)."""
    errors = []
    kind = form.get("kind", "")
    if kind not in bg.KINDS:
        errors.append("Choose whether this is a revenue or an expense.")
    name = form.get("name", "").strip()
    if not name:
        errors.append("Enter a name, such as Salary or Rent.")
    # Only expenses have groups. A group typed in another case joins the existing one ("housing" -> "Housing").
    group = " ".join(form.get("group_name", "").split())[:40] if kind == "expense" else ""
    group = next((u for u in budget_groups() if u.casefold() == group.casefold()), group)
    frequency = form.get("frequency", "")
    if frequency not in bg.FREQUENCIES:
        errors.append("Choose how often it comes.")
    amount = None
    raw = form.get("amount", "").strip()
    if frequency != "varies":
        if not raw:
            errors.append("Enter the amount each time, or choose Varies to enter it for each pay day.")
        else:
            try:
                amount = csv_io.parse_number(raw)
            except ValueError:
                errors.append("The amount must be a number.")
            else:
                if amount <= 0:
                    errors.append("The amount must be greater than zero.")

    def day(field, label):
        raw = form.get(field, "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            errors.append(f"Enter a valid {label}.")
            return None

    start, end = day("start_date", "first date"), day("end_date", "end date")
    if frequency == "varies":
        start = end = None
    elif frequency == "once":
        end = None
    if frequency in bg.NEEDS_DATE and not form.get("start_date", "").strip():
        errors.append("Enter the date it is due." if frequency == "once" else "Enter the first date it is due.")
    if start and end and end < start:
        errors.append("The end date can't be before the first date.")
    values = {"kind": kind, "name": name, "group_name": group, "frequency": frequency, "amount": amount,
              "start_date": start.isoformat() if start else None, "end_date": end.isoformat() if end else None}
    return values, errors


BUDGET_COLUMNS = ("kind", "name", "group_name", "frequency", "amount", "start_date", "end_date")


def render_budget(**extra):
    kind = request.args.get("kind")
    return render_template(
        "budget.html", plan=budget_plan(), kinds=bg.KINDS, frequencies=bg.FREQUENCIES, groups=group_choices(),
        **{"line_form": {"kind": kind if kind in bg.KINDS else "expense", "frequency": "monthly"}, "line_errors": [],
           **extra},
    )


@bp.get("/budget")
@login_required
def budget():
    return render_budget()


@bp.post("/budget/pay-day")
@login_required
def set_pay_day():
    try:
        pay_day = date.fromisoformat(request.form.get("pay_day", "").strip())
    except ValueError:
        flash("Enter the date of one of your pay days.", "error")
        return redirect(url_for("views.budget"))
    db = get_db()
    db.execute("UPDATE users SET pay_day = ? WHERE id = ?", (pay_day.isoformat(), g.user["id"]))
    db.commit()
    flash("Saved your pay day. The budget is planned every two weeks from it.", "success")
    return redirect(url_for("views.budget"))


@bp.post("/budget/lines")
@login_required
def new_budget_line():
    values, errors = _parse_budget_line_form(request.form)
    if errors:
        return render_budget(line_form=request.form, line_errors=errors)
    db = get_db()
    db.execute(
        f"INSERT INTO budget_lines (user_id, {', '.join(BUDGET_COLUMNS)}) VALUES (?{', ?' * len(BUDGET_COLUMNS)})",
        (g.user["id"], *(values[c] for c in BUDGET_COLUMNS)),
    )
    db.commit()
    flash(f"Added {values['name']} to your {'revenues' if values['kind'] == 'revenue' else 'expenses'}.", "success")
    return redirect(url_for("views.budget", _anchor="budget-plan"))


@bp.route("/budget/lines/<int:line_id>/edit", methods=["GET", "POST"])
@login_required
def edit_budget_line(line_id):
    line = get_budget_line_or_404(line_id)
    if request.method == "POST":
        values, errors = _parse_budget_line_form(request.form)
        if not errors:
            db = get_db()
            db.execute(
                f"UPDATE budget_lines SET {', '.join(f'{c} = ?' for c in BUDGET_COLUMNS)} WHERE id = ? AND user_id = ?",
                (*(values[c] for c in BUDGET_COLUMNS), line_id, g.user["id"]),
            )
            db.commit()
            flash(f"Updated {values['name']}.", "success")
            return redirect(url_for("views.budget", _anchor="budget-plan"))
        for e in errors:
            flash(e, "error")
        form = request.form
    else:
        form = budget_line_form_values(line)
    return render_template("budget_line_form.html", line=line, line_form=form, kinds=bg.KINDS,
                           frequencies=bg.FREQUENCIES, groups=group_choices())


@bp.post("/budget/lines/<int:line_id>/delete")
@login_required
def delete_budget_line(line_id):
    line = get_budget_line_or_404(line_id)
    db = get_db()
    db.execute("DELETE FROM budget_lines WHERE id = ? AND user_id = ?", (line_id, g.user["id"]))
    db.commit()
    flash(f"Deleted {line['name']} from your budget.", "success")
    return redirect(url_for("views.budget", _anchor="budget-plan"))


@bp.get("/api/budget")
@login_required
def api_budget():
    plan = budget_plan()
    if plan is None:
        return jsonify(error="Set your pay day first."), 400
    return jsonify(plan)


@bp.post("/api/budget/entries")
@login_required
def api_budget_entry():
    """Enter one pay period's amount for a line ({"line_id", "pay_day", "amount"}). A blank
    amount removes it, so a recurring line goes back to its fixed amount. Returns the budget."""
    data = request.get_json(silent=True) or {}
    if not g.user["pay_day"]:
        return jsonify(error="Set your pay day first."), 400
    line = get_db().execute(
        "SELECT id FROM budget_lines WHERE id = ? AND user_id = ?", (data.get("line_id"), g.user["id"])
    ).fetchone()
    if line is None:
        return jsonify(error="That budget line doesn't exist."), 404
    try:
        pay_day = date.fromisoformat(str(data.get("pay_day", "")))
    except ValueError:
        pay_day = None
    if pay_day is None or not bg.is_pay_day(date.fromisoformat(g.user["pay_day"]), pay_day):
        return jsonify(error="That date isn't one of your pay days."), 400
    raw = str(data.get("amount") if data.get("amount") is not None else "").strip()
    db = get_db()
    if raw:
        try:
            amount = csv_io.parse_number(raw)
        except ValueError:
            return jsonify(error=f"{raw} isn't an amount."), 400
        db.execute(
            """INSERT INTO budget_entries (line_id, pay_day, amount) VALUES (?, ?, ?)
               ON CONFLICT (line_id, pay_day) DO UPDATE SET amount = excluded.amount""",
            (line["id"], pay_day.isoformat(), amount),
        )
    else:
        db.execute("DELETE FROM budget_entries WHERE line_id = ? AND pay_day = ?", (line["id"], pay_day.isoformat()))
    db.commit()
    return jsonify(budget_plan())
