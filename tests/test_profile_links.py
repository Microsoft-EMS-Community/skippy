# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Authors' profile links: only real profiles on LinkedIn, X, YouTube and
# GitHub get into the feed, whichever way members type them.

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import profile_links  # noqa: E402
from profile_links import normalize  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_links_and_handles_become_one_standard_link(self):
        cases = {
            "linkedin": ["https://www.linkedin.com/in/jonas-b/", "dk.linkedin.com/in/jonas-b?trk=public",
                         "linkedin.com/in/jonas-b", "jonas-b"],
            "x": ["https://twitter.com/jonasb", "x.com/jonasb/", "@jonasb", "jonasb", "mobile.twitter.com/jonasb"],
            "youtube": ["https://www.youtube.com/@sky.made-simple", "m.youtube.com/@sky.made-simple",
                        "@sky.made-simple"],
            "github": ["https://github.com/JonasBogvad/", "github.com/JonasBogvad", "JonasBogvad", "@JonasBogvad"],
        }
        want = {"linkedin": "https://www.linkedin.com/in/jonas-b", "x": "https://x.com/jonasb",
                "youtube": "https://www.youtube.com/@sky.made-simple", "github": "https://github.com/JonasBogvad"}
        for site, typed in cases.items():
            for value in typed:
                self.assertEqual(normalize(site, value), want[site], (site, value))
        self.assertEqual(normalize("youtube", "youtube.com/channel/UC1234567890123456789012"),
                         "https://www.youtube.com/channel/UC1234567890123456789012")

    def test_anything_but_a_profile_on_that_site_is_refused(self):
        refused = [("linkedin", "https://linkedin.com/company/microsoft"), ("linkedin", "https://evil.com/in/me"),
                   ("linkedin", "https://linkedin.com.evil.com/in/me"), ("x", "https://x.com/i/status/1"),
                   ("x", "https://x.com/home"), ("x", "https://example.com/jonasb"),
                   ("youtube", "https://youtube.com/watch?v=abc"), ("github", "https://github.com/orgs/x"),
                   ("github", "https://github.com/owner/repo"), ("github", "javascript:alert(1)"),
                   ("x", "https://youtube.com/@me")]
        for site, value in refused:
            with self.assertRaises(ValueError, msg=(site, value)):
                normalize(site, value)

    def test_the_error_says_what_to_type(self):
        with self.assertRaisesRegex(ValueError, r"an X profile; use x.com/yourhandle or @yourhandle"):
            normalize("x", "https://x.com/home")

    def test_line_in_a_fixed_order_without_preview_cards(self):
        links = {"github": "https://github.com/a", "linkedin": "https://www.linkedin.com/in/a"}
        self.assertEqual(profile_links.line(links, {"linkedin": "💼"}),
                         "💼 [LinkedIn](<https://www.linkedin.com/in/a>) · 🔗 [GitHub](<https://github.com/a>)")
        self.assertEqual(profile_links.line({}, {}), "")


if __name__ == "__main__":
    unittest.main()
