import unittest

from mathutils import average, median


class TestMathutils(unittest.TestCase):
    def test_average(self):
        self.assertEqual(average([1, 2, 3]), 2)
        self.assertEqual(average([]), 0.0)

    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)
        self.assertEqual(median([10, 20]), 15)


if __name__ == "__main__":
    unittest.main()
