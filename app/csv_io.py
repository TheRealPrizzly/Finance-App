"""CSV import/export in the broker activity format from the project requirements:

Transaction Date, Action, Symbol, Quantity, Price, Costs, Net Amount, Currency,
Account #, Activity Type, Account Type

Column names are matched loosely (case, spacing and punctuation are ignored, and a few
common broker aliases are accepted), so exports from most Canadian brokers import as-is.
"""
import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from . import portfolio as pf

COLUMNS = [
    "Transaction Date", "Action", "Symbol", "Quantity", "Price", "Costs",
    "Net Amount", "Currency", "Account #", "Activity Type", "Account Type",
]
_ALIASES = {
    "date": ("transactiondate", "date", "tradedate"),
    "action": ("action", "transactiontype", "type"),
    "symbol": ("symbol", "ticker"),
    "quantity": ("quantity", "qty", "shares", "units"),
    "price": ("price", "unitprice"),
    "costs": ("costs", "cost", "commission", "fees", "fee"),
    "net_amount": ("netamount", "net", "amount"),
    "currency": ("currency",),
    "account_number": ("account", "accountnumber", "accountno"),
    "activity_type": ("activitytype", "activity"),
    "account_type": ("accounttype",),
}
_LABELS = {
    "date": "Transaction Date", "action": "Action", "currency": "Currency", "account_number": "Account #",
}
REQUIRED = ("date", "action", "currency", "account_number")
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y")

_BUY = {"BUY", "BOUGHT", "BUY TO OPEN"}
_SELL = {"SELL", "SOLD", "SELL TO CLOSE"}
_DIVIDEND = {"DIV", "DIVIDEND", "DIVIDENDS", "NRT"}
_DEPOSIT = {"DEP", "CON", "DEPOSIT", "CONTRIBUTION"}
_WITHDRAWAL = {"WDR", "WITHDRAWAL", "WITHDRAW"}
_FEE = {"FCH", "FEE", "FEES", "COMMISSION"}
_INTEREST = {"INT", "INTEREST"}
_FX = {"FXT", "FX", "FX CONVERSION", "CONVERSION"}
_TRANSFER = {"TRANSFER", "TF6", "TFO", "TSF", "TRF"}


@dataclass
class Row:
    line: int
    date: date
    action: str
    kind: str
    symbol: str
    quantity: float
    price: float
    costs: float
    net_amount: float
    currency: str
    account_number: str
    activity_type: str
    account_type: str


def classify(action, activity_type, symbol="", quantity=0.0, net_amount=0.0):
    """Map a broker action / activity type to one of the normalized kinds."""
    a, t = action.strip().upper(), activity_type.strip().lower()
    if a in _BUY:
        return pf.BUY
    if a in _SELL:
        return pf.SELL
    if "trade" in t:
        return pf.BUY if quantity > 0 or net_amount < 0 else pf.SELL
    if "reinvest" in t or ((a in _DIVIDEND or "dividend" in t) and symbol and quantity and not net_amount):
        return pf.OTHER  # shares received without cash, e.g. a reinvested dividend
    if a in _DIVIDEND or "dividend" in t:
        return pf.DIVIDEND
    if a in _DEPOSIT or "deposit" in t or "contribution" in t:
        return pf.DEPOSIT
    if a in _WITHDRAWAL or "withdrawal" in t:
        return pf.WITHDRAWAL
    if a in _INTEREST or "interest" in t:
        return pf.INTEREST
    if a in _FEE or "fee" in t:
        return pf.FEE
    if a in _FX or "fx" in t or "exchange" in t:
        return pf.FX
    if a in _TRANSFER or "transfer" in t:
        return pf.TRANSFER
    return pf.OTHER


def parse_number(text):
    s = (text or "").strip()
    if not s or s == "-":
        return 0.0
    negative = s.startswith("(") and s.endswith(")")
    cleaned = re.sub(r"[^0-9.\-eE+]", "", s)
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"'{text}' is not a number") from None
    return -abs(value) if negative else value


def parse_date(text):
    s = (text or "").strip()
    if not s:
        raise ValueError("missing date")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    for candidate in (s, s.split(" ")[0], s.split("T")[0]):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    raise ValueError(f"unrecognized date '{s}'")


