import re
from datetime import date

import pytest

from app import budget as bg
from conftest import login, login_as

TODAY = date(2026, 10, 21)  # a Wednesday
ANCHOR = date(2026, 1, 9)  # a pay day; so is Oct 16, 2026, 20 pay days later


def line(id=1, kind="expense", frequency="monthly", amount=100.0, start="2026-01-01", end=None, name="Rent"):
    return {"id": id, "kind": kind, "name": name, "frequency": frequency, "amount": amount,
            "start_date": start, "end_date": end}


# -- schedule ------------------------------------------------------------------------


def test_pay_days_cover_twelve_months_from_the_current_period():
    days = bg.pay_days(ANCHOR, TODAY)
    assert days[0] == date(2026, 10, 16)  # the last pay day on or before today
    assert days[-1] == date(2027, 10, 15)  # the last before Oct 16, 2027
    assert len(days) == 27
    assert all((b - a).days == 14 for a, b in zip(days, days[1:]))
    assert bg.pay_days(ANCHOR, date(2026, 10, 16))[0] == date(2026, 10, 16)  # today is pay day
    assert bg.pay_days(date(2027, 3, 5), TODAY)[0] == date(2026, 10, 16)  # any pay day works, even a future one


def test_add_months_keeps_the_day_or_uses_the_months_last():
    assert bg.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert bg.add_months(date(2026, 1, 31), 2) == date(2026, 3, 31)
    assert bg.add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_occurrences_by_frequency():
    start, end = date(2026, 10, 16), date(2026, 10, 30)
    assert bg.occurrences(line(frequency="monthly", start="2026-01-20"), start, end) == [date(2026, 10, 20)]
    assert bg.occurrences(line(frequency="monthly", start="2026-01-05"), start, end) == []
    assert bg.occurrences(line(frequency="pay", start=None), start, end) == [start]
    assert bg.occurrences(line(frequency="weekly", start="2026-10-01"), start, end) == [date(2026, 10, 22), date(2026, 10, 29)]
    assert bg.occurrences(line(frequency="quarterly", start="2026-04-25"), start, end) == [date(2026, 10, 25)]
    assert bg.occurrences(line(frequency="quarterly", start="2026-05-25"), start, end) == []
    assert bg.occurrences(line(frequency="yearly", start="2024-10-18"), start, end) == [date(2026, 10, 18)]
    assert bg.occurrences(line(frequency="once", start="2026-10-29"), start, end) == [date(2026, 10, 29)]
    assert bg.occurrences(line(frequency="varies", amount=None, start=None), start, end) == []


def test_occurrences_respect_the_first_and_last_dates():
    start, end = date(2026, 10, 16), date(2026, 10, 30)
    assert bg.occurrences(line(frequency="pay", start="2026-11-01"), start, end) == []
    assert bg.occurrences(line(frequency="weekly", start="2026-10-25"), start, end) == [date(2026, 10, 25)]
    assert bg.occurrences(line(frequency="monthly", start="2026-01-20", end="2026-10-19"), start, end) == []
    assert bg.occurrences(line(frequency="pay", start=None, end="2026-10-16"), start, end) == [start]


def test_describe():
    assert bg.describe(line(amount=1850, start="2026-01-01"), TODAY) == "$1,850.00 on the 1st of every month"
    assert bg.describe(line(frequency="pay", amount=2400, start=None), TODAY) == "$2,400.00 every pay day"
    assert bg.describe(line(frequency="weekly", amount=80, start="2026-10-23"), TODAY) == "$80.00 every Friday, from Oct 23, 2026"
    assert bg.describe(line(frequency="yearly", amount=120, start="2026-03-03", end="2030-03-03"), TODAY) == \
        "$120.00 every year on Mar 3, until Mar 3, 2030"
    assert bg.describe(line(frequency="once", amount=500, start="2026-12-20"), TODAY) == "$500.00 on Dec 20, 2026"
    assert bg.describe(line(frequency="monthly", amount=10, start="2026-01-22"), TODAY).endswith("on the 22nd of every month")
    assert bg.describe(line(frequency="varies", amount=None, start=None), TODAY) == "Amount entered for each pay day"


