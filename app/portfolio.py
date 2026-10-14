"""Portfolio calculations: positions, cash, live valuation and performance history.

Everything here is a pure function of a list of transactions and a market-data
object, so it can be tested with a fake market. Amounts are in the base currency
(CAD) unless a key says ``native``.

Conventions
-----------
* ``net_amount`` is the cash effect of a transaction in its own currency
  (negative for buys, fees and withdrawals; positive for sells, dividends and deposits).
* Cash balance per account and currency = sum of ``net_amount``.
* Cost basis uses the average-cost method (the Canadian ACB convention). Book cost in
  CAD uses the exchange rate on each purchase date, so P/L includes currency gains.
* If no deposits, withdrawals or cash transfers are recorded at all, the portfolio is
  treated as "holdings only": every buy counts as a contribution, every sale or dividend
  as money returned, and cash balances are left out of the portfolio value.
* A split is reported as a pair of rows on the same day: the old shares out (negative
  quantity) and the new shares in. It converts the account's whole position, so the new
  shares carry all of the old cost and the account holds exactly the new quantity.
"""
from __future__ import annotations

import bisect
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta

BUY, SELL, DIVIDEND, DEPOSIT, WITHDRAWAL = "BUY", "SELL", "DIVIDEND", "DEPOSIT", "WITHDRAWAL"
TRANSFER, FEE, INTEREST, FX, OTHER = "TRANSFER", "FEE", "INTEREST", "FX", "OTHER"
SPLIT = "SPLIT"

KIND_LABELS = {
    BUY: "Buy",
    SELL: "Sell",
    DIVIDEND: "Dividend",
    DEPOSIT: "Deposit",
    WITHDRAWAL: "Withdrawal",
    TRANSFER: "Transfer",
    FEE: "Fee",
    INTEREST: "Interest",
    FX: "FX Conversion",
    SPLIT: "Split",
    OTHER: "Other",
}
ACTIVITY_TYPES = {
    BUY: "Trades",
    SELL: "Trades",
    DIVIDEND: "Dividends",
    DEPOSIT: "Deposits",
    WITHDRAWAL: "Withdrawals",
    TRANSFER: "Transfers",
    FEE: "Fees and rebates",
    INTEREST: "Interest",
    FX: "FX conversion",
    SPLIT: "Corporate actions",
    OTHER: "Other",
}
EPS = 1e-9


def max_threads():
    """Worker threads for market-data requests. PythonAnywhere web apps can't start
    threads, so it defaults to 1 (run sequentially) there."""
    default = "1" if "PYTHONANYWHERE_DOMAIN" in os.environ else "8"
    return max(int(os.environ.get("MAX_THREADS") or default), 1)


@dataclass
class Txn:
    id: int
    date: date
    kind: str
    symbol: str
    quantity: float
    price: float
    costs: float
    net_amount: float
    currency: str
    account_id: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            date=date.fromisoformat(row["date"]),
            kind=row["kind"],
            symbol=(row["symbol"] or "").upper(),
            quantity=row["quantity"] or 0.0,
            price=row["price"] or 0.0,
            costs=row["costs"] or 0.0,
            net_amount=row["net_amount"] or 0.0,
            currency=row["currency"],
            account_id=row["account_id"],
        )


# -- transaction rules ---------------------------------------------------------


def qty_delta(t):
    """Change in shares held caused by a transaction."""
    if t.kind == BUY:
        return abs(t.quantity)
    if t.kind == SELL:
        return -abs(t.quantity)
    if t.kind in (TRANSFER, OTHER, SPLIT) and t.symbol:
        return t.quantity  # in-kind transfers, reinvested dividends, splits
    return 0.0


def split_groups(txns):
    """Split rows by (account, date): each group is one day's split(s) in one account."""
    groups = defaultdict(list)
    for t in txns:
        if t.kind == SPLIT and t.symbol:
            groups[t.account_id, t.date].append(t)
    return groups


