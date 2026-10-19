from datetime import date

import pytest

from app.sec import SecData, parse_company_facts, same_company

FY = [("2023-01-01", "2023-12-31"), ("2024-01-01", "2024-12-31")]
Q24 = [("2024-01-01", "2024-03-31"), ("2024-04-01", "2024-06-30"), ("2024-07-01", "2024-09-30"), ("2024-10-01", "2024-12-31")]


def fact(start, end, val, filed="2025-02-01", form="10-K"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def facts(us_gaap):
    return {"facts": {"us-gaap": {name: {"units": units} for name, units in us_gaap.items()}}}


FILINGS = facts({
    # Revenue moved to a new concept in 2024, and the Q2 2024 10-Q gave both the quarter and the half.
    "SalesRevenueNet": {"USD": [fact(*FY[0], 100.0)]},
    "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
        fact(*FY[1], 120.0), fact(*Q24[0], 28.0, "2024-05-01", "10-Q"), fact(*Q24[1], 30.0, "2024-08-01", "10-Q"),
        fact("2024-01-01", "2024-06-30", 58.0, "2024-08-01", "10-Q"), fact(*Q24[2], 31.0, "2024-11-01", "10-Q"),
        fact("2025-01-01", "2025-03-31", 33.0, "2025-05-01", "10-Q")]},
    "OperatingIncomeLoss": {"USD": [fact(*FY[0], 20.0), fact(*FY[1], 25.0)]},
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {"USD": [
        fact(*FY[0], 18.0), fact(*FY[1], 22.0)]},
    "InterestExpense": {"USD": [fact(*FY[1], 3.0)]},
    "IncomeTaxExpenseBenefit": {"USD": [fact(*FY[1], 4.4)]},
    # Net income for 2023 was restated in the 2024 annual report.
    "NetIncomeLoss": {"USD": [fact(*FY[0], 15.0, "2024-02-01"), fact(*FY[0], 14.0, "2025-02-01"), fact(*FY[1], 17.6)]},
    # Cash flow items come year to date in 10-Qs.
    "DepreciationDepletionAndAmortization": {"USD": [
        fact("2024-01-01", "2024-03-31", 1.0, "2024-05-01", "10-Q"), fact("2024-01-01", "2024-06-30", 2.5, "2024-08-01", "10-Q"),
        fact("2024-01-01", "2024-09-30", 4.0, "2024-11-01", "10-Q"), fact(*FY[1], 6.0)]},
    # A 2:1 split in mid-2024: the 2023 figures were filed before it.
    "EarningsPerShareDiluted": {"USD/shares": [fact(*FY[0], 3.0, "2024-02-01"), fact(*FY[1], 1.76)], "pure": [fact(*FY[1], 9.9)]},
    "WeightedAverageNumberOfDilutedSharesOutstanding": {"shares": [
        fact(*FY[0], 5.0, "2024-02-01"), fact(*FY[1], 10.0), fact(*Q24[0], 0.01, "2024-05-01", "10-Q")]},  # one tagged in millions
})
SPLITS = [(date(2024, 6, 10), 2.0)]


@pytest.fixture
def statement():
    return parse_company_facts(FILINGS, SPLITS)


def test_years_merge_concepts_and_take_restatements(statement):
    a = statement["annual"]
    assert statement["currency"] == "USD" and statement["source"] == "sec"
    assert a["TotalRevenue"] == {"2023-12-31": 100.0, "2024-12-31": 120.0}
    assert a["NetIncome"]["2023-12-31"] == 14.0  # the restated figure


def test_quarters_fill_in_from_year_to_date_and_the_year(statement):
    q = statement["quarterly"]
    assert q["TotalRevenue"] == {"2024-03-31": 28.0, "2024-06-30": 30.0, "2024-09-30": 31.0,
                                 "2024-12-31": 120.0 - 28 - 30 - 31, "2025-03-31": 33.0}
    assert "EBIT" not in q and "EBITDA" not in q  # no quarterly operating income, so no EBIT either
    assert "DilutedEPS" not in q  # per-share figures aren't derived for missing quarters


