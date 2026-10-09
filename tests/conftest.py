import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402

SAMPLE_CSV = pathlib.Path(__file__).resolve().parents[1] / "samples" / "sample_transactions.csv"


class FakeMarket:
    """Deterministic stand-in for MarketData."""

    def __init__(self, quotes=None, history=None, fx=None, sectors=None):
        self._quotes = quotes or {}  # symbol -> (price, prev_close, currency)
        self._history = history or {}  # symbol -> (currency, {date: close})
        self._fx = fx or {}  # currency -> (rate_now, {date: rate})
        self._sectors = sectors or {}

    def resolve(self, symbol, currency):
        return symbol if symbol in self._quotes or symbol in self._history else None

    def quotes(self, symbols):
        out = {}
        for s in symbols:
            if s in self._quotes:
                price, prev, cur = self._quotes[s]
                out[s] = {"symbol": s, "price": price, "prev_close": prev, "currency": cur, "name": f"{s} Inc.",
                          "quote_type": "EQUITY"}
        return out

    def history(self, symbol, start):
        return self._history.get(symbol, (None, {}))

    def fx_rate(self, currency):
        return self._fx.get(currency, (None, {}))[0]

    def fx_history(self, currency, start):
        return self._fx.get(currency, (None, {}))[1]

    def sector_weights(self, symbol, quote_type=None):
        return self._sectors.get(symbol, {"Technology": 1.0})


@pytest.fixture
def market():
    return FakeMarket(
        quotes={"VFV.TO": (150.0, 148.0, "CAD"), "XEQT": (35.0, 35.0, "CAD"), "AAPL": (250.0, 245.0, "USD"),
                "MSFT": (500.0, 505.0, "USD"), "SHOP": (200.0, 198.0, "CAD")},
        fx={"USD": (1.40, {})},
        sectors={"VFV.TO": {"Technology": 0.4, "Financial Services": 0.6}},
    )


@pytest.fixture
def app(tmp_path, market):
    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "SECRET_KEY": "test",
        "DEV_LOGIN": True,
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "MARKET": market,
    })
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client):
    with client.session_transaction() as s:
        s.clear()
    token = csrf(client)
    resp = client.post("/login/dev", data={"csrf_token": token})
    assert resp.status_code == 302
    return csrf(client, "/accounts")  # signing in starts a new session with a new token


def login_as(app, client, sub, email):
    """Sign in as an arbitrary user by writing the session directly."""
    from app.db import connect

    conn = connect(app.config["DATABASE"])
    conn.execute("INSERT OR IGNORE INTO users (google_sub, email, name) VALUES (?, ?, ?)", (sub, email, email))
    conn.commit()
    user_id = conn.execute("SELECT id FROM users WHERE google_sub = ?", (sub,)).fetchone()[0]
    conn.close()
    with client.session_transaction() as s:
        s.clear()
        s["user_id"] = user_id
        s["csrf"] = "token"
    return "token"
