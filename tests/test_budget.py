import re
from datetime import date

import pytest

from app import budget as bg
from conftest import login, login_as

TODAY = date(2026, 10, 21)  # a Wednesday
ANCHOR = date(2026, 1, 9)  # a pay day; so is Oct 16, 2026, 20 pay days later


def line(id=1, kind="expense", frequency="monthly", amount=100.0, start="2026-01-01", end=None, name="Rent", group=""):
    return {"id": id, "kind": kind, "name": name, "frequency": frequency, "amount": amount,
            "start_date": start, "end_date": end, "group_name": group}


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
    # every 2 weeks from its own date, not necessarily a pay day
    assert bg.occurrences(line(frequency="biweekly", start="2026-10-01"), start, end) == [date(2026, 10, 29)]
    assert bg.occurrences(line(frequency="biweekly", start="2026-10-08"), start, end) == [date(2026, 10, 22)]
    assert bg.occurrences(line(frequency="biweekly", start="2026-10-20"), start, end) == [date(2026, 10, 20)]
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
    assert bg.describe(line(frequency="biweekly", amount=45, start="2026-10-08"), TODAY) == "$45.00 every other Thursday"
    assert bg.describe(line(frequency="yearly", amount=120, start="2026-03-03", end="2030-03-03"), TODAY) == \
        "$120.00 every year on Mar 3, until Mar 3, 2030"
    assert bg.describe(line(frequency="once", amount=500, start="2026-12-20"), TODAY) == "$500.00 on Dec 20, 2026"
    assert bg.describe(line(frequency="monthly", amount=10, start="2026-01-22"), TODAY).endswith("on the 22nd of every month")
    assert bg.describe(line(frequency="varies", amount=None, start=None), TODAY) == "Amount entered for each pay day"


def test_plan_totals_and_entered_amounts():
    lines = [
        line(4, "cash", "pay", 500, None, name="Cash on hand"),
        line(1, "revenue", "pay", 2400, None, name="Salary"),
        line(2, "expense", "monthly", 1850, "2026-01-01"),
        line(3, "expense", "varies", None, None, name="Groceries"),
    ]
    entries = {(3, "2026-10-16"): 300.0, (2, "2026-10-30"): 1900.0}
    plan = bg.plan(lines, entries, ANCHOR, TODAY)
    periods = [p["date"] for p in plan["periods"]]
    assert periods[:3] == ["2026-10-16", "2026-10-30", "2026-11-13"]
    assert plan["periods"][0]["end"] == "2026-10-29"
    cash, salary, rent, groceries = plan["lines"]
    assert cash["total"] == 500 * 27
    assert plan["totals"]["cash"] == [500] * 27
    assert all(c["amount"] == 2400 for c in salary["cells"]) and salary["total"] == 2400 * 27
    # Rent on the 1st: none in the first period, Nov 1 in the second (changed to 1,900), Dec 1 in the fourth.
    assert [c["amount"] for c in rent["cells"][:4]] == [None, 1900.0, None, 1850]
    assert rent["cells"][1] == {"amount": 1900.0, "scheduled": 1850, "entered": True}
    assert groceries["cells"][0] == {"amount": 300.0, "scheduled": None, "entered": True}
    assert plan["totals"]["revenue"][0] == 2400 and plan["totals"]["expense"][:2] == [300, 1900]
    assert plan["totals"]["net"][:2] == [2100, 500]
    assert plan["totals"]["running"][:2] == [2600, 3600]  # cash plus what's left over
    assert plan["sums"]["net"] == pytest.approx(plan["sums"]["revenue"] - plan["sums"]["expense"])
    assert plan["sums"]["expense"] == pytest.approx(rent["total"] + groceries["total"])
    assert plan["sums"]["cash"] == cash["total"]


