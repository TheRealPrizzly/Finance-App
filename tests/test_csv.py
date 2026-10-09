from datetime import date

import pytest

from app import csv_io
from app import portfolio as pf
from conftest import SAMPLE_CSV

HEADER = ",".join(csv_io.COLUMNS)


def test_parses_sample_file():
    rows, errors = csv_io.parse(SAMPLE_CSV.read_text())
    assert errors == []
    assert len(rows) == 20
    kinds = {r.action: r.kind for r in rows}
    assert kinds == {
        "CON": pf.DEPOSIT, "DEP": pf.DEPOSIT, "Buy": pf.BUY, "Sell": pf.SELL, "FXT": pf.FX,
        "DIV": pf.DIVIDEND, "FCH": pf.FEE,
    }


@pytest.mark.parametrize("text", ["2025-03-04", "2025-03-04 12:00:00 AM", "03/04/2025", "2025-03-04T00:00:00.000000-05:00"])
def test_date_formats(text):
    assert csv_io.parse_date(text) == date(2025, 3, 4)


def test_numbers_with_symbols_and_parentheses():
    assert csv_io.parse_number("$1,234.50") == 1234.5
    assert csv_io.parse_number("(25.00)") == -25.0
    assert csv_io.parse_number("") == 0.0
    with pytest.raises(ValueError):
        csv_io.parse_number("abc")


def test_missing_required_columns():
    rows, errors = csv_io.parse("Date,Symbol\n2025-01-01,AAPL\n")
    assert rows == []
    assert "Action" in errors[0][1] and "Currency" in errors[0][1]


def test_net_amount_computed_for_trades_when_missing():
    rows, errors = csv_io.parse(f"{HEADER}\n2025-01-02,Buy,AAPL,10,100,5,,USD,123,Trades,Margin\n")
    assert errors == []
    assert rows[0].net_amount == -1005


def test_bad_rows_are_reported_and_skipped():
    text = f"{HEADER}\nnot-a-date,Buy,AAPL,1,1,0,-1,USD,1,Trades,\n2025-01-02,Buy,,1,1,0,-1,USD,1,Trades,\n2025-01-02,DEP,,0,0,0,5,US,1,Deposits,\n"
    rows, errors = csv_io.parse(text)
    assert rows == []
    assert [e[0] for e in errors] == [2, 3, 4]


def test_header_aliases_and_semicolons():
    text = "Date;Type;Ticker;Qty;Price;Commission;Net;Currency;Account Number\n2025-01-02;Buy;VFV.TO;2;100;0;-200;CAD;9\n"
    rows, errors = csv_io.parse(text)
    assert errors == []
    assert (rows[0].symbol, rows[0].quantity, rows[0].account_number) == ("VFV.TO", 2, "9")


def test_fingerprints_distinguish_identical_rows_in_one_file():
    line = "2025-01-02,Buy,AAPL,1,100,0,-100,USD,1,Trades,Margin"
    rows, _ = csv_io.parse(f"{HEADER}\n{line}\n{line}\n")
    fps = list(csv_io.fingerprints(rows))
    assert len(set(fps)) == 2
    assert fps == list(csv_io.fingerprints(rows))  # stable


def test_classify_questrade_codes():
    assert csv_io.classify("WDR", "Withdrawals") == pf.WITHDRAWAL
    assert csv_io.classify("INT", "Interest") == pf.INTEREST
    assert csv_io.classify("", "Trades", "AAPL", -5, 500) == pf.SELL
    assert csv_io.classify("TF6", "Transfers") == pf.TRANSFER
    assert csv_io.classify("REI", "Dividend reinvestment", "XEQT", 1, 0) == pf.OTHER
    assert csv_io.classify("XYZ", "") == pf.OTHER


def test_export_round_trip():
    rows, _ = csv_io.parse(SAMPLE_CSV.read_text())
    exported = csv_io.export([
        {"date": r.date.isoformat(), "action": r.action, "symbol": r.symbol, "quantity": r.quantity, "price": r.price,
         "costs": r.costs, "net_amount": r.net_amount, "currency": r.currency, "account_number": r.account_number,
         "activity_type": r.activity_type, "account_type": r.account_type}
        for r in rows
    ])
    assert exported.splitlines()[0] == HEADER
    again, errors = csv_io.parse(exported)
    assert errors == []
    assert list(csv_io.fingerprints(again)) == list(csv_io.fingerprints(rows))
