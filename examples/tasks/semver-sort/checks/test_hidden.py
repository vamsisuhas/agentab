import unittest

from semver import compare, parse, sort_versions


class TestSemver(unittest.TestCase):
    def test_spec_precedence_chain(self):
        chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                 "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0",
                 "1.0.1", "1.1.0", "2.0.0", "10.0.0"]
        for lower, higher in zip(chain, chain[1:]):
            self.assertEqual(compare(lower, higher), -1, f"{lower} < {higher}")
            self.assertEqual(compare(higher, lower), 1, f"{higher} > {lower}")

    def test_numeric_identifiers(self):
        self.assertEqual(compare("1.0.0-2", "1.0.0-10"), -1)
        self.assertEqual(compare("1.0.0-10", "1.0.0-a"), -1)
        self.assertEqual(compare("1.0.0-alpha.9", "1.0.0-alpha.10"), -1)

    def test_build_metadata_ignored(self):
        self.assertEqual(compare("1.0.0+build.1", "1.0.0+build.2"), 0)
        self.assertEqual(compare("1.0.0-alpha+001", "1.0.0-alpha"), 0)
        self.assertEqual(compare("1.0.0+20130313144700", "1.0.0-rc.1"), 1)

    def test_parse(self):
        self.assertEqual(parse("1.2.3"), (1, 2, 3, None, None))
        self.assertEqual(parse("1.0.0-alpha.1+exp.sha.5114f85"),
                         (1, 0, 0, "alpha.1", "exp.sha.5114f85"))

    def test_valid_edge_cases(self):
        for v in ["0.0.0", "1.2.3-0", "1.2.3-x-y-z.--", "1.0.0-0A.is.legal",
                  "1.1.2+meta-valid", "1.0.0-rc.1+build.123"]:
            parse(v)

    def test_invalid(self):
        for v in ["", "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "v1.2.3",
                  "1.2.3-", "1.2.3+", "1.2.3-01", "1.2.3-alpha..1", "1.2.3-alpha_beta",
                  "1.2.3+build..1", " 1.2.3", "-1.2.3", "1.2.3-+x"]:
            with self.assertRaises(ValueError, msg=repr(v)):
                parse(v)
        with self.assertRaises(ValueError):
            compare("1.2", "1.2.3")

    def test_sort_is_stable(self):
        versions = ["1.0.0", "1.0.0-rc.1", "0.9.0", "1.0.0+b", "1.0.0-alpha", "1.0.0+a"]
        self.assertEqual(sort_versions(versions),
                         ["0.9.0", "1.0.0-alpha", "1.0.0-rc.1", "1.0.0", "1.0.0+b", "1.0.0+a"])
        self.assertEqual(versions[0], "1.0.0")  # input list is not modified


if __name__ == "__main__":
    unittest.main()
