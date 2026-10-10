"""Pages and JSON endpoints for the signed-in user's portfolio.

Every query is scoped to ``g.user`` so users only ever see their own data.
"""
import re
from datetime import date, datetime

from flask import (
    Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for,
)

from . import csv_io
from . import portfolio as pf
from .auth import login_required
from .db import get_db
from .market import get_market

bp = Blueprint("views", __name__)
PAGE_SIZE = 100
CASH_KINDS = (pf.DIVIDEND, pf.DEPOSIT, pf.WITHDRAWAL, pf.TRANSFER, pf.FEE, pf.INTEREST, pf.FX, pf.OTHER)


# -- template helpers -----------------------------------------------------------


@bp.app_template_filter("money")
def money_filter(value, currency="CAD"):
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f} {currency}"


@bp.app_template_filter("qty")
def qty_filter(value):
    if not value:
        return "—"
    return f"{value:,.6f}".rstrip("0").rstrip(".")


@bp.app_context_processor
def inject_globals():
    return {"kind_labels": pf.KIND_LABELS, "base_currency": current_app.config["BASE_CURRENCY"]}


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


def load_txns(account_id=None, symbol=None):
    sql, params = "SELECT * FROM transactions WHERE user_id = ?", [g.user["id"]]
    if account_id:
        sql += " AND account_id = ?"
        params.append(account_id)
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    return [pf.Txn.from_row(r) for r in get_db().execute(sql + " ORDER BY date, id", params)]


def has_transactions():
    return get_db().execute("SELECT 1 FROM transactions WHERE user_id = ? LIMIT 1", (g.user["id"],)).fetchone() is not None


# -- dashboard & positions ------------------------------------------------------------


@bp.get("/")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        accounts=user_accounts(),
        account_id=selected_account_id(),
        has_transactions=has_transactions(),
        benchmark=current_app.config["BENCHMARK_SYMBOL"],
    )


@bp.get("/position/<symbol>")
@login_required
def position(symbol):
    symbol = symbol.upper()
    exists = get_db().execute(
        "SELECT 1 FROM transactions WHERE user_id = ? AND symbol = ? LIMIT 1", (g.user["id"], symbol)
    ).fetchone()
    if not exists:
        abort(404)
    return render_template("position.html", symbol=symbol, accounts=user_accounts(), account_id=selected_account_id())


@bp.get("/api/portfolio")
@login_required
def api_portfolio():
    data = pf.snapshot(
        load_txns(selected_account_id()), account_labels(), get_market(), current_app.config["BASE_CURRENCY"]
    )
    data["as_of"] = datetime.now().isoformat(timespec="seconds")
    return jsonify(data)


@bp.get("/api/history")
@login_required
def api_history():
    return jsonify(pf.history(
        load_txns(selected_account_id()), get_market(),
        current_app.config["BASE_CURRENCY"], current_app.config["BENCHMARK_SYMBOL"],
    ))


@bp.get("/api/position/<symbol>")
@login_required
def api_position(symbol):
    symbol = symbol.upper()
    data = pf.position_detail(
        load_txns(selected_account_id(), symbol), symbol, account_labels(), get_market(),
        current_app.config["BASE_CURRENCY"],
    )
    if data is None:
        return jsonify(error="No transactions for this symbol in the selected account."), 404
    rows = get_db().execute(
        """SELECT t.*, a.number, a.name, a.type FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.user_id = ? AND t.symbol = ? ORDER BY t.date DESC, t.id DESC""",
        (g.user["id"], symbol),
    ).fetchall()
    account_id = selected_account_id()
    data["transactions"] = [
        {
            "id": r["id"], "date": r["date"], "kind": pf.KIND_LABELS.get(r["kind"], r["kind"]), "action": r["action"],
            "quantity": r["quantity"], "price": r["price"], "costs": r["costs"], "net_amount": r["net_amount"],
            "currency": r["currency"], "account": f"{r['name'] or r['type'] or 'Account'} · {r['number']}",
            "edit_url": url_for("views.edit_transaction", txn_id=r["id"]),
        }
        for r in rows if not account_id or r["account_id"] == account_id
    ]
    return jsonify(data)


# -- transactions -------------------------------------------------------------------------


@bp.get("/transactions")
@login_required
def transactions():
    account_id = selected_account_id()
    symbol = request.args.get("symbol", "").strip().upper()
    kind = request.args.get("kind", "")
    page = max(int(request.args.get("page", "1")) if request.args.get("page", "1").isdigit() else 1, 1)

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
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    return render_template(
        "transactions.html",
        rows=rows,
        accounts=user_accounts(),
        account_id=account_id,
        symbol=symbol,
        kind=kind,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total=total,
    )


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
        return redirect(url_for("views.accounts"))
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
            return redirect(url_for("views.transactions"))
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
            return redirect(url_for("views.transactions"))
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
    return redirect(request.referrer if request.referrer and "/edit" not in request.referrer
                    else url_for("views.transactions"))


# -- accounts ---------------------------------------------------------------------------


@bp.route("/accounts", methods=["GET", "POST"])
@login_required
def accounts():
    db = get_db()
    if request.method == "POST":
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
            return redirect(url_for("views.accounts"))
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
    return render_template("accounts.html", rows=rows, cash=cash)


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
            return redirect(url_for("views.accounts"))
    return render_template("account_form.html", account=account)


@bp.post("/accounts/<int:account_id>/delete")
@login_required
def delete_account(account_id):
    account = get_account_or_404(account_id)
    db = get_db()
    db.execute("DELETE FROM accounts WHERE id = ? AND user_id = ?", (account_id, g.user["id"]))
    db.commit()
    flash(f"Deleted {account_label(account)} and its transactions.", "success")
    return redirect(url_for("views.accounts"))


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
    rows = db.execute("SELECT * FROM portfolios WHERE user_id = ? ORDER BY name", (g.user["id"],)).fetchall()
    labels = {a["id"]: account_label(a) for a in accounts}
    members = {}
    for m in db.execute(
        """SELECT pa.portfolio_id, pa.account_id FROM portfolio_accounts pa
           JOIN portfolios p ON p.id = pa.portfolio_id WHERE p.user_id = ?""",
        (g.user["id"],),
    ):
        members.setdefault(m["portfolio_id"], []).append(labels[m["account_id"]])
    return render_template("portfolios.html", rows=rows, members=members, accounts=accounts,
                           selected=set(), label=account_label)


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
    result = None
    if request.method == "POST":
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
    return render_template("import.html", result=result, columns=csv_io.COLUMNS, accounts=user_accounts())


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
