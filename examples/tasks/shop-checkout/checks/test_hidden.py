import unittest
from decimal import Decimal

from shop import Cart


def cart_with(items, codes=(), tax_rate="0"):
    cart = Cart(tax_rate=tax_rate)
    for price, quantity in items:
        cart.add("item", price, quantity)
    for code in codes:
        cart.apply_code(code)
    return cart


class TestCheckout(unittest.TestCase):
    def test_reported_examples(self):
        self.assertEqual(cart_with([("100.00", 1)], ["FIVEOFF", "SAVE10"]).total(), Decimal("85.00"))
        self.assertEqual(cart_with([("2.50", 1)], tax_rate="0.07").total(), Decimal("2.68"))
        self.assertEqual(cart_with([("3.00", 1)], ["FIVEOFF"]).total(), Decimal("0.00"))

    def test_returns_decimal_cents(self):
        total = cart_with([("19.99", 3)], tax_rate="0.0825").total()
        self.assertIsInstance(total, Decimal)
        self.assertEqual(total, Decimal("64.92"))  # 59.97 * 1.0825 = 64.917525
        self.assertEqual(total.as_tuple().exponent, -2)

    def test_percent_codes_compound_before_fixed(self):
        self.assertEqual(cart_with([("100.00", 1)], ["SAVE10", "SAVE25"]).total(), Decimal("67.50"))
        self.assertEqual(cart_with([("100.00", 1)], ["TENOFF", "SAVE25", "FIVEOFF"]).total(),
                         Decimal("60.00"))

    def test_tax_after_discount(self):
        self.assertEqual(cart_with([("50.00", 2)], ["SAVE10"], tax_rate="0.10").total(),
                         Decimal("99.00"))

    def test_rounds_once_half_up(self):
        # 0.10 * 3 = 0.30; with 5% tax = 0.315 -> 0.32
        self.assertEqual(cart_with([("0.10", 3)], tax_rate="0.05").total(), Decimal("0.32"))
        # 1.005 must round to 1.01, not 1.00
        self.assertEqual(cart_with([("1.005", 1)]).total(), Decimal("1.01"))

    def test_never_negative(self):
        self.assertEqual(cart_with([("8.00", 1)], ["TENOFF", "FIVEOFF"]).total(), Decimal("0.00"))
        self.assertEqual(cart_with([], ["FIVEOFF"], tax_rate="0.07").total(), Decimal("0.00"))

    def test_codes(self):
        cart = cart_with([("100.00", 1)])
        with self.assertRaises(ValueError):
            cart.apply_code("BOGUS")
        cart.apply_code("SAVE10")
        cart.apply_code("SAVE10")
        cart.apply_code("FIVEOFF")
        cart.apply_code("FIVEOFF")
        self.assertEqual(cart.total(), Decimal("85.00"))


if __name__ == "__main__":
    unittest.main()
