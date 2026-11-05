import unittest

from durations import format_duration, parse_duration


class TestParse(unittest.TestCase):
    def test_valid(self):
        cases = {"1h30m": 5400, "2d 4h": 187200, "90s": 90, "1D2h3M4s": 93784,
                 "90m": 5400, "  45m  ": 2700, "1d 2h 3m 4s": 93784, "0s": 0,
                 "100h": 360000, "1d4s": 86404}
        for text, seconds in cases.items():
            self.assertEqual(parse_duration(text), seconds, repr(text))

    def test_invalid(self):
        for text in ["", "   ", "10", "1x", "-5m", "1.5h", "h", "1 h", "30m1h", "1h1h",
                     "1s2m", "1hh", "1h 30", "d1"]:
            with self.assertRaises(ValueError, msg=repr(text)):
                parse_duration(text)


class TestFormat(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(format_duration(93784), "1d 2h 3m 4s")
        self.assertEqual(format_duration(3600), "1h")
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(86460), "1d 1m")
        self.assertEqual(format_duration(59), "59s")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            format_duration(-1)
        for bad in (1.5, "60", None):
            with self.assertRaises(TypeError, msg=repr(bad)):
                format_duration(bad)

    def test_round_trip(self):
        for n in [0, 1, 59, 60, 61, 3599, 3600, 86399, 86400, 93784, 10**7]:
            self.assertEqual(parse_duration(format_duration(n)), n)


if __name__ == "__main__":
    unittest.main()
