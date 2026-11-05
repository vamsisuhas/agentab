import unittest

from money import allocate


class TestAllocate(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(allocate(100, [1, 1, 1]), [34, 33, 33])
        self.assertEqual(allocate(5, [3, 7]), [2, 3])
        self.assertEqual(allocate(1, [1, 1, 1]), [1, 0, 0])
        self.assertEqual(allocate(10, [1] * 7), [2, 2, 2, 1, 1, 1, 1])
        self.assertEqual(allocate(7, [1, 2, 4]), [1, 2, 4])

    def test_largest_remainder_not_first(self):
        # exact shares: 1.0, 4.4, 4.6 -> the leftover cent goes to the 0.6 remainder
        self.assertEqual(allocate(10, [10, 44, 46]), [1, 4, 5])
        self.assertEqual(allocate(100, [1, 2]), [33, 67])

    def test_zero_and_floats(self):
        self.assertEqual(allocate(0, [1, 2]), [0, 0])
        self.assertEqual(allocate(10, [0, 1]), [0, 10])
        self.assertEqual(allocate(100, [0.5, 0.25, 0.25]), [50, 25, 25])

    def test_negative_total(self):
        self.assertEqual(allocate(-100, [1, 1, 1]), [-34, -33, -33])
        self.assertEqual(allocate(-5, [3, 7]), [-2, -3])

    def test_always_sums(self):
        for total in [1, 2, 3, 99, 101, 1000, 12345]:
            for ratios in ([1, 1, 1], [3, 3, 3, 1], [0.1, 0.2, 0.7], [5, 0, 5]):
                self.assertEqual(sum(allocate(total, ratios)), total, (total, ratios))

    def test_invalid(self):
        for ratios in ([], [0, 0], [-1, 2]):
            with self.assertRaises(ValueError, msg=ratios):
                allocate(100, ratios)
        for total in (10.5, "100", None):
            with self.assertRaises(TypeError, msg=total):
                allocate(total, [1, 1])


if __name__ == "__main__":
    unittest.main()
