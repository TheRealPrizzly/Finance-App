CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub  TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL,
    name        TEXT,
    picture     TEXT,
    theme       TEXT NOT NULL DEFAULT 'white',
    default_scope TEXT NOT NULL DEFAULT '',  -- view the dashboard opens with: '', 'account:<id>' or 'portfolio:<id>'
    pay_day     TEXT,                        -- any pay day (YYYY-MM-DD); pay comes every 14 days from it
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

-- Dashboard warnings the user closed; matched by their exact text so they stay hidden.
CREATE TABLE IF NOT EXISTS dismissed_warnings (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, message)
);

-- Something the user owns, counted in their net worth: the live value of a portfolio or of chosen
-- accounts, or a value entered by hand (a home, a car, ...).
CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- 'portfolio', 'accounts' or 'manual'
    portfolio_id  INTEGER REFERENCES portfolios(id) ON DELETE SET NULL,
    value         REAL NOT NULL DEFAULT 0,    -- manual assets only, in the base currency
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS asset_accounts (
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (asset_id, account_id)
);

-- A mortgage, amortized from its original amount, or from a statement balance when one is given.
CREATE TABLE IF NOT EXISTS mortgages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,
    lender               TEXT NOT NULL DEFAULT '',
    principal            REAL NOT NULL,              -- original amount borrowed
    start_date           TEXT NOT NULL,              -- YYYY-MM-DD; the first payment is one period later
    rate                 REAL NOT NULL,              -- annual interest rate, percent
    compounding          TEXT NOT NULL DEFAULT 'semi-annual',
    amortization_months  INTEGER NOT NULL,
    frequency            TEXT NOT NULL DEFAULT 'monthly',
    payment              REAL,                       -- regular payment; NULL = calculated
    extra_payment        REAL NOT NULL DEFAULT 0,    -- prepaid with every regular payment
    term_end             TEXT,                       -- end of the current term (renewal date)
    balance              REAL,                       -- balance from a statement, with ...
    balance_date         TEXT,                       -- ... the date of that balance
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A line of the budget: money coming in (revenue) or going out (expense). Recurring lines have a fixed
-- amount on a schedule from start_date; lines that vary ('varies') have amounts entered per pay day.
CREATE TABLE IF NOT EXISTS budget_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,              -- 'revenue' or 'expense'
    name        TEXT NOT NULL,
    frequency   TEXT NOT NULL,              -- 'pay', 'weekly', 'biweekly', 'monthly', 'quarterly', 'yearly', 'once' or 'varies'
    amount      REAL,                       -- each time it is due; NULL for lines that vary
    start_date  TEXT,                       -- YYYY-MM-DD: the first time it is due
    end_date    TEXT,                       -- YYYY-MM-DD: the last day it can be due, or NULL
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- An amount entered for one pay period of a line: the amount of a line that varies, or a change to
-- a recurring line's fixed amount for that period.
CREATE TABLE IF NOT EXISTS budget_entries (
    line_id     INTEGER NOT NULL REFERENCES budget_lines(id) ON DELETE CASCADE,
    pay_day     TEXT NOT NULL,              -- YYYY-MM-DD: the pay day that starts the period
    amount      REAL NOT NULL,
    PRIMARY KEY (line_id, pay_day)
);