def test_expense_groups_are_subtotalled_with_ungrouped_expenses_last():
    lines = [
        line(1, "revenue", "pay", 2400, None, name="Salary"),
        line(2, "expense", "monthly", 1850, "2026-01-01", group="Housing"),
        line(3, "expense", "pay", 50, None, name="Coffee"),
        line(4, "expense", "monthly", 300, "2026-01-20", name="Income tax", group="Taxes"),
        line(5, "expense", "monthly", 120, "2026-01-01", name="Home insurance", group="Housing"),
        line(6, "expense", "yearly", 3000, "2026-11-15", name="Property tax", group="housing tax"),
    ]
    plan = bg.plan(lines, {}, ANCHOR, TODAY)
    assert [l["group"] for l in plan["lines"]] == ["", "Housing", "", "Taxes", "Housing", "housing tax"]
    groups = plan["groups"]
    assert [(g["name"], g["named"], g["lines"]) for g in groups] == [
        ("Housing", True, [2, 5]), ("housing tax", True, [6]), ("Taxes", True, [4]), ("Other expenses", False, [3])]
    housing = groups[0]
    assert housing["totals"][:2] == [0, 1970]  # Nov 1: rent and insurance
    assert housing["total"] == pytest.approx(sum(plan["lines"][i]["total"] for i in (1, 4)))
    assert groups[1]["totals"][2] == 3000  # Nov 15 falls in the Nov 13 pay period
    # the groups add up to all the expenses
    assert sum(g["total"] for g in groups) == pytest.approx(plan["sums"]["expense"])
    # without any group, expenses are listed as before
    assert bg.plan([line(1), line(2, name="Phone")], {}, ANCHOR, TODAY)["groups"] == []


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
    add_line(client, token, kind="cash", name="Cash on hand", frequency="pay", amount="500", start_date="")
    plan = client.get("/api/budget").get_json()
    assert [(l["name"], l["kind"]) for l in plan["lines"]] == [
        ("Cash on hand", "cash"), ("Salary", "revenue"), ("Rent", "expense"), ("Groceries", "expense")]
    assert plan["lines"][2]["schedule"] == "$1,850.00 on the 1st of every month"
    assert 'id="budget-table"' in client.get("/budget").get_data(as_text=True)
    html = client.get("/budget").get_data(as_text=True)
    assert 'href="/budget?kind=cash#add-line">Add cash</a>' in html
    assert plan["sums"]["cash"] == 500 * len(plan["periods"])
    assert plan["sums"]["net"] == plan["sums"]["revenue"] - plan["sums"]["expense"]

    # Enter an amount for a pay day; a blank one removes it again.
    day = plan["periods"][1]["date"]
    entry = {"line_id": 3, "pay_day": day, "amount": "$320.50"}
    resp = client.post("/api/budget/entries", json=entry, headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert resp.get_json()["lines"][3]["cells"][1] == {"amount": 320.5, "scheduled": None, "entered": True}
    client.post("/api/budget/entries", json={**entry, "amount": "400"}, headers={"X-CSRF-Token": token})
    assert client.get("/api/budget").get_json()["lines"][3]["cells"][1]["amount"] == 400
    resp = client.post("/api/budget/entries", json={**entry, "amount": ""}, headers={"X-CSRF-Token": token})
    assert resp.get_json()["lines"][3]["cells"][1]["amount"] is None

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
    assert client.get("/api/budget").get_json()["lines"][2]["schedule"].startswith("$1,900.00")

    # Delete: the line and its entries go.
    client.post("/api/budget/entries", json=entry, headers={"X-CSRF-Token": token})
    assert client.post("/budget/lines/3/delete", data={"csrf_token": token}).status_code == 302
    assert [l["name"] for l in client.get("/api/budget").get_json()["lines"]] == ["Cash on hand", "Salary", "Rent"]
    assert client.get("/budget/lines/3/edit").status_code == 404


def test_expense_groups_from_the_form(client):
    token = login(client)
    set_pay_day(client, token)
    html = client.get("/budget").get_data(as_text=True)
    assert '<datalist id="budget-groups">' in html and '<option value="Housing">' in html  # suggestions to start
    add_line(client, token, group_name="  Housing ")
    add_line(client, token, name="Home insurance", amount="120", group_name="housing")  # joins Housing
    add_line(client, token, kind="revenue", name="Salary", frequency="pay", start_date="", group_name="Housing")
    add_line(client, token, name="Income tax", group_name="Taxes")
    plan = client.get("/api/budget").get_json()
    assert {l["name"]: l["group"] for l in plan["lines"]} == {
        "Rent": "Housing", "Home insurance": "Housing", "Salary": "", "Income tax": "Taxes"}  # revenues have no group
    assert [g["name"] for g in plan["groups"]] == ["Housing", "Taxes"]
    html = client.get("/budget/lines/1/edit").get_data(as_text=True)
    assert 'name="group_name" value="Housing"' in html
    client.post("/budget/lines/1/edit", data={"csrf_token": token, "kind": "expense", "name": "Rent", "group_name": "",
                                              "frequency": "monthly", "amount": "1850", "start_date": "2026-01-01"})
    assert [g["name"] for g in client.get("/api/budget").get_json()["groups"]] == ["Housing", "Taxes", "Other expenses"]


@pytest.mark.parametrize("fields, error", [
    ({"name": ""}, "Enter a name"),
    ({"amount": ""}, "Enter the amount each time"),
    ({"amount": "-5"}, "greater than zero"),
    ({"amount": "abc"}, "must be a number"),
    ({"start_date": ""}, "Enter the first date"),
    ({"frequency": "once", "start_date": ""}, "Enter the date it is due."),
    ({"end_date": "2025-01-01"}, "can&#39;t be before the first date"),
    ({"kind": "gift"}, "cash, a revenue or an expense"),
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
    html = client.get("/budget?kind=cash").get_data(as_text=True)
    assert re.search(r'<option value="cash"\s+selected>', html)


def test_typing_cash_creates_the_cash_line(client):
    token = login(client)
    set_pay_day(client, token)
    assert 'id="budget-table"' in client.get("/budget").get_data(as_text=True)  # a row to type cash in, even with no lines
    plan = client.get("/api/budget").get_json()
    day, headers = plan["periods"][1]["date"], {"X-CSRF-Token": token}

    # A blank amount with no cash line changes nothing.
    resp = client.post("/api/budget/entries", json={"kind": "cash", "pay_day": day, "amount": ""}, headers=headers)
    assert resp.status_code == 200 and resp.get_json()["lines"] == []

    # The first amount creates a "Cash" line that varies; the next ones go on that same line.
    resp = client.post("/api/budget/entries", json={"kind": "cash", "pay_day": day, "amount": "1,200"}, headers=headers)
    (cash,) = resp.get_json()["lines"]
    assert (cash["name"], cash["kind"], cash["frequency"]) == ("Cash", "cash", "varies")
    assert cash["cells"][1]["amount"] == 1200 and resp.get_json()["sums"]["cash"] == 1200
    plan = client.post("/api/budget/entries", json={"kind": "cash", "pay_day": plan["periods"][2]["date"], "amount": "900"},
                       headers=headers).get_json()
    assert [l["id"] for l in plan["lines"]] == [cash["id"]] and plan["sums"]["cash"] == 2100
    assert plan["sums"]["net"] == 0  # cash doesn't count as revenue

    resp = client.post("/api/budget/entries", json={"kind": "cash", "pay_day": day, "amount": "lots"}, headers=headers)
    assert resp.status_code == 400


def test_clearing_a_recurring_cell_leaves_nothing_that_pay_day(client):
    token = login(client)
    set_pay_day(client, token)
    add_line(client, token, kind="revenue", name="Salary", frequency="pay", amount="2400", start_date="")
    plan = client.get("/api/budget").get_json()
    day, headers = plan["periods"][1]["date"], {"X-CSRF-Token": token}
    post = lambda amount: client.post("/api/budget/entries", json={"line_id": 1, "pay_day": day, "amount": amount},
                                      headers=headers).get_json()

    # Cleared: 0 for that pay day, not the fixed amount.
    plan = post("")
    assert plan["lines"][0]["cells"][1] == {"amount": 0, "scheduled": 2400, "entered": True}
    assert plan["totals"]["revenue"][1] == 0 and plan["lines"][0]["cells"][2]["amount"] == 2400
    assert post("")["lines"][0]["cells"][1]["amount"] == 0  # clearing again keeps it cleared

    # A different amount replaces it; the fixed amount again goes back to the schedule.
    assert post("2,000")["lines"][0]["cells"][1] == {"amount": 2000, "scheduled": 2400, "entered": True}
    assert post("2400.00")["lines"][0]["cells"][1] == {"amount": 2400, "scheduled": 2400, "entered": False}

    # A pay day with nothing scheduled: clearing an entered amount removes it.
    add_line(client, token, name="Groceries", frequency="varies", amount="", start_date="")
    entry = {"line_id": 2, "pay_day": day}
    client.post("/api/budget/entries", json={**entry, "amount": "300"}, headers=headers)
    cell = client.post("/api/budget/entries", json={**entry, "amount": ""}, headers=headers).get_json()["lines"][1]["cells"][1]
    assert cell == {"amount": None, "scheduled": None, "entered": False}
