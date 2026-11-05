CODES = {
    "SAVE10": ("percent", 10),
    "SAVE25": ("percent", 25),
    "FIVEOFF": ("fixed", 5),
    "TENOFF": ("fixed", 10),
}


def apply_discounts(amount, codes):
    for code in codes:
        kind, value = CODES[code]
        if kind == "percent":
            amount = amount * (1 - value / 100)
        else:
            amount = amount - value
    return amount
