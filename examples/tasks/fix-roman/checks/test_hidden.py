import unittest

from roman import to_int


class TestRoman(unittest.TestCase):
    def test_values(self):
        cases = {"I": 1, "III": 3, "IV": 4, "IX": 9, "XIV": 14, "XL": 40,
                 "XC": 90, "CD": 400, "CM": 900, "MCMXCIV": 1994, "MMXXVI": 2026}
        for numeral, value in cases.items():
            self.assertEqual(to_int(numeral), value, numeral)

    def test_lowercase(self):
        self.assertEqual(to_int("xiv"), 14)

    def test_invalid(self):
        for bad in ["ABC", "", "X1", "IIZ"]:
            with self.assertRaises(ValueError, msg=bad):
                to_int(bad)


if __name__ == "__main__":
    unittest.main()
