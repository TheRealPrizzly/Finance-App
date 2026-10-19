from datetime import date

import pytest

from app.sec import parse_company_facts, same_company

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
    assert "DepreciationAndAmortization" not in a


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
