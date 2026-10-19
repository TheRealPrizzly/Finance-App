"""Financial statements as filed with the US SEC, from EDGAR's XBRL company facts.

Yahoo Finance gives four fiscal years and five quarters; EDGAR has every filing since about
2009 for companies listed in the US, including Canadian and other foreign companies that file
there (some of those, like TSMC, file only annual reports). The SEC asks every request to name
a contact, so this is off unless SEC_CONTACT (an email address) is set.
"""
import logging
import re
import threading
import time
from datetime import date

import requests
from flask import current_app

log = logging.getLogger(__name__)

FACTS_TTL = 12 * 60 * 60
TICKERS_TTL = 7 * 24 * 60 * 60
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{:010d}.json"

# Income statement lines as Yahoo names them, each with the XBRL concepts that carry it, in order
# of preference: companies switch concepts over the years (Apple's revenue was SalesRevenueNet
# until 2017), so each period takes the first one reported for it. Kinds: "flow" figures add up
# over quarters, so a missing quarter is the year (or year to date) less the others; "per_share"
# and "shares" figures don't, and are adjusted for later stock splits.
INCOME_CONCEPTS = {
    "TotalRevenue": ("flow", ["us-gaap:RevenuesNetOfInterestExpense", "us-gaap:Revenues",
                              "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                              "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax", "us-gaap:SalesRevenueNet",
                              "us-gaap:SalesRevenueGoodsNet", "ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers"]),
    "CostOfRevenue": ("flow", ["us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsAndServicesSold", "us-gaap:CostOfGoodsSold",
                               "ifrs-full:CostOfSales"]),
    "GrossProfit": ("flow", ["us-gaap:GrossProfit", "ifrs-full:GrossProfit"]),
    "ResearchAndDevelopment": ("flow", ["us-gaap:ResearchAndDevelopmentExpense",
                                        "us-gaap:ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
                                        "ifrs-full:ResearchAndDevelopmentExpense"]),
    "SellingGeneralAndAdministration": ("flow", ["us-gaap:SellingGeneralAndAdministrativeExpense",
                                                 "ifrs-full:SellingGeneralAndAdministrativeExpense"]),
    "OperatingExpense": ("flow", ["us-gaap:OperatingExpenses", "us-gaap:NoninterestExpense"]),
    "OperatingIncome": ("flow", ["us-gaap:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities"]),
    "InterestIncome": ("flow", ["us-gaap:InvestmentIncomeInterest", "us-gaap:InterestAndDividendIncomeOperating",
                                "ifrs-full:RevenueFromInterest"]),
    "InterestExpense": ("flow", ["us-gaap:InterestExpense", "us-gaap:InterestExpenseNonoperating",
                                 "us-gaap:InterestExpenseOperating", "us-gaap:InterestExpenseDebt",
                                 "ifrs-full:FinanceCosts", "ifrs-full:InterestExpense"]),
    "OtherIncomeExpense": ("flow", ["us-gaap:NonoperatingIncomeExpense", "us-gaap:OtherNonoperatingIncomeExpense"]),
    "PretaxIncome": ("flow", ["us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                              "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                              "ifrs-full:ProfitLossBeforeTax"]),
    "TaxProvision": ("flow", ["us-gaap:IncomeTaxExpenseBenefit", "ifrs-full:IncomeTaxExpenseContinuingOperations"]),
    "NetIncome": ("flow", ["us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss", "ifrs-full:ProfitLossAttributableToOwnersOfParent",
                           "ifrs-full:ProfitLoss"]),
    "NetIncomeCommonStockholders": ("flow", ["us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic", "us-gaap:NetIncomeLoss",
                                             "ifrs-full:ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntity",
                                             "ifrs-full:ProfitLossAttributableToOwnersOfParent"]),
    "DepreciationAndAmortization": ("flow", ["us-gaap:DepreciationDepletionAndAmortization", "us-gaap:DepreciationAndAmortization",
                                             "us-gaap:DepreciationAmortizationAndAccretionNet",
                                             "ifrs-full:DepreciationAndAmortisationExpense"]),
    "BasicEPS": ("per_share", ["us-gaap:EarningsPerShareBasic", "ifrs-full:BasicEarningsLossPerShare"]),
    "DilutedEPS": ("per_share", ["us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasicAndDiluted",
                                 "ifrs-full:DilutedEarningsLossPerShare"]),
    "BasicAverageShares": ("shares", ["us-gaap:WeightedAverageNumberOfSharesOutstandingBasic", "ifrs-full:WeightedAverageShares"]),
    "DilutedAverageShares": ("shares", ["us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                                        "ifrs-full:AdjustedWeightedAverageShares"]),
}
# The balance sheet: "instant" figures are balances on a day, "instant_shares" a share count on a day.
# Lines starting with "_" only feed the ones worked out below and aren't kept.
BALANCE_CONCEPTS = {
    "CashAndCashEquivalents": ("instant", ["us-gaap:CashAndCashEquivalentsAtCarryingValue", "us-gaap:Cash",
                                           "ifrs-full:CashAndCashEquivalents"]),
    "CashCashEquivalentsAndShortTermInvestments": ("instant", ["us-gaap:CashCashEquivalentsAndShortTermInvestments"]),
    "_ShortTermInvestments": ("instant", ["us-gaap:ShortTermInvestments", "us-gaap:MarketableSecuritiesCurrent",
                                          "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                                          "ifrs-full:CurrentInvestments"]),
    "Receivables": ("instant", ["us-gaap:AccountsReceivableNetCurrent", "us-gaap:ReceivablesNetCurrent",
                                "ifrs-full:TradeAndOtherCurrentReceivables"]),
    "Inventory": ("instant", ["us-gaap:InventoryNet", "ifrs-full:Inventories"]),
    "CurrentAssets": ("instant", ["us-gaap:AssetsCurrent", "ifrs-full:CurrentAssets"]),
    "NetPPE": ("instant", ["us-gaap:PropertyPlantAndEquipmentNet",
                           "us-gaap:PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
                           "ifrs-full:PropertyPlantAndEquipment"]),
    "GoodwillAndOtherIntangibleAssets": ("instant", ["us-gaap:IntangibleAssetsNetIncludingGoodwill",
                                                     "ifrs-full:IntangibleAssetsAndGoodwill"]),
    "_Goodwill": ("instant", ["us-gaap:Goodwill", "ifrs-full:Goodwill"]),
    "_Intangibles": ("instant", ["us-gaap:IntangibleAssetsNetExcludingGoodwill", "us-gaap:FiniteLivedIntangibleAssetsNet",
                                 "ifrs-full:IntangibleAssetsOtherThanGoodwill"]),
    "TotalNonCurrentAssets": ("instant", ["us-gaap:AssetsNoncurrent", "us-gaap:NoncurrentAssets", "ifrs-full:NoncurrentAssets"]),
    "TotalAssets": ("instant", ["us-gaap:Assets", "ifrs-full:Assets"]),
    "PayablesAndAccruedExpenses": ("instant", ["us-gaap:AccountsPayableAndAccruedLiabilitiesCurrent", "us-gaap:AccountsPayableCurrent",
                                               "ifrs-full:TradeAndOtherCurrentPayables"]),
    "CurrentDebt": ("instant", ["us-gaap:DebtCurrent", "us-gaap:LongTermDebtCurrent", "us-gaap:ShortTermBorrowings",
                                "ifrs-full:CurrentBorrowings", "ifrs-full:ShorttermBorrowings"]),
    "CurrentLiabilities": ("instant", ["us-gaap:LiabilitiesCurrent", "ifrs-full:CurrentLiabilities"]),
    "LongTermDebt": ("instant", ["us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligations",
                                 "ifrs-full:NoncurrentBorrowings", "ifrs-full:LongtermBorrowings"]),
    "_LongTermDebtTotal": ("instant", ["us-gaap:LongTermDebt",  # current and long-term together (JPMorgan)
                                       "us-gaap:LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"]),
    "_LongTermDebtCurrent": ("instant", ["us-gaap:LongTermDebtCurrent"]),
    "TotalLiabilitiesNetMinorityInterest": ("instant", ["us-gaap:Liabilities", "ifrs-full:Liabilities"]),
    "_LiabilitiesAndEquity": ("instant", ["us-gaap:LiabilitiesAndStockholdersEquity", "ifrs-full:EquityAndLiabilities"]),
    "_TotalEquity": ("instant", ["us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "ifrs-full:Equity"]),
    "RetainedEarnings": ("instant", ["us-gaap:RetainedEarningsAccumulatedDeficit", "ifrs-full:RetainedEarnings"]),
    "StockholdersEquity": ("instant", ["us-gaap:StockholdersEquity", "ifrs-full:EquityAttributableToOwnersOfParent"]),
    "MinorityInterest": ("instant", ["us-gaap:MinorityInterest", "ifrs-full:NoncontrollingInterests"]),
    "OrdinarySharesNumber": ("instant_shares", ["us-gaap:CommonStockSharesOutstanding", "ifrs-full:NumberOfSharesOutstanding"]),
}
# The cash flow statement. A sign of -1 turns payments, which filers report as positive amounts, into
# the outflows Yahoo shows (capital expenditure, dividends, buybacks and debt repaid are negative).
CASH_FLOW_CONCEPTS = {
    "DepreciationAndAmortization": INCOME_CONCEPTS["DepreciationAndAmortization"],
    "StockBasedCompensation": ("flow", ["us-gaap:ShareBasedCompensation", "us-gaap:AllocatedShareBasedCompensationExpense",
                                        "ifrs-full:AdjustmentsForSharebasedPayments"]),
    "OperatingCashFlow": ("flow", ["us-gaap:NetCashProvidedByUsedInOperatingActivities",
                                   "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                                   "ifrs-full:CashFlowsFromUsedInOperatingActivities"]),
    "CapitalExpenditure": ("flow", ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap:PaymentsToAcquireProductiveAssets",
                                    "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"], -1),
    "InvestingCashFlow": ("flow", ["us-gaap:NetCashProvidedByUsedInInvestingActivities",
                                   "us-gaap:NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
                                   "ifrs-full:CashFlowsFromUsedInInvestingActivities"]),
    "CashDividendsPaid": ("flow", ["us-gaap:PaymentsOfDividends", "us-gaap:PaymentsOfDividendsCommonStock",
                                   "ifrs-full:DividendsPaidClassifiedAsFinancingActivities", "ifrs-full:DividendsPaid"], -1),
    "RepurchaseOfCapitalStock": ("flow", ["us-gaap:PaymentsForRepurchaseOfCommonStock", "us-gaap:PaymentsForRepurchaseOfEquity",
                                          "ifrs-full:PaymentsToAcquireOrRedeemEntitysShares"], -1),
    "IssuanceOfCapitalStock": ("flow", ["us-gaap:ProceedsFromIssuanceOfCommonStock", "ifrs-full:ProceedsFromIssuingShares"]),
    "IssuanceOfDebt": ("flow", ["us-gaap:ProceedsFromIssuanceOfLongTermDebt", "us-gaap:ProceedsFromIssuanceOfDebt",
                                "ifrs-full:ProceedsFromBorrowingsClassifiedAsFinancingActivities", "ifrs-full:ProceedsFromNoncurrentBorrowings"]),
    "RepaymentOfDebt": ("flow", ["us-gaap:RepaymentsOfLongTermDebt", "us-gaap:RepaymentsOfDebt",
                                 "ifrs-full:RepaymentsOfBorrowingsClassifiedAsFinancingActivities",
                                 "ifrs-full:RepaymentsOfNoncurrentBorrowings"], -1),
    "FinancingCashFlow": ("flow", ["us-gaap:NetCashProvidedByUsedInFinancingActivities",
                                   "us-gaap:NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
                                   "ifrs-full:CashFlowsFromUsedInFinancingActivities"]),
    "ChangesInCash": ("flow", ["us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
                               "us-gaap:CashAndCashEquivalentsPeriodIncreaseDecrease",
                               "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
                               "ifrs-full:IncreaseDecreaseInCashAndCashEquivalents"]),
    "EndCashPosition": ("instant", ["us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                                    "us-gaap:CashAndCashEquivalentsAtCarryingValue", "ifrs-full:CashAndCashEquivalents"]),
}
CONCEPTS = {**INCOME_CONCEPTS, **BALANCE_CONCEPTS, **CASH_FLOW_CONCEPTS}
DERIVED_BALANCES = {"TotalDebt", "NetDebt", "WorkingCapital", "InvestedCapital"}  # worked out from balances: no TTM
YEAR_DAYS, QUARTER_DAYS = (350, 380), (80, 100)  # 52/53-week years and 12/13/14-week quarters


def _days(start, end):
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _currency(facts):
    """The currency most revenue and earnings figures are reported in."""
    counts = {}
    for name in INCOME_CONCEPTS["TotalRevenue"][1] + INCOME_CONCEPTS["NetIncome"][1]:
        taxonomy, concept = name.split(":")
        for unit, rows in ((facts.get(taxonomy) or {}).get(concept) or {}).get("units", {}).items():
            if re.fullmatch(r"[A-Z]{3}", unit):
                counts[unit] = counts.get(unit, 0) + len(rows)
    return max(counts, key=counts.get) if counts else None


def _periods(facts, names, unit, instant=False):
    """{(start, end): (value, filed)} for the first concept reported for each period, keeping the
    latest filing of each (later filings restate earlier periods); {end: (value, filed)} for balances."""
    out = {}
    for name in names:
        taxonomy, concept = name.split(":")
        seen = {}
        for row in ((facts.get(taxonomy) or {}).get(concept) or {}).get("units", {}).get(unit) or []:
            if bool(row.get("start")) == instant or row.get("val") is None:
                continue
            key = row["end"] if instant else (row["start"], row["end"])
            if key not in seen or row.get("filed", "") > seen[key][1]:
                seen[key] = (row["val"], row.get("filed", ""))
        for key, value in seen.items():
            out.setdefault(key, value)
    return out


def _split_factor(splits, filed):
    """How many of today's shares one share was when a figure was filed."""
    factor = 1.0
    for split_date, ratio in splits:
        if filed and split_date.isoformat() > filed:
            factor *= ratio
    return factor


def _quarters(periods, flow):
    """Three-month figures by period end. A flow quarter missing from the filings (the fourth is never
    filed on its own, and cash flows come year to date) is one cumulative figure less an earlier one
    from the same start, or the year less its first three quarters."""
    out = {end: v for (start, end), v in periods.items() if QUARTER_DAYS[0] <= _days(start, end) <= QUARTER_DAYS[1]}
    if not flow:
        return out
    by_start = {}
    for (start, end), v in periods.items():
        if _days(start, end) <= YEAR_DAYS[1]:
            by_start.setdefault(start, []).append((end, v))
    for start, rows in by_start.items():
        rows.sort()
        for (prev_end, prev), (end, v) in zip(rows, rows[1:]):
            if end not in out and QUARTER_DAYS[0] <= _days(prev_end, end) <= QUARTER_DAYS[1]:
                out[end] = v - prev
    for (start, end), v in periods.items():
        if end in out or not YEAR_DAYS[0] <= _days(start, end) <= YEAR_DAYS[1]:
            continue
        inside = sorted(e for e in out if start < e < end and _days(start, e) > 60)
        if len(inside) == 3:
            out[end] = v - sum(out[e] for e in inside)
    return out


def parse_company_facts(data, splits=()):
    """The income statement, balance sheet and cash flow from EDGAR company facts, in the same shape as
    market.parse_timeseries:
    {"currency", "annual" | "quarterly" | "trailing": {line item: {ISO period end: value}}}. EBIT
    is pretax income plus interest expense (operating income where interest isn't reported), and
    EBITDA adds depreciation and amortization; the trailing twelve months add up the last four
    quarters when they run past the last fiscal year. Companies with no operating income (banks and
    insurers) have no EBIT. Balances are at each fiscal year's and quarter's end; total debt is current
    plus long-term debt (leases aren't included), and free cash flow is operating cash flow less capital
    expenditure. ``splits`` are [(date, ratio)], as Yahoo gives them."""
    facts = (data or {}).get("facts") or {}
    currency = _currency(facts)
    out = {"currency": currency, "annual": {}, "quarterly": {}, "trailing": {}, "source": "sec"}
    if not currency:
        return out
    balances = {}  # line item -> {date: value}, placed on the period ends below
    for key, (kind, names, *sign) in CONCEPTS.items():
        sign = sign[0] if sign else 1
        unit = {"flow": currency, "per_share": f"{currency}/shares", "shares": "shares",
                "instant": currency, "instant_shares": "shares"}[kind]
        instant = kind.startswith("instant")
        periods = _periods(facts, names, unit, instant)
        if kind not in ("flow", "instant"):
            power = -1 if kind == "per_share" else 1
            periods = {k: (v * _split_factor(splits, filed) ** power, filed) for k, (v, filed) in periods.items()}
        values = {k: v * sign for k, (v, _) in periods.items()}
        if kind in ("shares", "instant_shares") and values:  # now and then a filer tags a count in millions by mistake
            middle = sorted(values.values())[len(values) // 2]
            values = {k: v for k, v in values.items() if v > middle / 100}
        if instant:
            balances[key] = values
            continue
        annual = {end: v for (start, end), v in values.items() if YEAR_DAYS[0] <= _days(start, end) <= YEAR_DAYS[1]}
        quarterly = _quarters(values, kind == "flow")
        if annual:
            out["annual"][key] = annual
        if quarterly:
            out["quarterly"][key] = quarterly

    # Balances count at the end of each fiscal year and quarter the statements cover.
    for freq in ("annual", "quarterly"):
        ends = {d for series in out[freq].values() for d in series}
        for key, values in balances.items():
            found = {d: v for d, v in values.items() if d in ends}
            if found:
                out[freq][key] = found

    for freq in ("annual", "quarterly"):
        series = out[freq]
        pretax, interest, operating = (series.get(k, {}) for k in ("PretaxIncome", "InterestExpense", "OperatingIncome"))
        # Banks and insurers report no operating income, and their interest expense is a cost of
        # doing business rather than of financing, so they get no EBIT (as on Yahoo).
        ebit = {d: pretax[d] + interest[d] if d in pretax and d in interest else v for d, v in operating.items()}
        da = series.get("DepreciationAndAmortization", {})
        tax = series.get("TaxProvision", {})
        for key, values in (("EBIT", ebit), ("EBITDA", {d: v + da[d] for d, v in ebit.items() if d in da}),
                            ("TaxRateForCalcs", {d: tax[d] / v for d, v in pretax.items() if d in tax and v > 0})):
            if values:
                series[key] = values

        def fill(key, values):
            if values and key not in series:
                series[key] = values
            elif values:  # a line the filer reports for some periods only: fill the others
                series[key] = {**values, **series[key]}

        get = lambda key: series.get(key, {})  # noqa: E731
        cash, sti = get("CashAndCashEquivalents"), get("_ShortTermInvestments")
        fill("CashCashEquivalentsAndShortTermInvestments", {d: v + sti.get(d, 0) for d, v in cash.items()} if sti else {})
        goodwill, intangibles = get("_Goodwill"), get("_Intangibles")
        fill("GoodwillAndOtherIntangibleAssets", {d: goodwill.get(d, 0) + intangibles.get(d, 0) for d in {*goodwill, *intangibles}})
        assets, current_assets = get("TotalAssets"), get("CurrentAssets")
        fill("TotalNonCurrentAssets", {d: v - current_assets[d] for d, v in assets.items() if d in current_assets})
        both, equity = get("_LiabilitiesAndEquity"), get("_TotalEquity") or get("StockholdersEquity")
        fill("TotalLiabilitiesNetMinorityInterest", {d: v - equity[d] for d, v in both.items() if d in equity})
        current_debt, long_debt = get("CurrentDebt"), get("LongTermDebt")
        all_long_debt, long_current = get("_LongTermDebtTotal"), get("_LongTermDebtCurrent")
        debt = {}
        for d in {*current_debt, *long_debt, *all_long_debt}:
            if d in long_debt:  # the long-term part alone, so all current debt comes on top
                debt[d] = long_debt[d] + current_debt.get(d, 0)
            elif d in all_long_debt:  # a total that includes its current part: only other short-term debt comes on top
                debt[d] = all_long_debt[d] + max(current_debt.get(d, 0) - long_current.get(d, 0), 0)
            else:
                debt[d] = current_debt[d]
        fill("TotalDebt", debt)
        fill("NetDebt", {d: v - cash[d] for d, v in debt.items() if d in cash})
        current_liabilities = get("CurrentLiabilities")
        fill("WorkingCapital", {d: v - current_liabilities[d] for d, v in current_assets.items() if d in current_liabilities})
        holders = get("StockholdersEquity")
        fill("InvestedCapital", {d: v + debt[d] for d, v in holders.items() if d in debt})
        ocf, capex = get("OperatingCashFlow"), get("CapitalExpenditure")
        fill("FreeCashFlow", {d: v + capex[d] for d, v in ocf.items() if d in capex})
        for key in [k for k in series if k.startswith("_")]:
            del series[key]

    last_year = max((d for s in out["annual"].values() for d in s), default="")
    revenue = out["quarterly"].get("TotalRevenue") or out["quarterly"].get("NetIncome") or {}
    ends = sorted(revenue)[-4:]
    if len(ends) == 4 and ends[-1] > last_year and all(
            QUARTER_DAYS[0] <= _days(a, b) <= QUARTER_DAYS[1] for a, b in zip(ends, ends[1:])):
        for key, values in out["quarterly"].items():
            kind = CONCEPTS.get(key, ("flow",))[0]
            if key == "TaxRateForCalcs" or kind.startswith("instant") or key in DERIVED_BALANCES or not all(e in values for e in ends):
                continue
            total = sum(values[e] for e in ends)
            out["trailing"][key] = {ends[-1]: total / 4 if kind == "shares" else total}
        trailing = out["trailing"]
        if trailing.get("PretaxIncome", {}).get(ends[-1], 0) > 0 and "TaxProvision" in trailing:
            trailing["TaxRateForCalcs"] = {ends[-1]: trailing["TaxProvision"][ends[-1]] / trailing["PretaxIncome"][ends[-1]]}
    return out


def _words(name):
    stop = {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "the", "sa", "nv", "ag", "holdings", "group"}
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in stop]


def same_company(a, b):
    """Whether two company names look like the same company: "Shopify Inc." and "SHOPIFY INC.",
    but not TELUS (T on the TSX) and AT&T (T in New York)."""
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and wa[0] == wb[0]


class SecData:
    """EDGAR income statements with caching. ``contact`` is the email address the SEC asks requests to carry."""

    def __init__(self, contact, timeout=20):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"Finance-App {contact}"
        self.timeout = timeout
        self._tickers = None  # (fetched_at, {ticker: (cik, name)})
        self._facts = {}  # (cik, splits) -> (fetched_at, parsed income statement)
        self._lock = threading.Lock()

    def _get(self, url):
        r = self.session.get(url, timeout=self.timeout)
        if r.status_code == 404:
            return None
        if r.status_code in (403, 429):  # EDGAR turns away requests without a contact, and over 10 a second
            raise requests.HTTPError(f"HTTP {r.status_code}: EDGAR refused the request", response=r)
        r.raise_for_status()
        return r.json()

    def company(self, yahoo_symbol, name=None):
        """(CIK, SEC name) for a Yahoo symbol, or None. A symbol from another exchange (SHOP.TO) is
        matched on its ticker and only counts if the names agree."""
        if not self._tickers or time.time() - self._tickers[0] > TICKERS_TTL:
            tickers = {row["ticker"].upper(): (row["cik_str"], row["title"]) for row in (self._get(TICKERS_URL) or {}).values()}
            if not tickers:  # never keep an empty list: every company would look like a non-filer for a week
                raise ValueError("EDGAR's list of tickers came back empty")
            with self._lock:
                self._tickers = (time.time(), tickers)
        base, _, suffix = yahoo_symbol.upper().partition(".")
        found = self._tickers[1].get(base)
        if not found or (suffix and not same_company(name, found[1])):
            return None
        return found

    def income_statement(self, yahoo_symbol, name=None, splits=()):
        """The filed statements for a Yahoo symbol: {"statement": see parse_company_facts, or None;
        "status": "ok", "not_filed" (the company doesn't file with the SEC) or "unavailable" (EDGAR
        couldn't be reached; "detail" says why)}. Only the parsed statement is kept: the filings
        themselves run to several megabytes."""
        key = None
        try:
            found = self.company(yahoo_symbol, name)
            if not found:
                return {"statement": None, "status": "not_filed"}
            key = (found[0], tuple(splits))
            cached = self._facts.get(key)
            if cached and time.time() - cached[0] < FACTS_TTL:
                return {"statement": cached[1], "status": "ok" if cached[1] else "not_filed"}
            data = self._get(FACTS_URL.format(found[0]))
        except (requests.RequestException, ValueError) as exc:
            log.warning("EDGAR request for %s failed: %s", yahoo_symbol, exc)
            cached = self._facts.get(key) if key else None
            if cached and cached[1]:
                return {"statement": cached[1], "status": "ok"}
            return {"statement": None, "status": "unavailable", "detail": str(exc)}
        statement = parse_company_facts(data, splits) if data else None
        with self._lock:
            self._facts[key] = (time.time(), statement)
        return {"statement": statement, "status": "ok" if statement else "not_filed"}


def get_sec():
    """The app-wide EDGAR service, or None when SEC_CONTACT isn't set (tests can inject one via config["SEC"])."""
    app = current_app._get_current_object()
    if "SEC" in app.config:
        return app.config["SEC"]
    sec = app.extensions.get("sec")
    if sec is None and app.config.get("SEC_CONTACT"):
        sec = app.extensions["sec"] = SecData(app.config["SEC_CONTACT"])
    return sec
