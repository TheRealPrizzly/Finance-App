CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub  TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL,
    name        TEXT,
    picture     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A brokerage account (TFSA, RRSP, margin, ...). Each account acts as a sub-portfolio.
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    number      TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, number)
);

-- A named group of accounts (e.g. "Retirement"). An account can belong to several portfolios.
CREATE TABLE IF NOT EXISTS portfolios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS portfolio_accounts (
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (portfolio_id, account_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date           TEXT NOT NULL,              -- YYYY-MM-DD
    action         TEXT NOT NULL,              -- as entered or imported (e.g. "Buy", "DIV")
    kind           TEXT NOT NULL,              -- normalized: BUY, SELL, DIVIDEND, ...
    symbol         TEXT NOT NULL DEFAULT '',
    quantity       REAL NOT NULL DEFAULT 0,
    price          REAL NOT NULL DEFAULT 0,
    costs          REAL NOT NULL DEFAULT 0,
    net_amount     REAL NOT NULL DEFAULT 0,    -- cash effect in the transaction currency
    currency       TEXT NOT NULL,
    activity_type  TEXT NOT NULL DEFAULT '',
    fingerprint    TEXT,                       -- set on CSV import to skip duplicates
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_transactions_user_date ON transactions (user_id, date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_fingerprint
    ON transactions (user_id, fingerprint) WHERE fingerprint IS NOT NULL;

-- Shared market-data cache: how a (symbol, currency) maps to a Yahoo Finance symbol.
CREATE TABLE IF NOT EXISTS securities (
    symbol        TEXT NOT NULL,
    currency      TEXT NOT NULL,
    yahoo_symbol  TEXT,
    name          TEXT,
    quote_type    TEXT,
    resolved_at   TEXT NOT NULL,
    PRIMARY KEY (symbol, currency)
);

-- Shared market-data cache: sector weights per Yahoo symbol (ETFs are looked through).
CREATE TABLE IF NOT EXISTS sectors (
    yahoo_symbol  TEXT PRIMARY KEY,
    weights       TEXT NOT NULL,              -- JSON {"Technology": 0.37, ...}
    updated_at    TEXT NOT NULL
);
