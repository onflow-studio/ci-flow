import os
import re
import unittest

PAGE = os.path.join(os.path.dirname(__file__), "..", "ciflow", "web", "index.html")


class TestFavicon(unittest.TestCase):
    def test_data_uri_has_no_raw_hash(self):
        # A raw '#' starts the URL fragment and truncates the SVG.
        with open(PAGE) as f:
            html = f.read()
        m = re.search(r'rel="icon"[^>]*href="(data:image/svg\+xml,[^"]+)"', html)
        self.assertIsNotNone(m)
        self.assertNotIn("#", m.group(1))


if __name__ == "__main__":
    unittest.main()