def split_pairs(rows):
    """Pair a split group's old-shares-out rows with its new-shares-in rows. Rows pair by
    symbol; a single leftover old and new row pair with each other (the security changed
    symbol). Returns [(old or None, new or None)]."""
    old = [t for t in rows if t.quantity < 0]
    new = [t for t in rows if t.quantity > 0]
    pairs = []
    for o in list(old):
        n = next((n for n in new if n.symbol == o.symbol), None)
        if n:
            pairs.append((o, n))
            old.remove(o)
            new.remove(n)
    if len(old) == 1 and len(new) == 1:
        pairs.append((old.pop(), new.pop()))
    return pairs + [(o, None) for o in old] + [(None, n) for n in new]


def with_opening_positions(txns):
    """Add the shares an account held before its imported history starts.

    A broker export covers a limited period, so a sale can draw on shares bought before
    its first row, and the position would go negative. For each account and symbol whose
    share count dips below zero, the shortfall is added as shares transferred in on the
    account's first date (id < 0 marks them). Like any transfer without a price, they are
    valued at that day's close, both as book cost and as money contributed."""
    txns = list(txns)
    if any(t.id < 0 for t in txns):  # already added
        return txns
    start, currency = {}, {}
    for t in txns:
        start[t.account_id] = min(start.get(t.account_id, t.date), t.date)
        if t.symbol:
            currency.setdefault((t.account_id, t.symbol), t.currency)
    held, lowest = defaultdict(float), defaultdict(float)
    splits = split_groups(txns)
    for t in sorted(txns, key=lambda t: (t.date, qty_delta(t) < 0, t.id)):  # same order as build_lots
        if not t.symbol:
            continue
        if t.kind == SPLIT:  # same rules as apply_split
            for old, new in split_pairs(splits.pop((t.account_id, t.date), [])):
                if old and new:
                    held[old.account_id, old.symbol] = 0.0
                elif old:
                    held[old.account_id, old.symbol] = max(held[old.account_id, old.symbol] + old.quantity, 0.0)
                if new:
                    held[new.account_id, new.symbol] += new.quantity
            continue
        key = (t.account_id, t.symbol)
        held[key] += qty_delta(t)
        lowest[key] = min(lowest[key], held[key])
    opening = [
        Txn(-(i + 1), start[acct], TRANSFER, sym, -low, 0.0, 0.0, 0.0, currency[acct, sym], acct)
        for i, ((acct, sym), low) in enumerate(sorted(lowest.items())) if low < -1e-7
    ]
    return opening + txns


def compute_net_amount(kind, quantity=0.0, price=0.0, costs=0.0, amount=0.0):
    """Cash effect of a manually entered transaction."""
    q, c = abs(quantity), abs(costs)
    if kind == BUY:
        return -(q * price + c)
    if kind == SELL:
        return q * price - c
    if kind in (DEPOSIT, INTEREST):
        return abs(amount)
    if kind in (WITHDRAWAL, FEE):
        return -abs(amount)
    return amount


def is_holdings_only(txns):
    return not any(
        t.kind in (DEPOSIT, WITHDRAWAL) or (t.kind == TRANSFER and abs(t.net_amount) > EPS) for t in txns
    )


def external_cash_flow(t, holdings_only):
    """Money entering (+) or leaving (-) the portfolio, in the transaction currency."""
    if holdings_only:
        return -t.net_amount
    if t.kind in (DEPOSIT, WITHDRAWAL, TRANSFER):
        return t.net_amount
    return 0.0


# -- time series helpers -------------------------------------------------------


class Series:
    """Daily values with forward-fill lookup."""

    def __init__(self, points):
        items = sorted(points.items())
        self.dates = [d for d, _ in items]
        self.values = [v for _, v in items]

    def at(self, d, backfill=False):
        i = bisect.bisect_right(self.dates, d) - 1
        if i >= 0:
            return self.values[i]
        return self.values[0] if backfill and self.values else None

    def last(self):
        return self.values[-1] if self.values else None


