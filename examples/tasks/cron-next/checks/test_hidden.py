import unittest
from datetime import datetime as dt

from cron import next_run


class TestNextRun(unittest.TestCase):
    def check(self, expression, after, expected):
        self.assertEqual(next_run(expression, after), expected, f"{expression!r} after {after}")

    def test_basic_fields(self):
        self.check("*/15 * * * *", dt(2026, 1, 1, 10, 7, 30), dt(2026, 1, 1, 10, 15))
        self.check("5,10-12 * * * *", dt(2026, 5, 5, 5, 10), dt(2026, 5, 5, 5, 11))
        self.check("0 */6 * * *", dt(2026, 5, 5, 18, 0), dt(2026, 5, 6, 0, 0))
        self.check("10-50/20 8 * * *", dt(2026, 5, 5, 8, 31), dt(2026, 5, 5, 8, 50))

    def test_weekdays(self):
        self.check("0 9 * * 1-5", dt(2026, 10, 16, 17, 0), dt(2026, 10, 19, 9, 0))
        self.check("0 0 * * 0", dt(2026, 10, 14, 8, 0), dt(2026, 10, 18, 0, 0))
        self.check("0 0 * * 7", dt(2026, 10, 18, 0, 0), dt(2026, 10, 25, 0, 0))
        self.check("15 10 * 3,6 1", dt(2026, 2, 10), dt(2026, 3, 2, 10, 15))

    def test_strictly_after(self):
        self.check("59 23 31 12 *", dt(2026, 12, 31, 23, 59), dt(2027, 12, 31, 23, 59))
        self.check("0 0 1 1 *", dt(2026, 12, 31, 23, 59, 59, 999999), dt(2027, 1, 1, 0, 0))

    def test_month_rollover_and_leap_day(self):
        self.check("30 2 1 * *", dt(2026, 1, 31, 12, 0), dt(2026, 2, 1, 2, 30))
        self.check("0 0 29 2 *", dt(2026, 3, 1), dt(2028, 2, 29, 0, 0))

    def test_day_of_month_or_day_of_week(self):
        # Both restricted: the 13th OR any Friday.
        self.check("0 12 13 * 5", dt(2026, 10, 1), dt(2026, 10, 2, 12, 0))
        self.check("0 12 13 * 5", dt(2026, 10, 9, 12, 0), dt(2026, 10, 13, 12, 0))

    def test_invalid(self):
        for expression in ["60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                           "* * * * 8", "* * * *", "* * * * * *", "a * * * *",
                           "*/0 * * * *", "5-1 * * * *", "1,,2 * * * *", "0 0 30 2 *"]:
            with self.assertRaises(ValueError, msg=repr(expression)):
                next_run(expression, dt(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