def test_year_to_date_flows_become_quarters():
    q = parse_company_facts(facts({
        "Revenues": {"USD": [fact(*FY[1], 10.0)]},
        "OperatingIncomeLoss": {"USD": [fact("2024-01-01", "2024-03-31", 1.0, "2024-05-01", "10-Q"),
                                        fact("2024-01-01", "2024-06-30", 2.5, "2024-08-01", "10-Q"),
                                        fact("2024-01-01", "2024-09-30", 4.0, "2024-11-01", "10-Q"), fact(*FY[1], 6.0)]},
    }))["quarterly"]["OperatingIncome"]
    assert q == {"2024-03-31": 1.0, "2024-06-30": 1.5, "2024-09-30": 1.5, "2024-12-31": 2.0}


def test_ebit_ebitda_and_tax_rate(statement):
    a = statement["annual"]
    assert a["EBIT"] == {"2023-12-31": 20.0, "2024-12-31": 22.0 + 3.0}  # pretax plus interest where it's reported
    assert a["EBITDA"] == {"2024-12-31": 25.0 + 6.0}
    assert a["TaxRateForCalcs"] == {"2024-12-31": pytest.approx(0.2)}
    assert a["DepreciationAndAmortization"] == {"2024-12-31": 6.0}  # for the cash flow statement


def test_a_bank_has_no_ebit():
    bank = parse_company_facts(facts({
        "RevenuesNetOfInterestExpense": {"USD": [fact(*FY[1], 100.0)]},
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {"USD": [fact(*FY[1], 40.0)]},
        "InterestExpense": {"USD": [fact(*FY[1], 90.0)]},
    }))
    assert "EBIT" not in bank["annual"] and bank["annual"]["TotalRevenue"] == {"2024-12-31": 100.0}


def test_per_share_figures_are_adjusted_for_later_splits(statement):
    a = statement["annual"]
    assert a["DilutedEPS"] == {"2023-12-31": 1.5, "2024-12-31": 1.76}
    assert a["DilutedAverageShares"] == {"2023-12-31": 10.0, "2024-12-31": 10.0}
    assert statement["quarterly"].get("DilutedAverageShares", {}) == {}  # the count tagged in millions is dropped


def test_ttm_adds_up_the_last_four_quarters(statement):
    assert statement["trailing"]["TotalRevenue"] == {"2025-03-31": 30.0 + 31.0 + 31.0 + 33.0}


def test_no_filings():
    assert parse_company_facts({}) == {"currency": None, "annual": {}, "quarterly": {}, "trailing": {}, "source": "sec"}


def test_same_company():
    assert same_company("Shopify Inc.", "SHOPIFY INC.")
    assert same_company("Royal Bank of Canada", "ROYAL BANK OF CANADA")
    assert not same_company("TELUS Corporation", "AT&T INC.")
    assert not same_company(None, "AT&T INC.")


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, answers):
        self.answers, self.headers, self.asked = answers, {}, []

    def get(self, url, timeout=None):
        self.asked.append(url)
        answer = self.answers[url]
        return answer.pop(0) if isinstance(answer, list) else answer


TICKERS = "https://www.sec.gov/files/company_tickers.json"
AAPL_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


def sec_with(answers):
    sec = SecData("me@example.com")
    sec.session = FakeSession(answers)
    return sec


def test_statement_and_status():
    sec = sec_with({TICKERS: FakeResponse(200, {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                                                "1": {"cik_str": 1, "ticker": "T", "title": "AT&T INC."}}),
                    AAPL_FACTS: FakeResponse(200, FILINGS)})
    found = sec.income_statement("AAPL", "Apple Inc.")
    assert found["status"] == "ok" and found["statement"]["annual"]["TotalRevenue"]["2024-12-31"] == 120.0
    assert sec.income_statement("T.TO", "TELUS Corporation") == {"statement": None, "status": "not_filed"}
    assert sec.income_statement("XEQT.TO", "iShares Core Equity ETF") == {"statement": None, "status": "not_filed"}
    sec.income_statement("AAPL", "Apple Inc.")
    assert sec.session.asked.count(AAPL_FACTS) == 1  # kept for a while


def test_an_empty_ticker_list_is_not_kept():
    sec = sec_with({TICKERS: [FakeResponse(200, {}), FakeResponse(200, {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}})],
                    AAPL_FACTS: FakeResponse(200, FILINGS)})
    first = sec.income_statement("AAPL")
    assert first["status"] == "unavailable" and "empty" in first["detail"]
    assert sec.income_statement("AAPL")["status"] == "ok"  # asked again, rather than every company being a non-filer for a week


