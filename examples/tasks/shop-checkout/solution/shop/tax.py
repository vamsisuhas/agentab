from decimal import Decimal


def add_tax(amount, rate):
    return Decimal(amount) * (1 + Decimal(str(rate)))
