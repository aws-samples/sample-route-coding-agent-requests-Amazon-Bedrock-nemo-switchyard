import ast
import builtins
import unittest
from unittest import mock

from calc import CalcError, evaluate


class CalcTest(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(evaluate("2 + 3 * 4"), 14)
        self.assertEqual(evaluate("(2 + 3) * 4"), 20)
        self.assertEqual(evaluate("10 - 4 - 3"), 3)
        self.assertEqual(evaluate("2 * 7 % 4"), 2)

    def test_power(self):
        self.assertEqual(evaluate("2 ** 3 ** 2"), 512)
        self.assertEqual(evaluate("-2 ** 2"), -4)
        self.assertEqual(evaluate("(-2) ** 2"), 4)
        self.assertEqual(evaluate("2 ** -1"), 0.5)

    def test_unary(self):
        self.assertEqual(evaluate("--3"), 3)
        self.assertEqual(evaluate("-+-3"), 3)
        self.assertEqual(evaluate("4 * -2"), -8)

    def test_numbers(self):
        self.assertEqual(evaluate("1.5e3 + .5"), 1500.5)
        self.assertEqual(evaluate("2E-2"), 0.02)

    def test_variables_and_functions(self):
        self.assertEqual(evaluate("rate * hours", {"rate": 12.5, "hours": 4}), 50)
        self.assertEqual(evaluate("max(1, min(x, 3), -5)", {"x": 2}), 2)
        self.assertEqual(evaluate("abs(-3 - 4) * 2"), 14)

    def test_errors(self):
        for bad in ["", "2 +", "(1 + 2", "1 + 2)", "3 $ 4", "1..2", "max()", "abs(1, 2)", "foo(1)", "1 2"]:
            with self.subTest(bad=bad), self.assertRaises(CalcError):
                evaluate(bad)
        with self.assertRaises(CalcError):
            evaluate("y + 1")
        with self.assertRaises(CalcError):
            evaluate("1 / (2 - 2)")
        with self.assertRaises(CalcError):
            evaluate("5 % 0")

    def test_no_eval(self):
        with mock.patch.object(builtins, "eval", side_effect=AssertionError("eval used")), \
                mock.patch.object(builtins, "compile", side_effect=AssertionError("compile used")), \
                mock.patch.object(ast, "parse", side_effect=AssertionError("ast used")):
            self.assertEqual(evaluate("1 + 2 * 3"), 7)
