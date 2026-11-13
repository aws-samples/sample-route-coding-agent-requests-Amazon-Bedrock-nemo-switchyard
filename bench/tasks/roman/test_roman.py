import unittest

from roman import to_roman


class RomanTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(to_roman(3), "III")
        self.assertEqual(to_roman(8), "VIII")

    def test_subtractive(self):
        self.assertEqual(to_roman(4), "IV")
        self.assertEqual(to_roman(944), "CMXLIV")

    def test_large(self):
        self.assertEqual(to_roman(3999), "MMMCMXCIX")

    def test_out_of_range(self):
        for bad in (0, 4000, -1):
            with self.assertRaises(ValueError):
                to_roman(bad)
