import io
import re

from conftest import SAMPLE_CSV, csrf, login, login_as


def upload(client, token, data=None, name="activity.csv"):
    data = data if data is not None else SAMPLE_CSV.read_bytes()
    return client.post("/import", data={"csrf_token": token, "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def test_pages_require_sign_in(client):
    assert client.get("/").status_code == 302
    assert client.get("/transactions").headers["Location"].startswith("/login")
    assert client.get("/api/portfolio").status_code == 401


def test_dev_login_can_be_disabled(app, client):
    app.config["DEV_LOGIN"] = False
    assert "local dev user" not in client.get("/login").get_data(as_text=True)
    with client.session_transaction() as s:
        s["csrf"] = "token"
    assert client.post("/login/dev", data={"csrf_token": "token"}).status_code == 404


def test_post_without_csrf_token_is_rejected(client):
    login(client)
    assert client.post("/accounts", data={"number": "1"}).status_code == 400


def test_import_is_idempotent(client):
    token = login(client)
    html = upload(client, token).get_data(as_text=True)
    assert "<strong>20</strong> imported" in html
    assert "<strong>2</strong> new accounts" in html
    html = upload(client, token).get_data(as_text=True)
    assert "<strong>0</strong> imported" in html
    assert "<strong>20</strong> already imported" in html


def test_portfolio_api(client):
    token = login(client)
    upload(client, token)
    data = client.get("/api/portfolio").get_json()
    holdings = {h["symbol"]: h for h in data["holdings"]}
    assert set(holdings) == {"VFV.TO", "XEQT", "AAPL", "MSFT", "SHOP"}
    assert holdings["VFV.TO"]["quantity"] == 70
    assert holdings["AAPL"]["quantity"] == 10
    assert holdings["AAPL"]["market_value"] == 10 * 250 * 1.40
    cash = {(c["account"].split(" · ")[1], c["currency"]): round(c["amount"], 2) for c in data["cash"]}
    assert cash == {("51234567", "CAD"): 2796.07, ("51234568", "CAD"): 1200.0, ("51234568", "USD"): 1280.0}
    assert not data["holdings_only"]
    assert round(data["totals"]["net_contributions"], 2) == 32000.0


def test_account_filter_and_history(client):
    token = login(client)
    upload(client, token)
    accounts = client.get("/portfolios").get_data(as_text=True)
    assert "51234567" in accounts and "51234568" in accounts
    # account ids are 1 and 2 in a fresh database
    rrsp = client.get("/api/portfolio?account=2").get_json()
    assert {h["symbol"] for h in rrsp["holdings"]} == {"AAPL", "MSFT"}
    hist = client.get("/api/history?account=1").get_json()
    assert hist["points"] and hist["points"][0]["date"] == "2024-01-05"


def test_pages_render(client):
    token = login(client)
    upload(client, token)
    for path in ("/", "/portfolios?symbol=AAPL", "/portfolios", "/position/VFV.TO",
                 "/transactions/new", "/accounts/1/edit"):
        assert client.get(path).status_code == 200, path
    detail = client.get("/api/position/AAPL").get_json()
    assert detail["holding"]["quantity"] == 10
    assert [t["kind"] for t in detail["transactions"]] == ["Sell", "Dividend", "Buy"]
    export = client.get("/export.csv").get_data(as_text=True)
    assert export.startswith("Transaction Date,Action,Symbol")
    assert len(export.strip().splitlines()) == 21


def test_add_edit_delete_transaction(client):
    token = login(client)
    client.post("/accounts", data={"csrf_token": token, "number": "777", "type": "TFSA"})
    resp = client.post("/transactions/new", data={
        "csrf_token": token, "kind": "BUY", "date": "2025-05-01", "account_id": "1", "currency": "cad",
        "symbol": "vfv.to", "quantity": "10", "price": "100", "costs": "5", "net_amount": "",
    })
    assert resp.status_code == 302
    data = client.get("/api/portfolio").get_json()
    assert data["holdings"][0]["book_cost"] == 1000  # the 5 commission is not book cost

    resp = client.post("/transactions/1/edit", data={
        "csrf_token": token, "kind": "BUY", "date": "2025-05-01", "account_id": "1", "currency": "CAD",
        "symbol": "VFV.TO", "quantity": "20", "price": "100", "costs": "0", "net_amount": "",
    })
    assert resp.status_code == 302
    assert client.get("/api/portfolio").get_json()["holdings"][0]["quantity"] == 20

    client.post("/transactions/1/delete", data={"csrf_token": token})
    assert client.get("/api/portfolio").get_json()["holdings"] == []


def test_invalid_transaction_shows_errors(client):
    token = login(client)
    client.post("/accounts", data={"csrf_token": token, "number": "777"})
    html = client.post("/transactions/new", data={
        "csrf_token": token, "kind": "BUY", "date": "", "account_id": "1", "currency": "CAD",
        "symbol": "", "quantity": "0", "price": "abc", "costs": "", "net_amount": "",
    }).get_data(as_text=True)
    assert "Enter a valid date." in html
    assert "Enter the symbol that was traded." in html
    assert "Price must be a number." in html


def test_users_cannot_see_each_others_data(app, client):
    token = login(client)
    upload(client, token)
    login_as(app, client, "other-user", "other@example.com")
    assert client.get("/api/portfolio").get_json()["holdings"] == []
    assert client.get("/transactions/1/edit").status_code == 404
    assert client.get("/api/portfolio?account=1").status_code == 404
    assert client.get("/position/VFV.TO").status_code == 404
    assert client.post("/transactions/1/delete", data={"csrf_token": "token"}).status_code == 302
    # still there for the owner
    login(client)
    assert len(client.get("/api/portfolio").get_json()["holdings"]) == 5


def test_portfolios(app, client):
    token = login(client)
    upload(client, token)
    assert client.get("/portfolios").status_code == 200
    # account ids are 1 and 2 in a fresh database; 99 is not the user's and is ignored
    resp = client.post("/portfolios", data={"csrf_token": token, "name": "Retirement", "account_ids": ["1", "2", "99"]})
    assert resp.status_code == 302
    html = client.get("/portfolios").get_data(as_text=True)
    assert "Retirement" in html and "51234567" in html and "51234568" in html
    dup = client.post("/portfolios", data={"csrf_token": token, "name": "Retirement"}).get_data(as_text=True)
    assert "already have a portfolio named Retirement" in dup

    assert client.get("/portfolios/1/edit").status_code == 200
    html = client.get("/?portfolio=1").get_data(as_text=True)
    assert '<option value="portfolio:1" selected>Retirement</option>' in html
    assert "portfolio=1" in html  # API URLs carry the selection
    both = client.get("/api/portfolio?portfolio=1").get_json()
    assert {h["symbol"] for h in both["holdings"]} == {"VFV.TO", "XEQT", "AAPL", "MSFT", "SHOP"}
    assert client.get("/api/history?portfolio=1").get_json()["points"]
    assert client.get("/position/AAPL?portfolio=1").status_code == 200
    assert client.get("/api/portfolio?portfolio=x").status_code == 400
    assert client.get("/api/portfolio?portfolio=99").status_code == 404
    client.post("/portfolios/1/edit", data={"csrf_token": token, "name": "RRSP only", "account_ids": ["2"]})
    from app.db import connect
    conn = connect(app.config["DATABASE"])
    assert [r[0] for r in conn.execute("SELECT account_id FROM portfolio_accounts")] == [2]
    rrsp = client.get("/api/portfolio?portfolio=1").get_json()
    assert {h["symbol"] for h in rrsp["holdings"]} == {"AAPL", "MSFT"}
    assert {t["account"] for t in client.get("/api/position/AAPL?portfolio=1").get_json()["transactions"]} == {"RRSP · 51234568"}
    client.post("/portfolios/1/edit", data={"csrf_token": token, "name": "Empty"})
    empty = client.get("/api/portfolio?portfolio=1").get_json()
    assert empty["holdings"] == [] and empty["cash"] == []

    other = login_as(app, client, "other", "other@example.com")
    assert client.get("/portfolios/1/edit").status_code == 404
    assert client.post("/portfolios/1/delete", data={"csrf_token": other}).status_code == 404

    token = login(client)
    client.post("/portfolios/1/delete", data={"csrf_token": token})
    assert conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2
    conn.close()


def test_theme_setting(app, client):
    token = login(client)
    assert 'data-theme="white"' in client.get("/portfolios").get_data(as_text=True)
    resp = client.post("/settings/theme", data={"csrf_token": token, "theme": "dark-grey", "next": "/portfolios?x=1"})
    assert resp.headers["Location"] == "/portfolios?x=1"
    html = client.get("/portfolios").get_data(as_text=True)
    assert 'data-theme="dark-grey"' in html and 'value="dark-grey" class="theme-option" title="Dark grey" aria-current="true"' in html
    assert 'title="Theme: Dark grey"' in html and "#26282c" in html  # the picker shows the theme's colours
    assert client.post("/settings/theme", data={"csrf_token": token, "theme": "neon"}).status_code == 400
    for evil in ("https://evil.example", "//evil.example", "/\\evil.example"):
        resp = client.post("/settings/theme", data={"csrf_token": token, "theme": "white", "next": evil})
        assert resp.headers["Location"] == "/"


def test_migration_adds_theme_to_existing_users(tmp_path):
    from app.db import connect, init_db

    path = str(tmp_path / "old.db")
    conn = connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, google_sub TEXT, email TEXT, name TEXT, picture TEXT)")
    conn.execute("INSERT INTO users (google_sub, email) VALUES ('s', 'a@b.c')")
    conn.commit()
    conn.close()
    init_db(path)
    conn = connect(path)
    assert conn.execute("SELECT theme FROM users").fetchone()[0] == "white"
    assert conn.execute("SELECT default_scope FROM users").fetchone()[0] == ""
    conn.close()


def test_migration_turns_in_kind_deposits_and_withdrawals_into_transfers(tmp_path):
    from app.db import connect, init_db

    path = str(tmp_path / "old.db")
    init_db(path)
    conn = connect(path)
    conn.execute("INSERT INTO users (id, google_sub, email) VALUES (1, 's', 'a@b.c')")
    conn.execute("INSERT INTO accounts (id, user_id, number, type) VALUES (1, 1, '1', 'TFSA')")
    for kind, symbol, qty, net in (("WITHDRAWAL", "V", -5, 0), ("WITHDRAWAL", "", 0, -100),
                                   ("DEPOSIT", "V", 5, 0), ("DEPOSIT", "", 0, 100)):
        conn.execute("INSERT INTO transactions (user_id, account_id, date, action, kind, symbol, quantity, "
                     "net_amount, currency) VALUES (1, 1, '2021-11-23', 'WDR', ?, ?, ?, ?, 'USD')",
                     (kind, symbol, qty, net))
    conn.commit()
    conn.close()
    init_db(path)
    conn = connect(path)
    kinds = [r[0] for r in conn.execute("SELECT kind FROM transactions ORDER BY id")]
    conn.close()
    assert kinds == ["TRANSFER", "WITHDRAWAL", "TRANSFER", "DEPOSIT"]


def test_dismissed_warnings_stay_hidden(client):
    token = login(client)
    client.post("/accounts", data={"csrf_token": token, "number": "777", "type": "TFSA"})
    client.post("/transactions/new", data={  # no live quote for this symbol, so the API warns
        "csrf_token": token, "kind": "BUY", "date": "2025-05-01", "account_id": "1", "currency": "CAD",
        "symbol": "PRIVATE", "quantity": "10", "price": "12", "costs": "0", "net_amount": "",
    })
    warning = "No live price for PRIVATE; valued at its last trade price."
    assert warning in client.get("/api/portfolio").get_json()["warnings"]

    assert client.post("/api/warnings/dismiss", json={"message": warning}).status_code == 400  # no CSRF token
    resp = client.post("/api/warnings/dismiss", json={"message": warning}, headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert warning not in client.get("/api/portfolio").get_json()["warnings"]
    assert warning not in client.get("/api/position/PRIVATE").get_json()["warnings"]
    assert 'name="csrf-token"' in client.get("/").get_data(as_text=True)


def test_default_view(app, client):
    token = login(client)
    upload(client, token)
    client.post("/portfolios", data={"csrf_token": token, "name": "Retirement", "account_ids": ["1", "2"]})

    html = client.get("/").get_data(as_text=True)  # no default yet: All accounts
    assert "All accounts (default)" in html and ">Default</span>" in html

    resp = client.post("/settings/default-view", data={"csrf_token": token, "scope": "portfolio:1", "next": "/?portfolio=1"})
    assert resp.status_code == 302 and resp.headers["Location"] == "/?portfolio=1"
    resp = client.get("/")
    assert resp.status_code == 302 and resp.headers["Location"] == "/?portfolio=1"
    html = client.get("/?portfolio=1").get_data(as_text=True)
    assert "Retirement (default)</option>" in html and ">Default</span>" in html

    html = client.get("/?all=1").get_data(as_text=True)  # All accounts can still be picked
    assert "Set as default" in html and "all=1" in html  # position links keep the choice
    assert "all=1" in client.get("/position/AAPL?all=1").get_data(as_text=True)  # back link too

    assert client.post("/settings/default-view", data={"csrf_token": token, "scope": "bogus"}).status_code == 400
    assert client.post("/settings/default-view", data={"csrf_token": token, "scope": "account:99"}).status_code == 404

    client.post("/portfolios/1/delete", data={"csrf_token": token})
    assert client.get("/").status_code == 200  # default deleted: back to All accounts

    client.post("/settings/default-view", data={"csrf_token": token, "scope": "account:2"})
    assert client.get("/").headers["Location"] == "/?account=2"
    client.post("/settings/default-view", data={"csrf_token": token, "scope": ""})
    assert client.get("/").status_code == 200


def test_performance_tab(app, client):
    token = login(client)
    html = client.get("/performance").get_data(as_text=True)
    assert "No activity yet" in html  # empty state before any transactions
    nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
    assert nav.index(">Dashboard<") < nav.index(">Performance<") < nav.index(">Portfolio Manager<")

    upload(client, token)
    html = client.get("/performance").get_data(as_text=True)
    assert 'id="perf-chart"' in html and 'id="period-table"' in html
    ranges = html[html.index('id="chart-range"'):html.index("</div>", html.index('id="chart-range"'))]
    assert [r for r in ("1M", "3M", "6M", "YTD", "1Y", "5Y", "All") if f'data-range="{r}"' in ranges] ==         ["1M", "3M", "6M", "YTD", "1Y", "5Y", "All"]
    assert ranges.index('data-range="1Y"') < ranges.index('data-range="5Y"') < ranges.index('data-range="All"')
    assert 'aria-current="page">Performance<' in html
    assert "/api/history" in html and 'data-page="performance"' in html
    assert 'id="holdings"' not in html  # only the performance section
    assert 'id="annual-chart"' in html and 'id="annual-table"' in html and "Performance by year" in html
    assert 'id="annual-chart"' not in client.get("/?all=1").get_data(as_text=True)  # Performance tab only
    assert client.get("/api/history").get_json()["years"]  # yearly figures come with the history
    assert 'id="perf-chart"' in client.get("/?all=1").get_data(as_text=True)  # still on the dashboard

    client.post("/settings/default-view", data={"csrf_token": token, "scope": "account:2"})
    assert client.get("/performance").headers["Location"] == "/performance?account=2"
    assert "account=2" in client.get("/performance?account=2").get_data(as_text=True)  # history URL scoped


def test_accounts_live_on_the_portfolio_manager_page(client):
    token = login(client)
    upload(client, token)
    client.post("/portfolios", data={"csrf_token": token, "name": "Retirement", "account_ids": ["1"]})
    html = client.get("/portfolios").get_data(as_text=True)
    table = html[html.index('id="accounts-table"'):html.index("</table>", html.index('id="accounts-table"'))]
    assert "51234567" in table and "51234568" in table and "/accounts/1/edit" in table  # accounts with edit links
    assert html.index("Create a portfolio") < html.index('id="accounts"') < html.index('id="add-account"')
    assert 'action="/accounts"' in html

    nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
    assert ">Accounts<" not in nav and 'aria-current="page">Portfolio Manager<' in nav
    edit_page = client.get("/accounts/1/edit").get_data(as_text=True)
    assert 'aria-current="page">Portfolio Manager<' in edit_page and "/portfolios#accounts" in edit_page

    assert client.get("/accounts").headers["Location"] == "/portfolios#accounts"  # old address still works
    resp = client.post("/accounts", data={"csrf_token": token, "number": "999", "type": "FHSA"})
    assert resp.headers["Location"] == "/portfolios#accounts"
    assert "999" in client.get("/portfolios").get_data(as_text=True)
    dup = client.post("/accounts", data={"csrf_token": token, "number": "999"})
    assert dup.headers["Location"] == "/portfolios"  # error: back to the top, where the message shows
    assert "already have an account numbered 999" in client.get("/portfolios").get_data(as_text=True)
    resp = client.post("/accounts/1/edit", data={"csrf_token": token, "number": "51234567", "type": "TFSA", "name": "Main"})
    assert resp.headers["Location"] == "/portfolios#accounts"


def test_transactions_live_on_the_portfolio_manager_page(client):
    token = login(client)
    upload(client, token)
    html = client.get("/portfolios?show=all").get_data(as_text=True)
    assert html.index('id="accounts"') < html.index('id="transactions"') < html.index('id="transactions-table"')
    table = html[html.index('id="transactions-table"'):html.index("</table>", html.index('id="transactions-table"'))]
    assert "VFV.TO" in table and "AAPL" in table and "/transactions/1/edit" in table
    assert 'action="/portfolios#transactions"' in html  # filters jump back to the section

    filtered = client.get("/portfolios?symbol=aapl").get_data(as_text=True)
    table = filtered[filtered.index('id="transactions-table"'):filtered.index("</table>", filtered.index('id="transactions-table"'))]
    assert "AAPL" in table and "VFV.TO" not in table
    assert 'value="AAPL"' in filtered and 'href="/portfolios#transactions">Clear</a>' in filtered

    nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
    assert ">Transactions<" not in nav
    form_page = client.get("/transactions/new").get_data(as_text=True)
    assert 'aria-current="page">Portfolio Manager<' in form_page and "/portfolios#transactions" in form_page
    assert "/portfolios?symbol=AAPL#transactions" in client.get("/position/AAPL").get_data(as_text=True)

    assert client.get("/transactions?symbol=AAPL").headers["Location"] == "/portfolios?symbol=AAPL#transactions"
    resp = client.post("/transactions/new", data={
        "csrf_token": token, "kind": "DEPOSIT", "date": "2025-06-01", "account_id": "1", "currency": "CAD",
        "net_amount": "100", "symbol": "", "quantity": "", "price": "", "costs": "",
    })
    assert resp.headers["Location"] == "/portfolios#transactions"
    resp = client.post("/transactions/1/delete", data={"csrf_token": token},
                       headers={"Referer": "http://localhost/portfolios?symbol=AAPL"})
    assert resp.headers["Location"] == "http://localhost/portfolios?symbol=AAPL#transactions"


def test_transactions_preview_the_latest_ten(client, monkeypatch):
    from app import views

    token = login(client)
    upload(client, token)  # 20 transactions

    def table_rows(html):
        start = html.index('id="transactions-table"')
        return html[start:html.index("</table>", start)].count("/edit")

    html = client.get("/portfolios").get_data(as_text=True)
    assert table_rows(html) == 10
    assert "20 transactions · showing the latest 10" in html
    assert 'href="/portfolios?show=all#transactions">Show all 20 transactions</a>' in html
    assert "Transaction pages" not in html  # no pager while collapsed

    html = client.get("/portfolios?show=all").get_data(as_text=True)
    assert table_rows(html) == 20 and "showing the latest" not in html
    assert 'href="/portfolios#transactions">Show fewer</a>' in html
    assert 'name="show" value="all"' in html  # filtering keeps the full list

    filtered = client.get("/portfolios?symbol=AAPL&show=all").get_data(as_text=True)
    assert 'href="/portfolios?symbol=AAPL#transactions">Show fewer</a>' not in filtered  # 3 rows: no toggle
    assert table_rows(client.get("/portfolios?page=2").get_data(as_text=True)) == 10  # page ignored when collapsed

    monkeypatch.setattr(views, "PAGE_SIZE", 8)  # expanded: 8 per page, 3 pages
    html = client.get("/portfolios?show=all&page=2").get_data(as_text=True)
    assert table_rows(html) == 8 and "Page 2 of 3" in html
    assert "/portfolios?show=all&amp;page=1#transactions" in html and "/portfolios?show=all&amp;page=3#transactions" in html
    assert table_rows(client.get("/portfolios?show=all&page=3").get_data(as_text=True)) == 4


def test_import_export_lives_on_the_portfolio_manager_page(client):
    token = login(client)
    html = client.get("/").get_data(as_text=True)  # empty dashboard points to the new section
    assert 'href="/portfolios#import">Import a CSV</a>' in html
    assert client.get("/import").headers["Location"] == "/portfolios#import"  # old address still works

    html = upload(client, token).get_data(as_text=True)  # results show on the Portfolio Manager page
    assert "Import results · activity.csv" in html and "<strong>20</strong> imported" in html
    assert html.index('id="import"') < html.index('id="import-result"') < html.index('id="import-form"')
    assert 'action="/import#import"' in html  # the page reloads at the section
    assert 'action="/portfolios" class="card form" id="create-portfolio">\n  <h2>Create a portfolio</h2>' in html  # not /import
    nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
    assert ">Import / Export<" not in nav and 'aria-current="page">Portfolio Manager<' in nav

    html = client.get("/portfolios").get_data(as_text=True)
    assert html.index("Create a portfolio") < html.index('id="import"') < html.index('id="accounts"') < html.index('id="transactions"')
    assert 'id="import-result"' not in html and 'id="export-form"' in html and "Transaction Date" in html

    missing = client.post("/import", data={"csrf_token": token}).get_data(as_text=True)
    assert "Choose a CSV file to import." in missing and 'id="import-result"' not in missing


def test_section_sidebar(app, client):
    token = login(client)
    # Before any activity the Dashboard and Performance tabs only show an empty state.
    for path in ("/", "/performance"):
        assert 'class="sidenav"' not in client.get(path).get_data(as_text=True), path
    upload(client, token)
    expected = {
        "/": ["kpis", "performance", "allocation", "cash-balances", "holdings-section", "earnings-section"],
        "/performance": ["performance", "annual"],
        "/markets": ["premarket", "futures", "sector-moves", "sentiment", "fear-greed-index", "rates", "rate-kpis",
                    "yield-curve"],
        "/portfolios": ["portfolios", "create-portfolio", "import", "accounts", "add-account", "transactions"],
    }
    for path, anchors in expected.items():
        html = client.get(path).get_data(as_text=True)
        nav = re.search(r'<nav class="sidenav".*?</nav>', html, re.S).group(0)
        assert re.findall(r'href="#([^"]+)"', nav) == anchors, path
        for anchor in anchors:  # every link lands on something
            assert f'id="{anchor}"' in html, (path, anchor)
        assert 'class="has-sidenav"' in html and "sidenav.js" in html
    for path in ("/position/VFV.TO", "/transactions/new"):
        html = client.get(path).get_data(as_text=True)
        assert 'class="sidenav"' not in html and "sidenav.js" not in html, path


MORTGAGE = {"name": "Home", "lender": "RBC", "principal": "500,000", "start_date": "2022-06-01", "rate": "5",
            "compounding": "semi-annual", "amortization_years": "25", "amortization_months": "0",
            "frequency": "monthly", "payment": "", "extra_payment": "", "term_end": "2027-06-01",
            "balance": "", "balance_date": ""}


def test_my_assets_net_worth(client):
    token = login(client)
    upload(client, token)
    client.post("/portfolios", data={"csrf_token": token, "name": "Retirement", "account_ids": ["1", "2"]})
    html = client.get("/assets").get_data(as_text=True)
    assert "My Net Worth" in html and "My Mortgages" in html and 'aria-current="page">My Assets' in html

    # A portfolio (named after it when the name is blank), chosen accounts, and a value entered by hand.
    resp = client.post("/assets", data={"csrf_token": token, "kind": "portfolio", "portfolio_id": "1", "name": ""})
    assert resp.headers["Location"].endswith("/assets#net-worth")
    client.post("/assets", data={"csrf_token": token, "kind": "accounts", "account_ids": ["2"], "name": "RRSP"})
    client.post("/assets", data={"csrf_token": token, "kind": "manual", "value": "$650,000", "name": "House"})
    client.post("/mortgages", data={"csrf_token": token, **MORTGAGE})

    whole = client.get("/api/portfolio?portfolio=1").get_json()["totals"]["total_value"]
    rrsp = client.get("/api/portfolio?account=2").get_json()["totals"]["total_value"]
    data = client.get("/api/assets").get_json()
    values = {a["id"]: a["value"] for a in data["assets"]}
    assert values == {1: whole, 2: rrsp, 3: 650000}
    assert data["total_assets"] == whole + rrsp + 650000
    assert len(data["mortgages"]) == 1 and 400000 < data["total_liabilities"] < 500000
    assert data["net_worth"] == data["total_assets"] - data["total_liabilities"]
    # The RRSP is in the portfolio too, so it is counted twice.
    assert any("counts it 2 times" in w for w in data["warnings"])

    html = client.get("/assets").get_data(as_text=True)
    assert "Portfolio · Retirement" in html and "$650,000.00" in html and 'id="mortgage-1"' in html

    # Edit: the house is revalued; deleting the portfolio leaves its asset at $0 with a warning.
    assert client.get("/assets/3/edit").status_code == 200
    client.post("/assets/3/edit", data={"csrf_token": token, "kind": "manual", "value": "700000", "name": "House"})
    client.post("/portfolios/1/delete", data={"csrf_token": token})
    data = client.get("/api/assets").get_json()
    values = {a["id"]: a["value"] for a in data["assets"]}
    assert values[3] == 700000 and values[1] == 0
    assert any("was deleted" in w for w in data["warnings"])
    assert "Portfolio deleted" in client.get("/assets").get_data(as_text=True)

    client.post("/assets/1/delete", data={"csrf_token": token})
    assert [a["id"] for a in client.get("/api/assets").get_json()["assets"]] == [3, 2]  # sorted by name


def test_asset_form_errors(client):
    token = login(client)
    html = client.post("/assets", data={"csrf_token": token, "kind": "manual", "value": "", "name": ""}).get_data(as_text=True)
    assert "Enter a name for the asset." in html and "Enter what the asset is worth." in html
    assert "data-errors" in html
    html = client.post("/assets", data={"csrf_token": token, "kind": "accounts", "name": "X"}).get_data(as_text=True)
    assert "Choose at least one account." in html
    html = client.post("/assets", data={"csrf_token": token, "kind": "portfolio", "portfolio_id": "99", "name": "X"}).get_data(as_text=True)
    assert "Choose a portfolio." in html
    html = client.post("/assets", data={"csrf_token": token, "kind": "manual", "value": "-5", "name": "X"}).get_data(as_text=True)
    assert "can&#39;t be negative" in html
    assert client.get("/api/assets").get_json()["assets"] == []


def test_mortgages(client):
    token = login(client)
    resp = client.post("/mortgages", data={"csrf_token": token, **MORTGAGE})
    assert resp.headers["Location"].endswith("/assets#mortgage-1")
    html = client.get("/assets").get_data(as_text=True)
    assert "Outstanding balance" in html and "Estimated final payment" in html and "June 2047" in html
    assert "$2,908.03" in html and "Balance at renewal" in html
    assert 'id="mortgage-chart"' in html and 'id="mortgage-chart-data"' in html
    assert 'class="pick"' not in html  # nothing to pick between with one mortgage
    assert 'href="#mortgage-1">Home</a>' in html and 'href="#mortgage-chart"' in html  # in the section sidebar

    # Edit page is filled in from the saved mortgage; saving an extra payment shows what it saves.
    html = client.get("/mortgages/1/edit").get_data(as_text=True)
    assert 'name="principal" value="500000"' in html and 'value="accelerated-bi-weekly"' in html
    client.post("/mortgages/1/edit", data={"csrf_token": token, **MORTGAGE, "extra_payment": "250"})
    assert "Saved by extra payments" in client.get("/assets").get_data(as_text=True)

    # A second mortgage adds a summary across both.
    client.post("/mortgages", data={"csrf_token": token, **MORTGAGE, "name": "Cottage", "principal": "200000"})
    html = client.get("/assets").get_data(as_text=True)
    assert 'id="mortgage-kpis"' in html and "Mortgage-free" in html
    # ...and one chart that can show either of them, or both combined.
    assert html.count('class="pick"') == 2 and 'data-combine="true"' in html
    assert html.count('id="mortgage-chart"') == 1 and "data-continued" in html
    # With continued contributions, the Cottage's payments go to the Home once it is paid off.
    data = client.get("/api/mortgages/continued?ids=1,2").get_json()
    assert [m["id"] for m in data["mortgages"]] == [1, 2] and data["months_saved"] > 0 and data["interest_saved"] > 0
    assert client.get("/api/mortgages/continued?ids=1").status_code == 400

    client.post("/mortgages/1/delete", data={"csrf_token": token})
    assert [m["name"] for m in client.get("/api/assets").get_json()["mortgages"]] == ["Cottage"]


def test_mortgage_form_errors(client):
    token = login(client)
    cases = {
        "Enter the original amount.": {"principal": ""},
        "The interest rate must be between 0% and 30%.": {"rate": "45"},
        "The amortization must be between 1 month and 50 years.": {"amortization_years": "0"},
        "never be paid off": {"payment": "1000"},
        "Enter both the balance from your statement and its date": {"balance": "400000"},
        "The statement date can&#39;t be in the future.": {"balance": "1", "balance_date": "2999-01-01"},
        "The term must end after the mortgage starts.": {"term_end": "2020-01-01"},
        "Choose how often you make payments.": {"frequency": "yearly"},
    }
    for message, change in cases.items():
        html = client.post("/mortgages", data={"csrf_token": token, **MORTGAGE, **change}).get_data(as_text=True)
        assert message in html, message
        assert 'value="RBC"' in html  # what was typed is kept
    assert client.get("/api/assets").get_json()["mortgages"] == []


def test_assets_and_mortgages_are_private(app, client):
    token = login(client)
    client.post("/assets", data={"csrf_token": token, "kind": "manual", "value": "1000", "name": "Car"})
    client.post("/mortgages", data={"csrf_token": token, **MORTGAGE})
    client.post("/mortgages", data={"csrf_token": token, **MORTGAGE, "name": "Cottage"})
    login_as(app, client, "someone-else", "else@example.com")
    assert client.get("/api/mortgages/continued?ids=1,2").status_code == 400
    data = client.get("/api/assets").get_json()
    assert data["assets"] == [] and data["mortgages"] == []
    for path in ("/assets/1/edit", "/mortgages/1/edit"):
        assert client.get(path).status_code == 404
    assert client.post("/mortgages/1/delete", data={"csrf_token": "token"}).status_code == 404
    assert client.post("/assets/1/delete", data={"csrf_token": "token"}).status_code == 404


def test_research(client, market):
    from datetime import date, datetime

    market.candles["AAPL"] = (
        "USD",
        [(date(2024, 2, 5), 180.0, 190.0, 179.0, 187.5), (date(2024, 11, 15), 220.0, 226.0, 219.0, 225.0)],
        [(date(2024, 6, 1), 2.0)],  # a 2:1 split between the buy and the sell
        [(datetime(2024, 11, 15, 9, 30), 220.0, 221.0, 219.5, 220.5)],
    )
    token = login(client)
    upload(client, token)
    # With no symbol picked, the biggest position in the Dashboard's account or portfolio.
    def default_symbol(query=""):
        return re.search(r'data-symbol="([^"]*)"', client.get(f"/research{query}").get_data(as_text=True)).group(1)

    top = client.get("/api/portfolio").get_json()["holdings"]
    assert default_symbol() == top[0]["symbol"] == "XEQT"
    rrsp = client.get("/api/portfolio?account=2").get_json()["holdings"]
    assert {h["symbol"] for h in rrsp} == {"AAPL", "MSFT"}
    assert default_symbol("?account=2") == rrsp[0]["symbol"]
    assert 'href="/research?account=2"' in client.get("/?account=2").get_data(as_text=True)  # the nav link keeps it
    client.post("/settings/default-view", data={"csrf_token": token, "scope": "account:2"})
    assert default_symbol() == rrsp[0]["symbol"]  # the saved default view
    assert default_symbol("?all=1") == "XEQT"
    assert 'aria-current="page">Research' in client.get("/research").get_data(as_text=True)
    assert client.get("/research?symbol=msft").status_code == 200
    assert "/research?symbol=AAPL" in client.get("/position/AAPL").get_data(as_text=True)

    data = client.get("/api/research/AAPL?start=2024-01-01").get_json()
    assert data["name"] == "AAPL Inc." and data["currency"] == "USD" and data["interval"] == "1d"
    assert data["daily"][0] == ["2024-02-05", 180.0, 190.0, 179.0, 187.5]
    # Prices before the split are halved (and quantities doubled) to match the adjusted chart.
    assert [(t["kind"], t["quantity"], t["price"]) for t in data["trades"]] == [("BUY", 30, 93.75), ("SELL", 5, 225.0)]
    assert data["trades"][0]["account"] == "RRSP · 51234568"

    intraday = client.get("/api/research/AAPL?span=1D").get_json()
    assert intraday["interval"] == "5m" and intraday["bars"] == [["2024-11-15T09:30", 220.0, 221.0, 219.5, 220.5]]

    # Any symbol Yahoo quotes works, traded or not; others don't.
    assert client.get("/api/research/MSFT?start=max").get_json()["trades"] == []
    assert client.get("/api/research/NOPE").status_code == 404
    assert client.get("/api/research/BAD%20SYMBOL").status_code == 400
    assert client.get("/api/research/AAPL?start=yesterday").status_code == 400
    assert client.get("/api/research/AAPL?span=2D").status_code == 400


def test_research_needs_sign_in(client):
    assert client.get("/research").status_code == 302
    assert client.get("/api/research/AAPL").status_code == 401
