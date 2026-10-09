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
    if t.kind in (TRANSFER, OTHER) and t.symbol:
        return t.quantity  # in-kind transfers, reinvested dividends, splits
    return 0.0


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
    cost_native: float = 0.0
    cost_base: float = 0.0
    invested_base: float = 0.0  # total ever bought, for return percentages
    realized_base: float = 0.0
    dividends_base: float = 0.0
    last_price: float = 0.0


def build_lots(txns, fx):
    """Average-cost positions keyed by (account_id, symbol)."""
    lots = {}
    for t in sorted(txns, key=lambda t: (t.date, t.id)):
        if not t.symbol or t.kind not in (BUY, SELL, TRANSFER, OTHER, DIVIDEND):
            continue
        key = (t.account_id, t.symbol)
        lot = lots.get(key)
        if lot is None:
            lot = lots[key] = Lot(t.account_id, t.symbol, t.currency)
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
                cost = -t.net_amount if t.net_amount < 0 else q * t.price + abs(t.costs)
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
    txns = sorted(txns, key=lambda t: (t.date, t.id))
    holdings_only = is_holdings_only(txns)
    fx = FxBook(market, base, (txns[0].date if txns else date.today()) - timedelta(days=7))
    fx.preload({t.currency for t in txns})
    lots = build_lots(txns, fx)
    warnings = []

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
            "unrealized": market_value - cost_base,
            "unrealized_pct": _ratio(market_value - cost_base, cost_base),
            "day_change": day_change,
            "day_change_pct": _ratio(price - prev, prev) if prev else None,
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
        if t.kind == TRANSFER and t.symbol:
            contributions += qty_delta(t) * t.price * fx.on(t.currency, t.date)

    market_value = sum(h["market_value"] for h in holdings)
    cash_total = sum(c["amount_base"] for c in cash)
    total_value = market_value + (0.0 if holdings_only else cash_total)
    book_cost = sum(h["book_cost"] for h in holdings)
    day_change = sum(h["day_change"] for h in holdings)
    total_gain = total_value - contributions

    for h in holdings:
        h["weight"] = _ratio(h["market_value"], total_value)

    sector_totals = defaultdict(float)
    weight_sets = _parallel_map(
        lambda h: market.sector_weights(h["yahoo_symbol"], h["quote_type"]) if h["yahoo_symbol"] else {"Unclassified": 1.0},
        holdings,
    )
    for h, weights in zip(holdings, weight_sets):
        for sector, w in weights.items():
            sector_totals[sector] += h["market_value"] * w
    if not holdings_only and cash_total > 0:
        sector_totals["Cash"] += cash_total
    sector_sum = sum(v for v in sector_totals.values() if v > 0)
    sectors = [
        {"sector": s, "value": v, "pct": v / sector_sum}
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
        "warnings": fx.warnings + warnings,
    }


# -- performance history -------------------------------------------------------

PERIODS = (("1M", 30), ("3M", 91), ("6M", 182), ("YTD", None), ("1Y", 365), ("All", None))


def history(txns, market, base="CAD", benchmark="VFV.TO", today=None):
    """Daily portfolio value, net contributions and time-weighted return, alongside the
    benchmark both as a return index and as "the same contributions invested in it"."""
    today = today or date.today()
    txns = sorted(txns, key=lambda t: (t.date, t.id))
    empty = {"points": [], "periods": [], "benchmark": benchmark, "holdings_only": True}
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
        return market.history(ysyms[sym], fetch_from) if ysyms[sym] else (None, {})

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

    qty, cash, last_trade = defaultdict(float), defaultdict(float), {}

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
            if t.symbol and abs(dq) > EPS:
                qty[t.symbol] += dq
                if t.price > 0:
                    last_trade[t.symbol] = t.price
            cash[t.currency] += t.net_amount
            pending_flow += external_cash_flow(t, holdings_only) * fx.on(t.currency, d)
            if t.kind == TRANSFER and t.symbol and abs(dq) > EPS:
                pending_flow += dq * price_on(t.symbol, d) * fx.on(price_currency[t.symbol], d)

        if d.weekday() < 5 or d == today:  # weekend activity rolls into the next weekday
            value = sum(q * price_on(s, d) * fx.on(price_currency[s], d) for s, q in qty.items() if abs(q) > EPS)
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
        "benchmark": benchmark,
        "holdings_only": holdings_only,
        "warnings": fx.warnings,
    }


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
    return out


# -- single position -----------------------------------------------------------


def position_detail(txns, symbol, account_labels, market, base="CAD", today=None):
    """One symbol: live holding, per-account cost basis, returns and price history."""
    today = today or date.today()
    txns = sorted((t for t in txns if t.symbol == symbol), key=lambda t: (t.date, t.id))
    if not txns:
        return None
    snap = snapshot(txns, account_labels, market, base)
    holding = snap["holdings"][0] if snap["holdings"] else None

    fx = FxBook(market, base, txns[0].date - timedelta(days=7))
    lots = build_lots(txns, fx)
    invested = sum(l.invested_base for l in lots.values())
    realized = sum(l.realized_base for l in lots.values())
    dividends = snap["totals"]["dividends"]
    unrealized = holding["unrealized"] if holding else 0.0
    total_return = unrealized + realized + dividends

    ysym = market.resolve(symbol, txns[0].currency)
    chart_start = min(txns[0].date, today - timedelta(days=365))
    currency, closes = market.history(ysym, chart_start - timedelta(days=7)) if ysym else (None, {})
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
                "avg_cost": l.cost_native / l.quantity if l.quantity > EPS else None,
                "currency": l.currency,
                "book_cost": l.cost_base,
                "realized": l.realized_base,
                "dividends": l.dividends_base,
            }
            for l in sorted(lots.values(), key=lambda l: account_labels.get(l.account_id, ""))
        ],
        "prices": [{"date": d.isoformat(), "close": c} for d, c in sorted(closes.items()) if d >= chart_start],
        "trades": [
            {"date": t.date.isoformat(), "kind": t.kind, "quantity": abs(t.quantity), "price": t.price}
            for t in txns if t.kind in (BUY, SELL)
        ],
        "warnings": snap["warnings"],
    }