class FxBook:
    """Exchange rates into the base currency, historical and current."""

    def __init__(self, market, base, start):
        self.market, self.base, self.start = market, base, start
        self._history, self._now = {}, {}
        self.warnings = []

    def preload(self, currencies):
        needed = [c for c in currencies if c and c != self.base and c not in self._history]
        for cur, hist in zip(needed, _parallel_map(lambda c: self.market.fx_history(c, self.start), needed)):
            self._history[cur] = Series(hist)

    def _series(self, currency):
        if currency not in self._history:
            self._history[currency] = Series(self.market.fx_history(currency, self.start))
        return self._history[currency]

    def on(self, currency, d):
        if not currency or currency == self.base:
            return 1.0
        rate = self._series(currency).at(d, backfill=True)
        return rate if rate else self.now(currency)

    def now(self, currency):
        if not currency or currency == self.base:
            return 1.0
        if currency not in self._now:
            rate = self.market.fx_rate(currency) or self._series(currency).last()
            if not rate:
                self.warnings.append(f"No {currency}→{self.base} exchange rate available; using 1.0.")
                rate = 1.0
            self._now[currency] = rate
        return self._now[currency]


# -- positions -----------------------------------------------------------------


@dataclass
class Lot:
    """Running position in one symbol within one account."""

    account_id: int
    symbol: str
    currency: str
    quantity: float = 0.0
    cost_native: float = 0.0  # book cost: price paid for the shares held, commissions excluded
    cost_base: float = 0.0
    invested_base: float = 0.0  # total ever bought, for return percentages
    realized_base: float = 0.0
    dividends_base: float = 0.0
    last_price: float = 0.0

    @property
    def avg_cost(self):
        """Average price paid per share."""
        return self.cost_native / self.quantity if self.quantity > EPS else None


class TransferPricer:
    """Value per share of an in-kind transfer: the row's own price, else that day's
    market close. Brokers report shares moved in or out of an account without a price."""

    def __init__(self, market, start):
        self.market, self.start = market, start
        self._closes = {}
        self.warnings = []

    def __call__(self, t):
        if t.price > 0:
            return t.price, t.currency
        if t.symbol not in self._closes:
            ysym = self.market.resolve(t.symbol, t.currency)
            currency, closes = self.market.history(ysym, self.start, as_traded=True) if ysym else (None, {})
            self._closes[t.symbol] = (Series(closes), currency or t.currency)
        series, currency = self._closes[t.symbol]
        price = series.at(t.date)
        if not price:
            self.warnings.append(f"No price for the {t.symbol} transfer on {t.date}; it is valued at zero.")
        return price or 0.0, currency


