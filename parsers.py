import csv
import io
import re
from datetime import date as _date
from dateutil import parser as dateparser

CATEGORY_RULES = [
    (["interac etrnsfr recvd", "etransfer received", "e-transfer received"], "Income", "income"),
    (["payday", "payroll", "salary", "direct dep", "employment insurance",
      "assurance-emploi", "assurance emploi"], "Income", "income"),
    (["rent", "mortgage", "property management", "strata"], "Housing", "expense"),
    (["supersto", "no frills", "nofrills", "metro", "walmart", "loblaws", "sobeys",
      "food basics", "costco", "maxi", "iga", "provigo", "farm boy", "whole foods",
      "freshco", "zehrs", "valumart", "safeway", "co-op grocery", "co-op food"], "Groceries", "expense"),
    (["tim hortons", "starbucks", "second cup", "coffee", "cafe", "caffè"], "Coffee", "expense"),
    (["netflix", "spotify", "disney", "prime video", "apple one", "youtube premium",
      "crave", "paramount", "hbo"], "Subscriptions", "expense"),
    (["petro-canada", "petro canada", "esso", "shell", "husky", "irving oil",
      "ultramar", "chevron", "sunoco", "gas station"], "Transportation", "expense"),
    (["noodlebox", "restaurant", "sushi", "pizza", "mcdonald", "burger king", "wendy",
      "subway", "pho", "thai", "chinese", "indian", "a&w", "kfc", "popeyes", "chipotle",
      "five guys", "harveys", "boston pizza", "east side mario", "swiss chalet",
      "the keg", "jugo juice", "panera", "nando", "cactus club", "avesta", "panda"], "Dining", "expense"),
    (["rogers", "bell", "telus", "fido", "virgin mobile", "koodo", "freedom mobile",
      "wind mobile", "public mobile", "service charge", "service fee",
      "overdraft", "frais de service", "insufficient funds"], "Bills", "expense"),
    (["fit4less", "goodlife", "ymca", "anytime fitness", "gym", "fitness",
      "la fitness", "planet fitness"], "Health", "expense"),
    (["insurance", "intact", "desjardins", "aviva", "td insurance",
      "belairdirect", "wawanesa"], "Insurance", "expense"),
    (["interac etrnsfr sent", "scotialine", "capital one-mc", "bmo loc",
      "e-transfer", "interac e-tran", "wire transfer", " tf ",
      "scotia visa",
      "payment thank you", "automatic payment", "customer payment", "credit card payment",
      "internet banking payment", "preauthorized payment"], "Transfer", "transfer"),
    (["lcbo", "saq", "beer store", "wine rack", "liquor", "co-op wines"], "Alcohol", "expense"),
    (["shoppers drug", "rexall", "pharmacy", "drug mart", "london drugs", "sephora"], "Pharmacy", "expense"),
    (["amazon", "best buy", "bestbuy", "apple.com", "microsoft", "hudson's bay",
      "the bay", "winners", "homesense", "marshalls", "dollarama", "ikea",
      "canadian tire", "rocket fizz"], "Shopping", "expense"),
    (["hydro", "electricity", "enbridge", "gas bill", "atco", "fortis",
      "alectra", "toronto hydro", "bc hydro"], "Utilities", "expense"),
    (["7 eleven", "convenience", "cordial"], "Convenience", "expense"),
]


def normalize_merchant_key(description: str) -> str:
    """Collapse a raw transaction description to a stable merchant key by dropping
    digits/punctuation (store numbers, dates, card fragments) that vary between visits
    to the same merchant."""
    key = re.sub(r'[^a-z\s]', ' ', description.lower())
    return re.sub(r'\s+', ' ', key).strip()


def categorize(description: str, amount: float = 0):
    desc_lower = description.lower()
    for keywords, category, txn_type in CATEGORY_RULES:
        if any(kw in desc_lower for kw in keywords):
            return category, txn_type
    # Unrecognized description: use the sign of the amount as the fallback
    return "Other", "income" if amount > 0 else "expense"


def _clean_amount(value: str) -> float:
    v = value.strip().replace(",", "").replace("$", "").replace('"', "")
    return float(v) if v else 0.0


