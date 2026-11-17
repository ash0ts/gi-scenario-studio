#!/usr/bin/env python3
"""Editorial verifier regressions; deterministic and read-only."""

import copy
import json
import unittest

from verify import ARTICLE_ROOT, GUIDED, MANIFEST, SUMMARY, ArticleMarkup, check_prose, check_tables


class ArticleEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.article = (ARTICLE_ROOT / "article.md").read_text()
        cls.methods = (ARTICLE_ROOT / "methods.md").read_text()
        cls.summary = json.loads(SUMMARY.read_text())
        cls.guided = json.loads(GUIDED.read_text())
        cls.manifest = json.loads(MANIFEST.read_text())

    def tables(self, article=None, methods=None, manifest=None):
        check_tables(
            ArticleMarkup(self.article if article is None else article),
            ArticleMarkup(self.methods if methods is None else methods),
            self.summary,
            self.guided,
            self.manifest if manifest is None else manifest,
        )

    def test_current_tables(self):
        self.tables()

    def test_harmless_whitespace(self):
        self.tables(article=self.article.replace(">Passes<", ">  Passes  <"))

    def test_wrong_volume(self):
        with self.assertRaisesRegex(ValueError, "Volume result"):
            self.tables(article=self.article.replace("<td>4.14% reduction", "<td>4.15% reduction"))

    def test_omitted_second_peak(self):
        with self.assertRaisesRegex(ValueError, "peak increase"):
            self.tables(article=self.article.replace("<br>Stress event: 13.33 → 13.34 cfs", ""))

    def test_wrong_disposition(self):
        with self.assertRaisesRegex(ValueError, "wrong disposition"):
            self.tables(article=self.article.replace("<td>Fails</td>", "<td>Passes</td>", 1))

    def test_wrong_behavior_denominator(self):
        with self.assertRaisesRegex(ValueError, "denominator drift"):
            self.tables(article=self.article.replace("<td>5/12</td>", "<td>5/24</td>"))

    def test_wrong_cost_conversion(self):
        with self.assertRaisesRegex(ValueError, "Cost conversion"):
            self.tables(article=self.article.replace("<td>$54,990</td>", "<td>$54.99</td>"))

    def test_missing_accessible_caption(self):
        with self.assertRaisesRegex(ValueError, "caption"):
            self.tables(article=self.article.replace("<caption>ALT-017 against the four teaching rules</caption>", ""))

    def test_wrong_diagnostic(self):
        with self.assertRaisesRegex(ValueError, "Token diagnostic"):
            self.tables(methods=self.methods.replace("<td>11/36</td>", "<td>12/36</td>"))

    def test_stale_projection(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["table_claims"]["alt017-rules"]["rules"][0]["actual"] = 4.15
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.tables(manifest=manifest)

    def test_missing_recovery_accounting(self):
        with self.assertRaisesRegex(ValueError, "Recovery scope"):
            check_prose(self.article, self.methods.replace("0 new provider calls", "new calls"), self.summary)


if __name__ == "__main__":
    unittest.main()