def build_lots(txns, fx, transfer_price=None):
    """Average-cost positions keyed by (account_id, symbol). ``transfer_price`` values
    shares transferred in without a price (see TransferPricer); without it they cost nothing."""
    lots = {}

    def lot_for(t):
        key = (t.account_id, t.symbol)
        if key not in lots:
            lots[key] = Lot(t.account_id, t.symbol, t.currency)
        return lots[key]

    splits = split_groups(txns)
    # Within a day, apply shares received before shares removed: exports list same-day rows
    # newest-first or oldest-first depending on the broker, and a sale can only come from
    # shares already held.
    for t in sorted(txns, key=lambda t: (t.date, qty_delta(t) < 0, t.id)):
        if not t.symbol or t.kind not in (BUY, SELL, TRANSFER, OTHER, DIVIDEND, SPLIT):
            continue
        if t.kind == SPLIT:  # the whole group is applied at its first row
            for old, new in split_pairs(splits.pop((t.account_id, t.date), [])):
                apply_split(old and lot_for(old), new and lot_for(new), old, new)
            continue
        lot = lot_for(t)
        rate = fx.on(t.currency, t.date)
        if t.kind == DIVIDEND:
            lot.dividends_base += t.net_amount * rate
            continue
        q = qty_delta(t)
        if abs(q) < EPS:
            continue
        if t.price > 0:
            lot.last_price = t.price
        if q > 0:
            if t.kind == BUY:
                paid = -t.net_amount if t.net_amount < 0 else q * t.price + abs(t.costs)
                cost = q * t.price if t.price > 0 else paid - abs(t.costs)
                lot.invested_base += paid * rate
                lot.realized_base -= (paid - cost) * rate  # commissions are an expense, not book cost
            elif t.kind == TRANSFER and transfer_price:  # shares deposited in kind, at market value
                price, currency = transfer_price(t)
                cost = q * price * fx.on(currency, t.date) / rate if currency != t.currency else q * price
                lot.invested_base += cost * rate
            else:
                cost = q * t.price
            lot.quantity += q
            lot.cost_native += cost
            lot.cost_base += cost * rate
        else:
            sold = -q
            frac = min(sold / lot.quantity, 1.0) if lot.quantity > EPS else 0.0
            cost_native, cost_base = lot.cost_native * frac, lot.cost_base * frac
            lot.quantity -= sold
            lot.cost_native -= cost_native
            lot.cost_base -= cost_base
            if t.kind == SELL:
                proceeds = t.net_amount if t.net_amount > 0 else sold * t.price - abs(t.costs)
                lot.realized_base += proceeds * rate - cost_base
        if abs(lot.quantity) < 1e-7:
            lot.quantity = lot.cost_native = lot.cost_base = 0.0
    return lots


def apply_split(src, dst, old, new):
    """Apply one pair from split_pairs to its lots (src for the old row, dst for the new).
    The new shares take over the whole old position and its cost. The old row's quantity
    is not subtracted: if the imported history is missing some of the old shares, the
    account still ends up with exactly the new quantity the broker reports."""
    if src and dst:
        cost_native, cost_base, last_price = src.cost_native, src.cost_base, src.last_price
        src.quantity = src.cost_native = src.cost_base = 0.0
        dst.quantity += new.quantity
        dst.cost_native += cost_native
        dst.cost_base += cost_base
        if last_price:
            dst.last_price = last_price * -old.quantity / new.quantity
    elif src:  # shares removed with nothing in exchange
        frac = min(-old.quantity / src.quantity, 1.0) if src.quantity > EPS else 0.0
        src.quantity = max(src.quantity + old.quantity, 0.0)
        src.cost_native -= src.cost_native * frac
        src.cost_base -= src.cost_base * frac
    elif dst:  # shares received at no cost
        dst.quantity += new.quantity


def _parallel_map(fn, items):
    items = list(items)
    workers = max_threads()
    if len(items) <= 1 or workers == 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(fn, items))


def _ratio(numerator, denominator):
    return numerator / denominator if denominator and abs(denominator) > EPS else None


# -- live snapshot -------------------------------------------------------------


