"""Mortgage amortization: regular payments, balances and payoff projections.

Pure functions, so they can be tested without a database. A mortgage is any mapping with
the columns of the ``mortgages`` table.
"""
import calendar
from datetime import date, timedelta

# key: (label, payments a year)
FREQUENCIES = {
    "monthly": ("Monthly", 12),
    "semi-monthly": ("Semi-monthly", 24),
    "bi-weekly": ("Bi-weekly", 26),
    "accelerated-bi-weekly": ("Accelerated bi-weekly", 26),
    "weekly": ("Weekly", 52),
    "accelerated-weekly": ("Accelerated weekly", 52),
}
# Accelerated payments are the monthly payment split in two or four, so 13 monthly payments are made a year.
ACCELERATED = {"accelerated-bi-weekly": 2, "accelerated-weekly": 4}
# key: (label, times interest is compounded a year)
COMPOUNDING = {
    "semi-annual": ("Semi-annually", 2),
    "monthly": ("Monthly", 12),
}
MAX_YEARS = 100  # a schedule that runs longer than this is treated as never paid off


class NeverPaidOff(ValueError):
    """The payment doesn't cover the interest, so the balance never reaches zero."""


def periodic_rate(annual_pct, compounding, per_year):
    """Interest rate for one payment period, from an annual rate compounded `compounding` times a year."""
    c = COMPOUNDING[compounding][1]
    return (1 + annual_pct / 100 / c) ** (c / per_year) - 1


def add_months(d, months):
    month = d.month - 1 + months
    year, month = d.year + month // 12, month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def months_between(start, end):
    """Whole months from `start` to `end` (negative if `end` is earlier)."""
    months = (end.year - start.year) * 12 + end.month - start.month
    if months > 0 and end.day < start.day and end != add_months(start, months):
        months -= 1
    return months


