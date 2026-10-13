from datetime import date

import pytest

from app import mortgage as mg


def mortgage(**changes):
    m = {"principal": 500000.0, "start_date": "2022-06-01", "rate": 5.0, "compounding": "semi-annual",
         "amortization_months": 300, "frequency": "monthly", "payment": None, "extra_payment": 0.0,
         "term_end": None, "balance": None, "balance_date": None}
    m.update(changes)
    return m


def test_canadian_monthly_payment():
    # $500,000 at 5% compounded semi-annually over 25 years: $2,908.02 a month (rounded up to the cent).
    assert mg.regular_payment(500000, 5, "semi-annual", 300, "monthly") == 2908.03
    # Compounded monthly, as with US mortgages and some variable rates, it costs a little more.
    assert mg.regular_payment(500000, 5, "monthly", 300, "monthly") == 2922.96  # $2,922.953...


def test_accelerated_payments_are_the_monthly_payment_split():
    monthly = mg.regular_payment(500000, 5, "semi-annual", 300, "monthly")
    assert mg.regular_payment(500000, 5, "semi-annual", 300, "accelerated-bi-weekly") == pytest.approx(monthly / 2, abs=0.01)
    assert mg.regular_payment(500000, 5, "semi-annual", 300, "accelerated-weekly") == pytest.approx(monthly / 4, abs=0.01)


def test_zero_rate():
    assert mg.regular_payment(120000, 0, "semi-annual", 120, "monthly") == 1000.0
    p = mg.project(mortgage(principal=120000.0, rate=0.0, amortization_months=120), today=date(2022, 6, 1))
    assert p["payoff_date"] == "2032-06-01"
    assert p["interest_left"] == 0


def test_payment_dates():
    assert mg.payment_date(date(2024, 1, 31), "monthly", 1) == date(2024, 2, 29)
    assert mg.payment_date(date(2024, 1, 1), "semi-monthly", 1) == date(2024, 1, 16)
    assert mg.payment_date(date(2024, 1, 1), "semi-monthly", 2) == date(2024, 2, 1)
    assert mg.payment_date(date(2024, 1, 1), "bi-weekly", 2) == date(2024, 1, 29)
    assert mg.payment_date(date(2024, 1, 1), "accelerated-weekly", 1) == date(2024, 1, 8)


def test_paid_off_at_the_end_of_the_amortization():
    p = mg.project(mortgage(), today=date(2026, 10, 13))
    assert p["payoff_date"] == "2047-06-01"
    assert p["payments_left"] == 248  # Nov 2026 to Jun 2047
    assert p["months_left"] == 247
    assert 450000 < p["balance"] < 452000
    assert p["paid_pct"] == pytest.approx(1 - p["balance"] / 500000)
    # The chart starts at the original amount and ends at zero.
    chart = p["chart"]
    assert chart["dates"][0] == "2022-06-30" and chart["balance"][0] == 500000
    assert chart["balance"][-1] == 0
    assert chart["base_balance"] is None
    years = {y["year"]: y for y in chart["years"]}
    assert sum(y["principal"] for y in chart["years"]) == pytest.approx(500000, abs=0.05)
    assert years[2023]["interest"] > years[2046]["interest"]


def test_before_the_first_payment_nothing_is_paid():
    p = mg.project(mortgage(start_date="2026-10-01"), today=date(2026, 10, 13))
    assert p["balance"] == 500000
    assert p["paid_pct"] == 0
    assert p["payments_left"] == 300


def test_extra_payments_pay_it_off_sooner():
    p = mg.project(mortgage(extra_payment=500.0), today=date(2026, 10, 13))
    assert p["payment"] == 2908.03 and p["extra_payment"] == 500
    assert p["payoff_date"] < "2047-06-01"
    assert p["base_payoff_date"] == "2047-06-01"
    assert p["months_saved"] > 48
    assert p["interest_saved"] > 50000
    assert p["chart"]["base_balance"][-1] == 0
    assert p["monthly_cost"] == pytest.approx(3408.03)


def test_term_end_balance():
    p = mg.project(mortgage(term_end="2027-06-01"), today=date(2026, 10, 13))
    assert p["term_end"] == "2027-06-01"
    assert 440000 < p["term_end_balance"] < p["balance"]


def test_statement_balance_restarts_the_projection():
    # After a lump sum the balance is lower than the schedule says; a blank payment is
    # recalculated over what is left of the amortization, so the payoff date stays the same.
    p = mg.project(mortgage(balance=400000.0, balance_date="2025-06-01"), today=date(2026, 10, 13))
    assert p["anchor_balance"] == 400000 and p["anchor_date"] == "2025-06-01"
    assert p["payment"] < 2908.03
    assert p["payoff_date"] == "2047-06-01"
    assert p["chart"]["dates"][0] == "2025-06-30"
    # With the payment kept as it was, the lower balance is paid off sooner.
    p = mg.project(mortgage(balance=400000.0, balance_date="2025-06-01", payment=2908.03), today=date(2026, 10, 13))
    assert p["payoff_date"] < "2045-01-01"


def test_payment_too_small():
    with pytest.raises(mg.NeverPaidOff):
        mg.project(mortgage(payment=1500.0))
    with pytest.raises(mg.NeverPaidOff):  # the amortization has run out and no payment is set
        mg.project(mortgage(start_date="1990-01-01", balance=1000.0, balance_date="2020-01-01"))


def test_duration():
    assert mg.duration(220) == "18 years 4 months"
    assert mg.duration(12) == "1 year"
    assert mg.duration(1) == "1 month"
    assert mg.duration(0) == "0 months"
