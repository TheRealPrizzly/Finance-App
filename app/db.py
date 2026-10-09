"""SQLite access: per-request connections and the shared market-data cache."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import current_app, g

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    init_db(app.config["DATABASE"])


class SecurityStore:
    """Persists symbol resolution and sector data. Opens its own connections so it
    can be used from worker threads outside the request context."""

    def __init__(self, path):
        self.path = path

    def _run(self, sql, params=(), fetch=False):
        conn = connect(self.path)
        try:
            cur = conn.execute(sql, params)
            if fetch:
                return cur.fetchone()
            conn.commit()
        finally:
            conn.close()

    def get_security(self, symbol, currency):
        row = self._run("SELECT * FROM securities WHERE symbol = ? AND currency = ?", (symbol, currency), fetch=True)
        if row is None:
            return None
        data = dict(row)
        data["resolved_at"] = datetime.fromisoformat(data["resolved_at"])
        return data

    def save_security(self, symbol, currency, yahoo_symbol, name=None, quote_type=None):
        self._run(
            """INSERT INTO securities (symbol, currency, yahoo_symbol, name, quote_type, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (symbol, currency) DO UPDATE SET
                 yahoo_symbol = excluded.yahoo_symbol, name = excluded.name,
                 quote_type = excluded.quote_type, resolved_at = excluded.resolved_at""",
            (symbol, currency, yahoo_symbol, name, quote_type, datetime.now().isoformat()),
        )

    def get_sector(self, yahoo_symbol):
        row = self._run("SELECT * FROM sectors WHERE yahoo_symbol = ?", (yahoo_symbol,), fetch=True)
        if row is None:
            return None
        return json.loads(row["weights"]), datetime.fromisoformat(row["updated_at"])

    def save_sector(self, yahoo_symbol, weights):
        self._run(
            """INSERT INTO sectors (yahoo_symbol, weights, updated_at) VALUES (?, ?, ?)
               ON CONFLICT (yahoo_symbol) DO UPDATE SET weights = excluded.weights, updated_at = excluded.updated_at""",
            (yahoo_symbol, json.dumps(weights), datetime.now().isoformat()),
        )
