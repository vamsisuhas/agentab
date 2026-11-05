from decimal import ROUND_HALF_UP, Decimal

from .discounts import CODES, apply_discounts
from .tax import add_tax


class Cart:
    def __init__(self, tax_rate="0"):
        self.items = []
        self.codes = []
        self.tax_rate = tax_rate

    def add(self, name, price, quantity=1):
        """price is a string like "19.99"."""
        self.items.append((name, price, quantity))

    def apply_code(self, code):
        if code not in CODES:
            raise ValueError(f"unknown discount code: {code}")
        if code not in self.codes:
            self.codes.append(code)

    def subtotal(self):
        return sum((Decimal(price) * quantity for _, price, quantity in self.items),
                   Decimal("0"))

    def total(self):
        discounted = apply_discounts(self.subtotal(), self.codes)
        return add_tax(discounted, self.tax_rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
