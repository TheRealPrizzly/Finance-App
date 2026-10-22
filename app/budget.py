"""Personal budget planned by pay period.

Pay arrives every two weeks. Each pay period runs from a pay day to the day before the next
one, and each cash, revenue or expense line counts in the period its dates fall in. A line is either
recurring, with a fixed amount on a schedule, or varies, with an amount entered for each pay
period. An amount entered for a recurring line replaces its fixed amount for that one period.
Expenses can be put in groups (Housing, Taxes, ...), which are subtotalled for each pay period.

Amounts are positive. Cash is tracked separately; revenue and expense lines determine the budget
surplus.
"""
from calendar import monthrange
from datetime import date, timedelta

PAY_DAYS = 14
MONTHS_AHEAD = 12
KINDS = {"cash": "Cash", "revenue": "Revenue", "expense": "Expense"}
FREQUENCIES = {
    "pay": "Every pay day",
    "weekly": "Every week",
    "biweekly": "Bi-weekly (every 2 weeks)",
    "monthly": "Every month",
    "quarterly": "Every 3 months",
    "yearly": "Every year",
    "once": "Once",
    "varies": "Varies: entered for each pay day",
}
# Offered when picking an expense's group, along with the groups already used.
SUGGESTED_GROUPS = ("Housing", "Taxes", "Transportation", "Food", "Utilities", "Insurance", "Health",
                    "Debt payments", "Savings", "Entertainment")
MONTH_STEPS = {"monthly": 1, "quarterly": 3, "yearly": 12}
NEEDS_DATE = ("weekly", "biweekly", "monthly", "quarterly", "yearly", "once")  # the date sets the schedule


def add_months(d, months):
    """The same day ``months`` later, or the month's last day when it is shorter (Jan 31 -> Feb 28)."""
    year, month = divmod(d.month - 1 + months, 12)
    year, month = d.year + year, month + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def last_pay_day(anchor, today):
    """The pay day on or before ``today``, from any pay day ``anchor``."""
    return anchor + timedelta(days=(today - anchor).days // PAY_DAYS * PAY_DAYS)


def is_pay_day(anchor, d):
    return (d - anchor).days % PAY_DAYS == 0


def pay_days(anchor, today, months=MONTHS_AHEAD):
    """The current pay period's pay day, then every pay day in the ``months`` after it."""
    d = last_pay_day(anchor, today)
    end = add_months(d, months)
    out = []
    while d < end:
        out.append(d)
        d += timedelta(days=PAY_DAYS)
    return out


def occurrences(line, start, end):
    """The dates in [start, end) when a recurring line is due; ``start`` is a pay day. Lines
    that vary have none."""
    freq = line["frequency"]
    first = date.fromisoformat(line["start_date"]) if line["start_date"] else None
    last = date.fromisoformat(line["end_date"]) if line["end_date"] else None
    if freq == "varies" or (freq in NEEDS_DATE and first is None):
        return []
    if freq == "once":
        dates = [first]
    elif freq in ("pay", "weekly", "biweekly"):
        step = 7 if freq == "weekly" else 14
        # the first date on or after start: start itself for pay days, else whole steps after the first
        d = start if freq == "pay" else first + timedelta(days=max(-(-(start - first).days // step), 0) * step)
        dates = []
        while d < end:
            dates.append(d)
            d += timedelta(days=step)
    else:
        step = MONTH_STEPS[freq]
        k = max((start.year - first.year) * 12 + start.month - first.month, 0) // step
        dates = []
        while (d := add_months(first, k * step)) < end:
            dates.append(d)
            k += 1
    return [d for d in dates if start <= d < end and (first is None or d >= first) and (last is None or d <= last)]


def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def short_date(d, year=True):
    return f"{d:%b} {d.day}, {d.year}" if year else f"{d:%b} {d.day}"


def describe(line, today):
    """The line's schedule in words, e.g. "$1,850.00 on the 1st of every month"."""
    freq = line["frequency"]
    if freq == "varies":
        return "Amount entered for each pay day"
    first = date.fromisoformat(line["start_date"]) if line["start_date"] else None
    amount = f"${line['amount']:,.2f}"
    text = {
        "pay": f"{amount} every pay day",
        "weekly": f"{amount} every {first:%A}" if first else amount,
        "biweekly": f"{amount} every other {first:%A}" if first else amount,
        "monthly": f"{amount} on the {ordinal(first.day)} of every month" if first else amount,
        "quarterly": f"{amount} every 3 months, on the {ordinal(first.day)}" if first else amount,
        "yearly": f"{amount} every year on {short_date(first, year=False)}" if first else amount,
        "once": f"{amount} on {short_date(first)}" if first else amount,
    }[freq]
    if first and first > today and freq != "once":
        text += f", from {short_date(first)}"
    if line["end_date"] and freq != "once":
        text += f", until {short_date(date.fromisoformat(line['end_date']))}"
    return text


def expense_groups(rows, periods):
    """The expense groups in alphabetical order, each with its lines' ids and subtotals. Once any
    expense has a group, the expenses without one come last, as "Other expenses"."""
    expenses = [r for r in rows if r["kind"] == "expense"]
    names = sorted({r["group"] for r in expenses if r["group"]}, key=str.casefold)
    if not names:
        return []
    if any(not r["group"] for r in expenses):
        names.append("")
    out = []
    for name in names:
        members = [r for r in expenses if r["group"] == name]
        totals = [sum(r["cells"][i]["amount"] or 0 for r in members) for i in range(periods)]
        out.append({"name": name or "Other expenses", "named": bool(name), "lines": [r["id"] for r in members],
                    "totals": totals, "total": sum(totals)})
    return out


def plan(lines, entries, anchor, today):
    """The budget for each pay period over the next year.

    ``lines`` are budget_lines rows; ``entries`` maps (line id, pay day ISO date) to an amount
    entered for that period. Each line's cells give the amount counted, the fixed amount its
    schedule gives (if any) and whether an entered amount replaced it."""
    days = pay_days(anchor, today)
    bounds = days + [days[-1] + timedelta(days=PAY_DAYS)]
    rows = []
    for line in lines:
        cells = []
        for start, end in zip(bounds, bounds[1:]):
            due = occurrences(line, start, end)
            scheduled = line["amount"] * len(due) if due else None
            entered = entries.get((line["id"], start.isoformat()))
            cells.append({"amount": entered if entered is not None else scheduled, "scheduled": scheduled,
                          "entered": entered is not None})
        rows.append({
            "id": line["id"], "kind": line["kind"], "name": line["name"], "frequency": line["frequency"],
            "group": line["group_name"] or "", "schedule": describe(line, today), "cells": cells,
            "total": sum(c["amount"] or 0 for c in cells),
        })

    def column_totals(kind):
        return [sum(r["cells"][i]["amount"] or 0 for r in rows if r["kind"] == kind) for i in range(len(days))]

    cash, revenue, expense = (column_totals(kind) for kind in ("cash", "revenue", "expense"))
    net = [r - e for r, e in zip(revenue, expense)]
    running, balance = [], 0.0
    for n in net:
        balance += n
        running.append(balance)
    return {
        "periods": [{"date": s.isoformat(), "end": (e - timedelta(days=1)).isoformat()} for s, e in zip(bounds, bounds[1:])],
        "today": today.isoformat(),
        "lines": rows,
        "groups": expense_groups(rows, len(days)),
        "totals": {"cash": cash, "revenue": revenue, "expense": expense, "net": net, "running": running},
        "sums": {"cash": sum(cash), "revenue": sum(revenue), "expense": sum(expense), "net": sum(net)},
    }