def snapshot(txns, account_labels, market, base="CAD"):
    """Current holdings at live prices, cash balances, totals and sector allocation."""
    txns = sorted(with_opening_positions(txns), key=lambda t: (t.date, t.id))
    holdings_only = is_holdings_only(txns)
    fx = FxBook(market, base, (txns[0].date if txns else date.today()) - timedelta(days=7))
    fx.preload({t.currency for t in txns})
    transfer_price = TransferPricer(market, (txns[0].date if txns else date.today()) - timedelta(days=10))
    lots = build_lots(txns, fx, transfer_price)
    warnings = [
        f"{t.quantity:g} {t.symbol} shares were sold before any purchase in the imported history; "
        f"they are assumed held from {t.date} and valued at that day's price."
        for t in txns if t.id < 0
    ]

    by_symbol = defaultdict(list)
    for lot in lots.values():
        by_symbol[lot.symbol].append(lot)
    symbols = sorted(by_symbol)
    ysyms = dict(zip(symbols, _parallel_map(lambda s: market.resolve(s, by_symbol[s][0].currency), symbols)))
    quotes = market.quotes([y for y in ysyms.values() if y])

    holdings, closed = [], []
    for sym in symbols:
        ls = by_symbol[sym]
        qty = sum(l.quantity for l in ls)
        realized = sum(l.realized_base for l in ls)
        dividends = sum(l.dividends_base for l in ls)
        if abs(qty) < EPS:
            if any(l.invested_base for l in ls):
                closed.append({"symbol": sym, "realized": realized, "dividends": dividends})
            continue
        ysym = ysyms[sym]
        q = quotes.get(ysym) if ysym else None
        trade_currency = ls[0].currency
        if q and q.get("price") is not None:
            price, prev, currency = q["price"], q.get("prev_close"), q["currency"] or trade_currency
        else:
            price = next((l.last_price for l in ls if l.last_price), 0.0)
            prev, currency = None, trade_currency
            warnings.append(f"No live price for {sym}; valued at its last trade price.")
        rate = fx.now(currency)
        cost_native = sum(l.cost_native for l in ls)
        cost_base = sum(l.cost_base for l in ls)
        market_value = qty * price * rate
        day_change = qty * (price - prev) * rate if prev else 0.0
        holdings.append({
            "symbol": sym,
            "yahoo_symbol": ysym,
            "name": (q or {}).get("name") or sym,
            "quote_type": (q or {}).get("quote_type"),
            "currency": currency,
            "cost_currency": trade_currency,
            "quantity": qty,
            "price": price,
            "prev_close": prev,
            "live": q is not None,
            "avg_cost": cost_native / qty,
            "market_value_native": qty * price,
            "market_value": market_value,
            "book_cost": cost_base,
            "book_cost_native": cost_native,
            "unrealized": market_value - cost_base,
            "unrealized_pct": _ratio(market_value - cost_base, cost_base),
            "day_change": day_change,
            "day_change_pct": _ratio(price - prev, prev) if prev else None,
            "earnings": (q or {}).get("earnings"),
            "realized": realized,
            "dividends": dividends,
            "accounts": [
                {"account_id": l.account_id, "account": account_labels.get(l.account_id, ""), "quantity": l.quantity}
                for l in ls if abs(l.quantity) > EPS
            ],
        })
    holdings.sort(key=lambda h: h["market_value"], reverse=True)

    balances = defaultdict(float)
    for t in txns:
        balances[(t.account_id, t.currency)] += t.net_amount
    cash = [
        {
            "account_id": acct,
            "account": account_labels.get(acct, ""),
            "currency": cur,
            "amount": amount,
            "amount_base": amount * fx.now(cur),
        }
        for (acct, cur), amount in sorted(balances.items(), key=lambda kv: (account_labels.get(kv[0][0], ""), kv[0][1]))
        if abs(amount) >= 0.005
    ]

    contributions = 0.0
    for t in txns:
        contributions += external_cash_flow(t, holdings_only) * fx.on(t.currency, t.date)
        if t.kind == TRANSFER and t.symbol and abs(qty_delta(t)) > EPS:
            price, currency = transfer_price(t)
            contributions += qty_delta(t) * price * fx.on(currency, t.date)

    market_value = sum(h["market_value"] for h in holdings)
    cash_total = sum(c["amount_base"] for c in cash)
    total_value = market_value + (0.0 if holdings_only else cash_total)
    book_cost = sum(h["book_cost"] for h in holdings)
    day_change = sum(h["day_change"] for h in holdings)
    total_gain = total_value - contributions

    for h in holdings:
        h["weight"] = _ratio(h["market_value"], total_value)

    sector_totals, sector_change = defaultdict(float), defaultdict(float)
    weight_sets = _parallel_map(
        lambda h: market.sector_weights(h["yahoo_symbol"], h["quote_type"]) if h["yahoo_symbol"] else {"Unclassified": 1.0},
        holdings,
    )
    for h, weights in zip(holdings, weight_sets):
        for sector, w in weights.items():
            sector_totals[sector] += h["market_value"] * w
            sector_change[sector] += h["day_change"] * w  # an ETF's move is split across its sectors
    if not holdings_only and cash_total > 0:
        sector_totals["Cash"] += cash_total
    sector_sum = sum(v for v in sector_totals.values() if v > 0)
    sectors = [
        {"sector": s, "value": v, "pct": v / sector_sum, "day_change": sector_change[s],
         "day_change_pct": None if s == "Cash" else _ratio(sector_change[s], v - sector_change[s])}
        for s, v in sorted(sector_totals.items(), key=lambda kv: kv[1], reverse=True)
        if v > 0
    ]

    return {
        "base_currency": base,
        "holdings_only": holdings_only,
        "totals": {
            "total_value": total_value,
            "market_value": market_value,
            "cash": cash_total,
            "book_cost": book_cost,
            "unrealized": market_value - book_cost,
            "unrealized_pct": _ratio(market_value - book_cost, book_cost),
            "day_change": day_change,
            "day_change_pct": _ratio(day_change, total_value - day_change),
            "net_contributions": contributions,
            "total_gain": total_gain,
            "total_gain_pct": _ratio(total_gain, contributions),
            "realized": sum(l.realized_base for l in lots.values()),
            "dividends": sum(t.net_amount * fx.on(t.currency, t.date) for t in txns if t.kind == DIVIDEND),
        },
        "holdings": holdings,
        "closed": closed,
        "cash": cash,
        "sectors": sectors,
        "warnings": fx.warnings + list(dict.fromkeys(transfer_price.warnings)) + warnings,
    }