def payment_date(start, frequency, k):
    """Date of the k-th payment (k = 1 is the first) for a mortgage that started on `start`."""
    if frequency == "monthly":
        return add_months(start, k)
    if frequency == "semi-monthly":
        return add_months(start, k // 2) + timedelta(days=15 * (k % 2))
    return start + timedelta(days=(14 if "bi-weekly" in frequency else 7) * k)


def annuity(principal, rate, periods):
    if periods <= 0:
        return principal
    if rate == 0:
        return principal / periods
    return principal * rate / (1 - (1 + rate) ** -periods)


def regular_payment(balance, annual_pct, compounding, months, frequency):
    """The payment that pays `balance` off over `months` months, rounded up to the cent."""
    per_year = FREQUENCIES[frequency][1]
    if frequency in ACCELERATED:
        monthly = annuity(balance, periodic_rate(annual_pct, compounding, 12), months)
        payment = monthly / ACCELERATED[frequency]
    else:
        periods = round(months * per_year / 12)
        payment = annuity(balance, periodic_rate(annual_pct, compounding, per_year), periods)
    return round(payment + 0.004999, 2)


def amortize(balance, start, after, frequency, rate, payment, boost=None):
    """The payments made after `after` until `balance` is paid off, as
    (date, paid, interest, principal, balance after) tuples. `boost(day)`, if given, is an
    amount added to the payment made on `day`."""
    if balance <= 0.005:
        return []
    if balance * rate >= payment:
        raise NeverPaidOff("The payment doesn't cover the interest, so the mortgage would never be paid off.")
    k = 1
    while payment_date(start, frequency, k) <= after:
        k += 1
    rows = []
    limit = MAX_YEARS * FREQUENCIES[frequency][1]
    while balance > 0.005:
        if len(rows) >= limit:
            raise NeverPaidOff(f"At this payment the mortgage would take more than {MAX_YEARS} years to pay off.")
        day = payment_date(start, frequency, k)
        interest = round(balance * rate, 2)
        paid = min(payment + (boost(day) if boost else 0.0), round(balance + interest, 2))
        balance = round(balance + interest - paid, 2)
        rows.append((day, paid, interest, paid - interest, balance))
        k += 1
    return rows


def _balance_on(rows, anchor_balance, day):
    """Balance after the last payment on or before `day`."""
    balance = anchor_balance
    for row in rows:
        if row[0] > day:
            break
        balance = row[4]
    return balance


def _month_end(d):
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _monthly_balances(rows, anchor_date, anchor_balance, months):
    """Balance at the end of each month in `months`, or None once it has been paid off."""
    out, i, balance, done = [], 0, anchor_balance, False
    for end in months:
        while i < len(rows) and rows[i][0] <= end:
            balance = rows[i][4]
            i += 1
        out.append(None if done else round(balance, 2))
        done = done or balance <= 0.005
    return out


def _month_grid(first, last):
    """The end of each month from `first` to `last`."""
    months, d = [], _month_end(first)
    while d <= _month_end(last):
        months.append(d)
        d = _month_end(add_months(d.replace(day=1), 1))
    return months


def _by_year(rows):
    """Principal and interest paid each calendar year."""
    by_year = {}
    for r in rows:
        y = by_year.setdefault(r[0].year, [0.0, 0.0])
        y[0] += r[3]
        y[1] += r[2]
    return [{"year": y, "principal": round(v[0], 2), "interest": round(v[1], 2)} for y, v in sorted(by_year.items())]


def _terms(m):
    """What the schedule of mortgage `m` is worked out from.

    The schedule starts from the original amount on the start date, or, when a balance from a
    statement is given, from that balance on its date. Without a set payment, the payment is
    the one that pays the starting balance off by the end of the amortization period."""
    frequency = m["frequency"]
    per_year = FREQUENCIES[frequency][1]
    start = date.fromisoformat(m["start_date"])
    if m["balance"] is not None and m["balance_date"]:
        anchor_date, anchor_balance = date.fromisoformat(m["balance_date"]), m["balance"]
    else:
        anchor_date, anchor_balance = start, m["principal"]
    amortization_end = add_months(start, m["amortization_months"])
    payment = m["payment"]
    if not payment:
        months_left = months_between(anchor_date, amortization_end)
        if months_left <= 0:
            raise NeverPaidOff("The amortization period has already ended. Enter the payment amount instead.")
        payment = regular_payment(anchor_balance, m["rate"], m["compounding"], months_left, frequency)
    return {"frequency": frequency, "per_year": per_year, "rate": periodic_rate(m["rate"], m["compounding"], per_year),
            "start": start, "anchor_date": anchor_date, "anchor_balance": anchor_balance,
            "amortization_end": amortization_end, "payment": payment, "extra": m["extra_payment"] or 0.0}


def _schedule(t, extra=True, boost=None):
    """The payments of a mortgage with terms `t` (see `_terms`), with or without its extra payment."""
    return amortize(t["anchor_balance"], t["start"], t["anchor_date"], t["frequency"], t["rate"],
                    t["payment"] + (t["extra"] if extra else 0.0), boost)


def project(m, today=None):
    """Payment, balances and payoff projection for mortgage `m`, as of `today`."""
    today = today or date.today()
    t = _terms(m)
    frequency, per_year, payment, extra = t["frequency"], t["per_year"], t["payment"], t["extra"]
    anchor_date, anchor_balance, amortization_end = t["anchor_date"], t["anchor_balance"], t["amortization_end"]
    rows = _schedule(t)

    balance_now = _balance_on(rows, anchor_balance, today)
    payoff = rows[-1][0] if rows else None
    future = [r for r in rows if r[0] > today]
    term_end = date.fromisoformat(m["term_end"]) if m["term_end"] else None

    # Without the extra payments, to show what they save.
    base_rows = _schedule(t, extra=False) if extra else None
    base_payoff = base_rows[-1][0] if base_rows else None

    # Chart: the balance at the end of each month, with and without the extra payments.
    months = _month_grid(anchor_date, max(payoff or anchor_date, base_payoff or anchor_date))

    return {
        "payment": payment,
        "extra_payment": extra,
        "payment_calculated": not m["payment"],
        "frequency_label": FREQUENCIES[frequency][0],
        "per_year": per_year,
        "monthly_cost": (payment + extra) * per_year / 12,
        "anchor_date": anchor_date.isoformat(),
        "anchor_balance": anchor_balance,
        "balance": balance_now,
        "paid_pct": 1 - balance_now / m["principal"] if m["principal"] else None,
        "payoff_date": payoff.isoformat() if payoff else None,
        "months_left": max(months_between(today, payoff), 0) if payoff and payoff > today else 0,
        "payments_left": len(future),
        "interest_left": sum(r[2] for r in future),
        "amortization_end": amortization_end.isoformat(),
        "term_end": term_end.isoformat() if term_end else None,
        "term_end_balance": _balance_on(rows, anchor_balance, term_end) if term_end else None,
        "base_payoff_date": base_payoff.isoformat() if base_payoff else None,
        "interest_saved": (sum(r[2] for r in base_rows if r[0] > today) - sum(r[2] for r in future)) if base_rows else None,
        "months_saved": months_between(payoff, base_payoff) if base_payoff and payoff else None,
        "chart": {
            "dates": [d.isoformat() for d in months],
            "balance": _monthly_balances(rows, anchor_date, anchor_balance, months),
            "base_balance": _monthly_balances(base_rows, anchor_date, anchor_balance, months) if base_rows else None,
            "years": _by_year(rows),
        },
    }


def project_continued(ms, today=None):
    """Payoff projection for mortgages `ms` paid together, with continued contributions: once one
    is paid off, everything that was being paid on it goes towards the remaining mortgage with
    the highest rate, until they are all paid off. Mortgages already paid off by `today` carry
    nothing over, so nothing changes before `today`.

    Each mortgage's balances are on the same months as its own projection's chart."""
    today = today or date.today()
    ms = {m["id"]: m for m in ms}
    terms = {i: _terms(m) for i, m in ms.items()}
    steps = {i: [] for i in ms}  # mortgage id -> [(after this day, monthly amount carried over to it)]

    def boost(i):
        if not steps[i]:
            return None
        per_year = terms[i]["per_year"]

        def extra(day):
            monthly = 0.0
            for after, amount in steps[i]:
                if day > after:
                    monthly = amount
            return round(monthly * 12 / per_year, 2)
        return extra

    base = {i: _schedule(t) for i, t in terms.items()}
    rows, left, carried = dict(base), set(ms), 0.0
    while left:
        for i in left:
            rows[i] = _schedule(terms[i], boost=boost(i))
        # The next one paid off: its payments, with what was carried over to it, move on to another.
        done = min(left, key=lambda i: (rows[i][-1][0] if rows[i] else date.min, i))
        left.remove(done)
        payoff = rows[done][-1][0] if rows[done] else None
        if not left or payoff is None or payoff <= today:
            continue
        t = terms[done]
        carried += (t["payment"] + t["extra"]) * t["per_year"] / 12
        target = max(left, key=lambda i: (ms[i]["rate"], -i))
        steps[target].append((payoff, carried))

    def interest_left(schedules):
        return sum(r[2] for s in schedules for r in s if r[0] > today)

    def payoff_of(schedules):
        days = [s[-1][0] for s in schedules if s]
        return max(days) if days else None

    out = []
    for i, t in terms.items():
        own_payoff = base[i][-1][0] if base[i] else t["anchor_date"]
        no_extra = _schedule(t, extra=False) if t["extra"] and base[i] else None
        months = _month_grid(t["anchor_date"], max(own_payoff, no_extra[-1][0] if no_extra else own_payoff))
        out.append({"id": i, "payoff_date": rows[i][-1][0].isoformat() if rows[i] else None,
                    "balance": _monthly_balances(rows[i], t["anchor_date"], t["anchor_balance"], months),
                    "years": _by_year(rows[i])})
    payoff, base_payoff = payoff_of(rows.values()), payoff_of(base.values())
    months_saved = months_between(payoff, base_payoff) if payoff and base_payoff else 0
    return {
        "mortgages": out,
        "payoff_date": payoff.isoformat() if payoff else None,
        "base_payoff_date": base_payoff.isoformat() if base_payoff else None,
        "months_saved": months_saved,
        "sooner": duration(months_saved),
        "interest_left": round(interest_left(rows.values()), 2),
        "interest_saved": round(interest_left(base.values()) - interest_left(rows.values()), 2),
    }


def duration(months):
    """'18 years 4 months'-style text."""
    years, months = divmod(max(months, 0), 12)
    parts = [f"{years} year{'s' if years != 1 else ''}"] if years else []
    if months or not years:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    return " ".join(parts)
