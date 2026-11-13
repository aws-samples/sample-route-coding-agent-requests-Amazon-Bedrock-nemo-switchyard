import unittest

from slug import slugify


class SlugifyTest(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_collapses_spaces(self):
        self.assertEqual(slugify("  many   spaces here "), "many-spaces-here")

    def test_drops_punctuation(self):
        self.assertEqual(slugify("Rock & Roll, Baby!"), "rock-roll-baby")

    def test_empty(self):
        self.assertEqual(slugify("   "), "")