def test_a_refusal_says_so():
    found = sec_with({TICKERS: FakeResponse(403)}).income_statement("AAPL")
    assert found == {"statement": None, "status": "unavailable", "detail": "HTTP 403: EDGAR refused the request"}


def instant(end, val, filed="2025-02-01", form="10-K"):
    return {"end": end, "val": val, "filed": filed, "form": form}


def test_balance_sheet_and_cash_flow():
    statement = parse_company_facts(facts({
        "Revenues": {"USD": [fact(*FY[0], 100.0), fact(*FY[1], 120.0), fact(*Q24[0], 28.0, "2024-05-01", "10-Q")]},
        # Balances at the year ends and the quarter, plus one on a day that's neither (left out).
        "Assets": {"USD": [instant("2023-12-31", 500.0), instant("2024-12-31", 600.0), instant("2024-03-31", 520.0, "2024-05-01", "10-Q"),
                           instant("2024-05-15", 1.0)]},
        "AssetsCurrent": {"USD": [instant("2024-12-31", 200.0)]},
        "LiabilitiesCurrent": {"USD": [instant("2024-12-31", 150.0)]},
        "StockholdersEquity": {"USD": [instant("2024-12-31", 300.0)]},
        "LiabilitiesAndStockholdersEquity": {"USD": [instant("2024-12-31", 600.0)]},  # no Liabilities line: worked out
        "CashAndCashEquivalentsAtCarryingValue": {"USD": [instant("2024-12-31", 40.0)]},
        "MarketableSecuritiesCurrent": {"USD": [instant("2024-12-31", 10.0)]},
        "LongTermDebtNoncurrent": {"USD": [instant("2024-12-31", 80.0)]},
        "LongTermDebtCurrent": {"USD": [instant("2024-12-31", 20.0)]},
        "CommonStockSharesOutstanding": {"shares": [instant("2023-12-31", 5.0, "2024-02-01")]},  # before the 2:1 split
        "NetCashProvidedByUsedInOperatingActivities": {"USD": [fact(*FY[1], 50.0), fact(*Q24[0], 10.0, "2024-05-01", "10-Q"),
                                                               fact("2024-01-01", "2024-06-30", 22.0, "2024-08-01", "10-Q")]},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"USD": [fact(*FY[1], 15.0)]},
        "PaymentsOfDividends": {"USD": [fact(*FY[1], 5.0)]},
    }), SPLITS)
    a, q = statement["annual"], statement["quarterly"]
    assert a["TotalAssets"] == {"2023-12-31": 500.0, "2024-12-31": 600.0}
    assert q["TotalAssets"] == {"2024-03-31": 520.0}
    assert a["TotalLiabilitiesNetMinorityInterest"] == {"2024-12-31": 300.0}
    assert a["WorkingCapital"] == {"2024-12-31": 50.0} and a["TotalNonCurrentAssets"] == {"2024-12-31": 400.0}
    assert a["CashCashEquivalentsAndShortTermInvestments"] == {"2024-12-31": 50.0}
    assert a["TotalDebt"] == {"2024-12-31": 100.0} and a["NetDebt"] == {"2024-12-31": 60.0}
    assert a["InvestedCapital"] == {"2024-12-31": 400.0}
    assert a["OrdinarySharesNumber"] == {"2023-12-31": 10.0}
    assert a["CapitalExpenditure"] == {"2024-12-31": -15.0} and a["CashDividendsPaid"] == {"2024-12-31": -5.0}  # outflows
    assert a["FreeCashFlow"] == {"2024-12-31": 35.0}
    assert q["OperatingCashFlow"] == {"2024-03-31": 10.0, "2024-06-30": 12.0}  # the half less the first quarter


def test_long_term_debt_reported_with_its_current_part():
    a = parse_company_facts(facts({
        "Revenues": {"USD": [fact(*FY[1], 100.0)]},
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": {"USD": [instant("2024-12-31", 400.0)]},
        "ShortTermBorrowings": {"USD": [instant("2024-12-31", 60.0)]},
    }))["annual"]
    assert a["TotalDebt"] == {"2024-12-31": 460.0}
