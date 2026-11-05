import unittest

from text import slugify


class TestSlugify(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("  Many   spaces  "), "many-spaces")
        self.assertEqual(slugify("Café Crème"), "cafe-creme")
        self.assertEqual(slugify("--Already-slugged--"), "already-slugged")
        self.assertEqual(slugify("C++ & Python 3.12"), "c-python-3-12")

    def test_more(self):
        self.assertEqual(slugify("Ünïcödé Têxt"), "unicode-text")
        self.assertEqual(slugify("a___b"), "a-b")
        self.assertEqual(slugify("!!!"), "")


if __name__ == "__main__":
    unittest.main()
