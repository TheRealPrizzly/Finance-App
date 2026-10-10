import io

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
    accounts = client.get("/accounts").get_data(as_text=True)
    assert "51234567" in accounts and "51234568" in accounts
    # account ids are 1 and 2 in a fresh database
    rrsp = client.get("/api/portfolio?account=2").get_json()
    assert {h["symbol"] for h in rrsp["holdings"]} == {"AAPL", "MSFT"}
    hist = client.get("/api/history?account=1").get_json()
    assert hist["points"] and hist["points"][0]["date"] == "2024-01-05"


def test_pages_render(client):
    token = login(client)
    upload(client, token)
    for path in ("/", "/transactions", "/transactions?symbol=AAPL", "/accounts", "/import", "/position/VFV.TO",
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
    assert data["holdings"][0]["book_cost"] == 1005

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
