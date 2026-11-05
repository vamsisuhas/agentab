from decimal import Decimal

CODES = {
    "SAVE10": ("percent", 10),
    "SAVE25": ("percent", 25),
    "FIVEOFF": ("fixed", 5),
    "TENOFF": ("fixed", 10),
}


def apply_discounts(amount, codes):
    amount = Decimal(amount)
    for code in codes:
        kind, value = CODES[code]
        if kind == "percent":
            amount = amount * (Decimal(100) - value) / Decimal(100)
    for code in codes:
        kind, value = CODES[code]
        if kind == "fixed":
            amount = amount - value
    return max(amount, Decimal("0"))