def decode(data):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _key(header):
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def parse(text):
    """Parse CSV text. Returns (rows, errors) where errors are (line, message)."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if not header:
        return [], [(0, "The file is empty.")]

    keys = [_key(h) for h in header]
    index = {}
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in keys:
                index[field] = keys.index(alias)
                break
    missing = [_LABELS[f] for f in REQUIRED if f not in index]
    if missing:
        return [], [(1, "Missing required column(s): " + ", ".join(missing))]
    if "net_amount" not in index and not {"quantity", "price"} <= index.keys():
        return [], [(1, "The file needs a Net Amount column, or Quantity and Price columns.")]

    rows, errors = [], []
    for line, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue

        def get(field):
            i = index.get(field)
            return raw[i].strip() if i is not None and i < len(raw) else ""

        try:
            when = parse_date(get("date"))
            quantity = parse_number(get("quantity"))
            price = parse_number(get("price"))
            costs = abs(parse_number(get("costs")))
            net = parse_number(get("net_amount"))
        except ValueError as exc:
            errors.append((line, str(exc)))
            continue
        action, activity = get("action"), get("activity_type")
        symbol, currency = get("symbol").upper(), get("currency").upper()
        account = get("account_number")
        if not action and not activity:
            errors.append((line, "missing action"))
            continue
        if not re.fullmatch(r"[A-Z]{3}", currency):
            errors.append((line, f"invalid currency '{currency}'"))
            continue
        if not account:
            errors.append((line, "missing account number"))
            continue
        kind = classify(action, activity, symbol, quantity, net)
        if kind in (pf.BUY, pf.SELL):
            if not symbol:
                errors.append((line, "trade without a symbol"))
                continue
            if abs(net) < pf.EPS:
                net = pf.compute_net_amount(kind, quantity, price, costs)
        rows.append(Row(line, when, action or pf.KIND_LABELS[kind], kind, symbol, quantity, price, costs, net,
                        currency, account, activity, get("account_type")))
    return rows, errors


def fingerprints(rows):
    """Stable per-row hashes so re-importing the same file doesn't duplicate rows.
    Identical rows within one file are told apart by their occurrence number."""
    seen = Counter()
    for r in rows:
        base = "|".join([
            r.date.isoformat(), r.action, r.symbol, f"{r.quantity:.6f}", f"{r.price:.6f}",
            f"{r.costs:.4f}", f"{r.net_amount:.4f}", r.currency, r.account_number, r.activity_type,
        ])
        n = seen[base]
        seen[base] += 1
        yield hashlib.sha256(f"{base}#{n}".encode()).hexdigest()


def import_rows(db, user_id, rows):
    """Insert parsed rows for a user, creating accounts as needed.
    Returns (imported, duplicates, accounts_created)."""
    accounts = {r["number"]: r["id"] for r in db.execute("SELECT id, number FROM accounts WHERE user_id = ?", (user_id,))}
    imported = duplicates = created = 0
    for row, fp in zip(rows, fingerprints(rows)):
        account_id = accounts.get(row.account_number)
        if account_id is None:
            cur = db.execute(
                "INSERT INTO accounts (user_id, number, type) VALUES (?, ?, ?)",
                (user_id, row.account_number, row.account_type),
            )
            account_id = accounts[row.account_number] = cur.lastrowid
            created += 1
        cur = db.execute(
            """INSERT OR IGNORE INTO transactions
               (user_id, account_id, date, action, kind, symbol, quantity, price, costs, net_amount,
                currency, activity_type, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, account_id, row.date.isoformat(), row.action, row.kind, row.symbol, row.quantity,
             row.price, row.costs, row.net_amount, row.currency, row.activity_type, fp),
        )
        if cur.rowcount:
            imported += 1
        else:
            duplicates += 1
    db.commit()
    return imported, duplicates, created


def export(rows):
    """CSV text for transaction rows joined with their account number and type."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for r in rows:
        writer.writerow([
            r["date"], r["action"], r["symbol"], _fmt(r["quantity"]), _fmt(r["price"]), _fmt(r["costs"]),
            _fmt(r["net_amount"]), r["currency"], r["account_number"], r["activity_type"], r["account_type"],
        ])
    return out.getvalue()


def _fmt(value):
    return f"{value:.8f}".rstrip("0").rstrip(".") if value else "0"