def test_plan_totals_and_entered_amounts():
    lines = [
        line(1, "revenue", "pay", 2400, None, name="Salary"),
        line(2, "expense", "monthly", 1850, "2026-01-01"),
        line(3, "expense", "varies", None, None, name="Groceries"),
    ]
    entries = {(3, "2026-10-16"): 300.0, (2, "2026-10-30"): 1900.0}
    plan = bg.plan(lines, entries, ANCHOR, TODAY)
    periods = [p["date"] for p in plan["periods"]]
    assert periods[:3] == ["2026-10-16", "2026-10-30", "2026-11-13"]
    assert plan["periods"][0]["end"] == "2026-10-29"
    salary, rent, groceries = plan["lines"]
    assert all(c["amount"] == 2400 for c in salary["cells"]) and salary["total"] == 2400 * 27
    # Rent on the 1st: none in the first period, Nov 1 in the second (changed to 1,900), Dec 1 in the fourth.
    assert [c["amount"] for c in rent["cells"][:4]] == [None, 1900.0, None, 1850]
    assert rent["cells"][1] == {"amount": 1900.0, "scheduled": 1850, "entered": True}
    assert groceries["cells"][0] == {"amount": 300.0, "scheduled": None, "entered": True}
    assert plan["totals"]["revenue"][0] == 2400 and plan["totals"]["expense"][:2] == [300, 1900]
    assert plan["totals"]["net"][:2] == [2100, 500]
    assert plan["totals"]["running"][:2] == [2100, 2600]
    assert plan["sums"]["net"] == pytest.approx(plan["sums"]["revenue"] - plan["sums"]["expense"])
    assert plan["sums"]["expense"] == pytest.approx(rent["total"] + groceries["total"])


# -- pages ----------------------------------------------------------------------------


def add_line(client, token, **fields):
    data = {"csrf_token": token, "kind": "expense", "name": "Rent", "frequency": "monthly", "amount": "1,850",
            "start_date": "2026-01-01", "end_date": "", **fields}
    return client.post("/budget/lines", data=data)


def set_pay_day(client, token, day="2026-10-16"):
    return client.post("/budget/pay-day", data={"csrf_token": token, "pay_day": day})


def test_budget_tab_asks_for_a_pay_day_first(client):
    assert client.get("/budget").status_code == 302  # sign-in required
    token = login(client)
    html = client.get("/budget").get_data(as_text=True)
    assert 'href="/budget" aria-current="page">Budget</a>' in html
    assert html.index(">Accounts</a>") < html.index(">Budget</a>")  # to the right of Accounts
    assert "When do you get paid?" in html and 'id="budget-table"' not in html
    assert client.get("/api/budget").status_code == 400

    assert set_pay_day(client, token, "not a date").status_code == 302
    assert client.get("/api/budget").status_code == 400
    set_pay_day(client, token)
    html = client.get("/budget").get_data(as_text=True)
    assert "When do you get paid?" not in html and 'id="add-line"' in html and 'value="2026-10-16"' in html