def _parse_date(value: str):
    value = value.strip()
    # YYYYMMDD with no separators (BMO real format)
    if len(value) == 8 and value.isdigit():
        return _date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    return dateparser.parse(value).date()


_PREFIX_LABELS = {
    "IN": "Interest",
    "SC": "Service Charge",
    "DS": "Direct Service",
    "PR": "Purchase",
    "CW": "Transfer",
    "RN": "Return",
    "IB": "Internet Banking",
    "OP": "Online Payment",
    "BC": "Bank Charge",
}


def _clean_description(raw: str) -> str:
    """Strip BMO prefix codes like [PR], [CW], [DS], [OP], [BC] and collapse whitespace.
    If nothing remains after stripping the prefix, use a readable label for that code."""
    raw = raw.strip()
    m = re.match(r'^\[([A-Z]+)\]\s*', raw)
    if m:
        prefix = m.group(1)
        rest = re.sub(r'\s{2,}', ' ', raw[m.end():]).strip()
        return rest if rest else _PREFIX_LABELS.get(prefix, prefix)
    return re.sub(r'\s{2,}', ' ', raw).strip()


def _find_header_line(text: str, markers: list[str]) -> str:
    """
    Skip metadata/blank lines before the real CSV header.
    Returns the text starting from the header line.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if any(m.lower() in line.lower() for m in markers):
            return '\n'.join(lines[i:])
    return text  # fallback: use as-is


def _iter_rows(text: str):
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        yield {k.strip().lstrip('﻿'): v.strip() for k, v in row.items() if k}


def parse_bmo(text: str) -> list[dict]:
    """
    Real BMO export format:
      - Has metadata lines at the top (skip until 'Transaction Type' header)
      - Headers: First Bank Card, Transaction Type, Date Posted, Transaction Amount, Description
      - Date: YYYYMMDD
      - Amount: single signed column (negative = debit, positive = credit)
      - Description has [XX] prefix codes and trailing padding spaces
    """
    csv_text = _find_header_line(text, ["Transaction Type", "Date Posted"])
    rows = []
    for row in _iter_rows(csv_text):
        date_str = row.get("Date Posted") or row.get("Date") or ""
        raw_desc = row.get("Description", "")
        amount_str = row.get("Transaction Amount", "")

        # Also handle older split Debit/Credit format
        if not amount_str:
            debit = _clean_amount(row.get("Debit") or row.get("Withdrawals") or "")
            credit = _clean_amount(row.get("Credit") or row.get("Deposits") or "")
            amount = round(credit - debit, 2)
        else:
            try:
                amount = round(_clean_amount(amount_str), 2)
            except ValueError:
                continue

        description = _clean_description(raw_desc)
        if not date_str or not description:
            continue

        try:
            txn_date = _parse_date(date_str)
        except Exception:
            continue

        category, txn_type = categorize(description, amount)
        rows.append({"date": txn_date, "description": description, "amount": amount,
                     "category": category, "type": txn_type})
    return rows


def parse_scotiabank(text: str) -> list[dict]:
    """
    Scotiabank export. Handles two formats:
      1. Chequing: Date, Description, Withdrawals, Deposits, Total Balance
      2. ScotiaLine LOC: Filter, Date, Description, Sub-description, Status,
         Type of Transaction, Amount
         (Amount is positive for debits/charges, negative for credits/payments —
          negate to get our sign convention)
    """
    csv_text = _find_header_line(text, ["Withdrawals", "Deposits", "Date Posted", "Transaction Type"])
    rows = []
    for row in _iter_rows(csv_text):
        date_str = row.get("Date") or row.get("Date Posted") or ""
        description = row.get("Description", "").strip('"').strip()
        # Append sub-description when present (ScotiaLine format)
        sub = row.get("Sub-description", "").strip('"').strip()
        if sub:
            description = f"{description} {sub}".strip()

        txn_amount_str = row.get("Transaction Amount", "")
        raw_amount_str = row.get("Amount", "")

        if txn_amount_str:
            try:
                amount = round(_clean_amount(txn_amount_str), 2)
            except ValueError:
                continue
        elif raw_amount_str:
            # ScotiaLine LOC: positive = charge (debit), negative = payment (credit)
            # Negate → our convention: negative = expense, positive = income
            try:
                amount = round(-_clean_amount(raw_amount_str), 2)
            except ValueError:
                continue
        else:
            debit = _clean_amount(row.get("Withdrawals") or row.get("Debit") or "")
            credit = _clean_amount(row.get("Deposits") or row.get("Credit") or "")
            amount = round(credit - debit, 2)

        if not date_str or not description:
            continue

        try:
            txn_date = _parse_date(date_str)
        except Exception:
            continue

        category, txn_type = categorize(description, amount)
        rows.append({"date": txn_date, "description": description, "amount": amount,
                     "category": category, "type": txn_type})
    return rows


def parse_capital_one(text: str) -> list[dict]:
    """
    Capital One credit card CSV.
    Headers: Transaction Date, Posted Date, Card No., Description, Category, Debit, Credit
    Debit = charge (positive number, money out), Credit = payment/refund (positive number, money in).
    """
    csv_text = _find_header_line(text, ["Transaction Date", "Card No"])
    rows = []
    for row in _iter_rows(csv_text):
        date_str = row.get("Transaction Date") or row.get("Date") or ""
        description = row.get("Description", "").strip('"').strip()
        if not date_str or not description:
            continue

        debit = _clean_amount(row.get("Debit") or "")
        credit = _clean_amount(row.get("Credit") or "")
        amount = round(credit - debit, 2)

        try:
            txn_date = _parse_date(date_str)
        except Exception:
            continue

        category, txn_type = categorize(description, amount)
        rows.append({"date": txn_date, "description": description, "amount": amount,
                     "category": category, "type": txn_type})
    return rows


def parse_desjardins(text: str) -> list[dict]:
    """
    Desjardins chequing CSV export (no header row — fixed column positions).
      [3] Date (YYYY/MM/DD)
      [5] Description
      [7] Withdrawal/debit (positive = money out; negative = reversal)
      [8] Deposit/credit (positive = money in)
    Amount = deposit - withdrawal.
    """
    rows = []
    for cols in csv.reader(io.StringIO(text)):
        if len(cols) < 9:
            continue
        date_str = cols[3].strip()
        description = cols[5].strip()
        debit_str = cols[7].strip()
        credit_str = cols[8].strip()
        if not date_str or not description:
            continue
        try:
            debit = _clean_amount(debit_str) if debit_str else 0.0
            credit = _clean_amount(credit_str) if credit_str else 0.0
            amount = round(credit - debit, 2)
            txn_date = _parse_date(date_str)
        except Exception:
            continue
        if amount == 0:
            continue
        category, txn_type = categorize(description, amount)
        rows.append({"date": txn_date, "description": description, "amount": amount,
                     "category": category, "type": txn_type})
    return rows


def parse_loc(text: str) -> list[dict]:
    """
    Line of Credit CSV export.
    Headers: Item #, Card #, Transaction Date, Posting Date, Transaction Amount, Description
    First line is metadata (skip). Date: YYYYMMDD. BOM-prefixed file.
    Amount sign: positive = charge (increases debt), negative = payment received (reduces debt).
    Negate to match app convention: negative = expense, positive = income.
    """
    csv_text = _find_header_line(text, ["Transaction Date", "Posting Date"])
    rows = []
    for row in _iter_rows(csv_text):
        date_str = row.get("Transaction Date") or row.get("Posting Date") or ""
        description = row.get("Description", "").strip()
        amount_str = row.get("Transaction Amount", "")
        if not date_str or not description or not amount_str:
            continue
        try:
            # Negate: CSV positive = charge (expense), CSV negative = payment (income)
            amount = round(-_clean_amount(amount_str), 2)
            txn_date = _parse_date(date_str)
        except Exception:
            continue
        category, txn_type = categorize(description, amount)
        rows.append({"date": txn_date, "description": description, "amount": amount,
                     "category": category, "type": txn_type})
    return rows


PARSERS = {
    "bmo": parse_bmo,
    "scotiabank": parse_scotiabank,
    "capital_one": parse_capital_one,
    "loc": parse_loc,
    "desjardins": parse_desjardins,
}


def parse_csv(bank: str, text: str) -> list[dict]:
    parser = PARSERS.get(bank)
    if not parser:
        raise ValueError(f"Unknown bank: {bank!r}. Must be one of: {list(PARSERS)}")
    return parser(text)