# -- performance history -------------------------------------------------------

PERIODS = (("1M", 30), ("3M", 91), ("6M", 182), ("YTD", None), ("1Y", 365), ("5Y", 1826), ("All", None))


def history(txns, market, base="CAD", benchmark="VFV.TO", today=None):
    """Daily portfolio value, net contributions and time-weighted return, alongside the
    benchmark both as a return index and as "the same contributions invested in it"."""
    today = today or date.today()
    txns = sorted(with_opening_positions(txns), key=lambda t: (t.date, t.id))
    empty = {"points": [], "periods": [], "years": [], "benchmark": benchmark, "holdings_only": True}
    if not txns:
        return empty
    holdings_only = is_holdings_only(txns)
    start = txns[0].date
    fetch_from = start - timedelta(days=10)

    currency_of = {}
    for t in txns:
        if t.symbol and abs(qty_delta(t)) > EPS:
            currency_of.setdefault(t.symbol, t.currency)
    symbols = sorted(currency_of)
    ysyms = dict(zip(symbols, _parallel_map(lambda s: market.resolve(s, currency_of[s]), symbols)))

    def fetch(sym):
        return market.history(ysyms[sym], fetch_from, as_traded=True) if ysyms[sym] else (None, {})

    fetched = dict(zip(symbols, _parallel_map(fetch, symbols)))
    prices = {s: Series(fetched[s][1]) for s in symbols}
    price_currency = {s: fetched[s][0] or currency_of[s] for s in symbols}
    bench_currency, bench_closes = market.history(benchmark, fetch_from)
    bench_prices = Series(bench_closes)

    fx = FxBook(market, base, fetch_from)
    fx.preload({t.currency for t in txns} | set(price_currency.values()) | {bench_currency})

    by_day = defaultdict(list)
    for t in txns:
        by_day[t.date].append(t)

    qty, cash, last_trade = defaultdict(float), defaultdict(float), {}  # qty by (account, symbol)
    splits = split_groups(txns)

    def price_on(sym, d):
        p = prices[sym].at(d)
        return p if p is not None else last_trade.get(sym, 0.0)

    points = []
    prev_value = pending_flow = contributions = 0.0
    index = 1.0
    bench_units = 0.0
    bench_start = None
    d = start
    while d <= today:
        for t in by_day.get(d, ()):
            dq = qty_delta(t)
            if t.kind == SPLIT:  # same rules as apply_split
                for old, new in split_pairs(splits.pop((t.account_id, d), [])):
                    if old:
                        key = (old.account_id, old.symbol)
                        qty[key] = 0.0 if new else max(qty[key] + old.quantity, 0.0)
                    if new:
                        qty[new.account_id, new.symbol] += new.quantity
                    if old and new and old.symbol in last_trade:
                        last_trade[new.symbol] = last_trade[old.symbol] * -old.quantity / new.quantity
            elif t.symbol and abs(dq) > EPS:
                qty[t.account_id, t.symbol] += dq
                if t.price > 0:
                    last_trade[t.symbol] = t.price
            cash[t.currency] += t.net_amount
            pending_flow += external_cash_flow(t, holdings_only) * fx.on(t.currency, d)
            if t.kind == TRANSFER and t.symbol and abs(dq) > EPS:
                pending_flow += dq * price_on(t.symbol, d) * fx.on(price_currency[t.symbol], d)

        if d.weekday() < 5 or d == today:  # weekend activity rolls into the next weekday
            value = sum(q * price_on(s, d) * fx.on(price_currency[s], d) for (_, s), q in qty.items() if abs(q) > EPS)
            if not holdings_only:
                value += sum(amount * fx.on(cur, d) for cur, amount in cash.items())
            denominator = prev_value + pending_flow  # flows assumed at the start of the day
            if denominator > EPS and value >= 0:
                index *= value / denominator
            contributions += pending_flow

            point = {"date": d.isoformat(), "value": value, "contributions": contributions, "twr": index - 1}
            bench_px = bench_prices.at(d, backfill=True)
            if bench_px:
                bench_px *= fx.on(bench_currency, d)
                bench_start = bench_start or bench_px
                bench_units = max(bench_units + pending_flow / bench_px, 0.0)
                point["benchmark_value"] = bench_units * bench_px
                point["benchmark_twr"] = bench_px / bench_start - 1
            points.append(point)
            prev_value, pending_flow = value, 0.0
        d += timedelta(days=1)

    return {
        "points": points,
        "periods": period_returns(points, today),
        "years": annual_returns(points, today),
        "benchmark": benchmark,
        "holdings_only": holdings_only,
        "warnings": fx.warnings,
    }