def test_budget_lines_and_entries(client):
    token = login(client)
    set_pay_day(client, token)
    assert add_line(client, token).headers["Location"] == "/budget#budget-plan"
    add_line(client, token, kind="revenue", name="Salary", frequency="pay", amount="2400", start_date="")
    add_line(client, token, name="Groceries", frequency="varies", amount="", start_date="2026-01-01")
    plan = client.get("/api/budget").get_json()
    assert [(l["name"], l["kind"]) for l in plan["lines"]] == [("Salary", "revenue"), ("Rent", "expense"), ("Groceries", "expense")]
    assert plan["lines"][1]["schedule"] == "$1,850.00 on the 1st of every month"
    assert 'id="budget-table"' in client.get("/budget").get_data(as_text=True)

    # Enter an amount for a pay day; a blank one removes it again.
    day = plan["periods"][1]["date"]
    entry = {"line_id": 3, "pay_day": day, "amount": "$320.50"}
    resp = client.post("/api/budget/entries", json=entry, headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert resp.get_json()["lines"][2]["cells"][1] == {"amount": 320.5, "scheduled": None, "entered": True}
    client.post("/api/budget/entries", json={**entry, "amount": "400"}, headers={"X-CSRF-Token": token})
    assert client.get("/api/budget").get_json()["lines"][2]["cells"][1]["amount"] == 400
    resp = client.post("/api/budget/entries", json={**entry, "amount": ""}, headers={"X-CSRF-Token": token})
    assert resp.get_json()["lines"][2]["cells"][1]["amount"] is None

    # Bad requests: not a pay day, not an amount, someone else's line, no CSRF token.
    bad = [({**entry, "pay_day": "2026-10-17"}, 400), ({**entry, "amount": "lots"}, 400), ({**entry, "line_id": 99}, 404)]
    for body, status in bad:
        assert client.post("/api/budget/entries", json=body, headers={"X-CSRF-Token": token}).status_code == status
    assert client.post("/api/budget/entries", json=entry).status_code == 400

    # Edit (Rent was added first): the form is filled in; saving changes the line.
    html = client.get("/budget/lines/1/edit").get_data(as_text=True)
    assert 'value="1850"' in html and 'value="2026-01-01"' in html
    client.post("/budget/lines/1/edit", data={"csrf_token": token, "kind": "expense", "name": "Rent", "frequency": "monthly",
                                              "amount": "1900", "start_date": "2026-01-01", "end_date": ""})
    assert client.get("/api/budget").get_json()["lines"][1]["schedule"].startswith("$1,900.00")

    # Delete: the line and its entries go.
    client.post("/api/budget/entries", json=entry, headers={"X-CSRF-Token": token})
    assert client.post("/budget/lines/3/delete", data={"csrf_token": token}).status_code == 302
    assert [l["name"] for l in client.get("/api/budget").get_json()["lines"]] == ["Salary", "Rent"]
    assert client.get("/budget/lines/3/edit").status_code == 404


@pytest.mark.parametrize("fields, error", [
    ({"name": ""}, "Enter a name"),
    ({"amount": ""}, "Enter the amount each time"),
    ({"amount": "-5"}, "greater than zero"),
    ({"amount": "abc"}, "must be a number"),
    ({"start_date": ""}, "Enter the first date"),
    ({"frequency": "once", "start_date": ""}, "Enter the date it is due."),
    ({"end_date": "2025-01-01"}, "can&#39;t be before the first date"),
    ({"kind": "gift"}, "revenue or an expense"),
    ({"frequency": "daily"}, "how often"),
])
def test_budget_line_form_errors(client, fields, error):
    token = login(client)
    set_pay_day(client, token)
    resp = add_line(client, token, **fields)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert error in html and "data-errors" in html


def test_budget_is_private(app, client):
    token = login(client)
    set_pay_day(client, token)
    add_line(client, token)
    other = login_as(app, client, "other-sub", "other@example.com")
    assert client.get("/budget/lines/1/edit").status_code == 404
    assert client.post("/budget/lines/1/delete", data={"csrf_token": other}).status_code == 404
    set_pay_day(client, other)
    assert client.get("/api/budget").get_json()["lines"] == []
    resp = client.post("/api/budget/entries", json={"line_id": 1, "pay_day": "2026-10-16", "amount": "5"},
                       headers={"X-CSRF-Token": other})
    assert resp.status_code == 404


def test_add_revenue_and_expense_buttons_pick_the_type(client):
    token = login(client)
    set_pay_day(client, token)
    html = client.get("/budget?kind=revenue").get_data(as_text=True)
    assert re.search(r'<option value="revenue"\s+selected>', html)
    html = client.get("/budget").get_data(as_text=True)
    assert re.search(r'<option value="expense"\s+selected>', html)