def annual_returns(points, today):
    """Performance for each calendar year, oldest first.

    Returns are time-weighted (deposits and withdrawals don't count as gains), chained
    from the last point of the previous year; the first year starts from zero. Values
    are in the base currency: gain = end value - start value - net contributions.
    "partial" marks a first year that started after early January, "ytd" the current year."""
    if not points:
        return []
    by_year = defaultdict(list)
    for p in points:
        by_year[int(p["date"][:4])].append(p)
    out, prev = [], None  # prev: last point of the previous year
    for year in sorted(by_year):
        pts = by_year[year]
        end = pts[-1]
        base = prev or {"twr": 0.0, "benchmark_twr": 0.0, "value": 0.0, "contributions": 0.0}

        def chained(key):
            if key not in end or key not in base:
                return None
            return (1 + end[key]) / (1 + base[key]) - 1

        start_value, contributions = base["value"], end["contributions"] - base["contributions"]
        out.append({
            "year": year,
            "start": pts[0]["date"],
            "end": end["date"],
            "partial": prev is None and pts[0]["date"][5:] > "01-07",
            "ytd": year == today.year,
            "portfolio": chained("twr"),
            "benchmark": chained("benchmark_twr"),
            "start_value": start_value,
            "contributions": contributions,
            "end_value": end["value"],
            "gain": end["value"] - start_value - contributions,
        })
        prev = end
    return out


def period_returns(points, today):
    """Time-weighted returns for standard periods, portfolio vs benchmark."""
    if not points:
        return []
    dates = [date.fromisoformat(p["date"]) for p in points]
    end = points[-1]
    out = []
    for label, days in PERIODS:
        if label == "All":
            base = {"twr": 0.0, "benchmark_twr": 0.0}
        else:
            since = date(today.year, 1, 1) if label == "YTD" else today - timedelta(days=days)
            i = bisect.bisect_left(dates, since) - 1  # last point before the period starts
            if i < 0:
                out.append({"label": label, "portfolio": None, "benchmark": None})
                continue
            base = points[i]
        portfolio = (1 + end["twr"]) / (1 + base["twr"]) - 1
        bench = None
        if "benchmark_twr" in end and "benchmark_twr" in base:
            bench = (1 + end["benchmark_twr"]) / (1 + base["benchmark_twr"]) - 1
        out.append({"label": label, "portfolio": portfolio, "benchmark": bench})
    out.append(annualized(out[-1], (dates[-1] - dates[0]).days / 365.25))
    return out


def annualized(since_inception, years):
    """Compound annual rate of the since-inception returns. Periods under a year
    aren't annualized (standard practice: it would extrapolate a short streak)."""
    def rate(r):
        return None if r is None or years < 1 else (1 + r) ** (1 / years) - 1
    return {"label": "Annualized", "portfolio": rate(since_inception["portfolio"]),
            "benchmark": rate(since_inception["benchmark"])}


# -- single position -----------------------------------------------------------

SMA_LOOKBACK_DAYS = 200  # longest moving average on the position chart, in trading days


def position_detail(txns, symbol, account_labels, market, base="CAD", today=None):
    """One symbol: live holding, per-account cost basis, returns and price history.
    Pass the accounts' full history: shares held before it starts are dated from it."""
    today = today or date.today()
    txns = sorted((t for t in with_opening_positions(txns) if t.symbol == symbol), key=lambda t: (t.date, t.id))
    if not txns:
        return None
    snap = snapshot(txns, account_labels, market, base)
    holding = snap["holdings"][0] if snap["holdings"] else None

    fx = FxBook(market, base, txns[0].date - timedelta(days=7))
    lots = build_lots(txns, fx, TransferPricer(market, txns[0].date - timedelta(days=10)))
    invested = sum(l.invested_base for l in lots.values())
    realized = sum(l.realized_base for l in lots.values())
    dividends = snap["totals"]["dividends"]
    unrealized = holding["unrealized"] if holding else 0.0
    total_return = unrealized + realized + dividends

    ysym = market.resolve(symbol, txns[0].currency)
    chart_start = min(txns[0].date, today - timedelta(days=365))
    # Enough earlier closes that the longest moving average is defined from the chart's first day.
    lookback_start = chart_start - timedelta(days=SMA_LOOKBACK_DAYS * 7 // 5 + 21)
    currency, closes = market.history(ysym, lookback_start, as_traded=True) if ysym else (None, {})
    closes = sorted(closes.items())
    quote = market.quotes([ysym]).get(ysym) if ysym else None

    return {
        "symbol": symbol,
        "yahoo_symbol": ysym,
        "name": (quote or {}).get("name") or symbol,
        "currency": currency or (holding or {}).get("currency") or txns[0].currency,
        "base_currency": base,
        "quote": quote,
        "holding": holding,
        "invested": invested,
        "realized": realized,
        "dividends": dividends,
        "unrealized": unrealized,
        "total_return": total_return,
        "total_return_pct": _ratio(total_return, invested),
        "accounts": [
            {
                "account": account_labels.get(l.account_id, ""),
                "quantity": l.quantity,
                "avg_cost": l.avg_cost,
                "currency": l.currency,
                "book_cost": l.cost_base,
                "realized": l.realized_base,
                "dividends": l.dividends_base,
            }
            for l in sorted(lots.values(), key=lambda l: account_labels.get(l.account_id, ""))
        ],
        "prices": [{"date": d.isoformat(), "close": c} for d, c in closes if d >= chart_start],
        "lookback_closes": [c for d, c in closes if d < chart_start][-(SMA_LOOKBACK_DAYS - 1):],
        "trades": [
            {"date": t.date.isoformat(), "kind": t.kind, "quantity": abs(t.quantity), "price": t.price}
            for t in txns if t.kind in (BUY, SELL)
        ],
        "warnings": snap["warnings"],
    }
