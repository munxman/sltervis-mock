"""Offline tests for sltk_check parsers. Run: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import sltk_check as sc  # noqa: E402


def fx(name: str) -> str:
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return f.read()


GOOD = fx("good_et.html")
BASE = "https://example.ee/"
PAGE = "https://example.ee/registreerumine.html"


class TestHttpAndText(unittest.TestCase):
    def test_complete_html(self):
        self.assertTrue(sc.html_complete(GOOD))
        self.assertFalse(sc.html_complete(fx("truncated.html")))

    def test_check_http_truncated_fails(self):
        raw = fx("truncated.html")
        r = sc.check_http({"error": None, "status": 200, "text": raw,
                           "content": raw.encode(), "final_url": PAGE})
        self.assertEqual(r["status"], sc.FAIL)

    def test_visible_text_excludes_scripts_and_hidden(self):
        vis = sc.visible_text(GOOD)
        self.assertNotIn("esindaja_tel", vis)
        self.assertNotIn("Peidetud", vis)
        self.assertNotIn("MedicalClinic", vis)
        self.assertIn("Kuidas registreeruda?", vis)  # &nbsp; normalized


class TestJsonLd(unittest.TestCase):
    def test_graph_types_and_faq(self):
        blocks = sc.extract_jsonld(GOOD)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0]["ok"])
        self.assertEqual(blocks[0]["types"], ["MedicalClinic", "FAQPage"])
        self.assertEqual(len(blocks[0]["faq_questions"]), 2)

    def test_faq_questions_visible(self):
        r = sc.check_jsonld(GOOD, sc._norm_cmp(sc.visible_text(GOOD)))
        self.assertEqual(r["status"], sc.PASS, r["message"])
        self.assertIn("2 Qs all visible", r["message"])

    def test_invalid_json_and_missing_question(self):
        raw = fx("broken_jsonld.html")
        blocks = sc.extract_jsonld(raw)
        self.assertFalse(blocks[0]["ok"])
        self.assertTrue(blocks[1]["ok"])
        self.assertEqual(blocks[1]["faq_questions"], ["Hidden question?"])
        r = sc.check_jsonld(raw, sc._norm_cmp(sc.visible_text(raw)))
        self.assertEqual(r["status"], sc.FAIL)
        self.assertIn("INVALID JSON", r["message"])
        self.assertIn("1 not in visible text", r["message"])


class TestHreflang(unittest.TestCase):
    def test_extract(self):
        tags = sc.extract_hreflang(GOOD, PAGE)
        self.assertEqual([t["lang"] for t in tags], ["et", "en", "x-default"])

    def test_relative_href_resolved(self):
        tags = sc.extract_hreflang(fx("homepage_ru.html"), "https://example.ee/ru/")
        self.assertEqual(tags[0]["href"], "https://example.ee/ru/")

    def test_norm_url(self):
        self.assertEqual(sc.norm_url("https://www.example.ee/index.html"),
                         sc.norm_url("https://example.ee/"))
        self.assertEqual(sc.norm_url("http://WWW.Example.ee/en/index.html?x=1#a"),
                         "https://example.ee/en/")
        self.assertNotEqual(sc.norm_url("https://example.ee/en/"),
                            sc.norm_url("https://example.ee/en/register.html"))

    def test_reciprocity_with_fake_fetcher(self):
        en_ok = ('<html><head><link rel="alternate" hreflang="et" '
                 'href="https://example.ee/registreerumine.html"></head></html>')
        en_bad = '<html><head><link rel="alternate" hreflang="en" href="/en/register.html"></head></html>'

        class Fake:
            def __init__(self, en):
                self.en = en

            def get_page(self, url):
                text = {"https://www.example.ee/en/register.html": self.en}.get(url, GOOD)
                return {"status": 200, "text": text, "content": text.encode(),
                        "final_url": url, "error": None}

        self.assertEqual(sc.check_hreflang(PAGE, GOOD, Fake(en_ok))["status"], sc.PASS)
        r = sc.check_hreflang(PAGE, GOOD, Fake(en_bad))
        self.assertEqual(r["status"], sc.FAIL)
        self.assertIn("not reciprocal", r["message"])


class TestLinks(unittest.TestCase):
    def test_internal_refs(self):
        refs = sc.extract_internal_refs(GOOD, PAGE, BASE)
        self.assertEqual(refs["links"], {"https://example.ee/",
                                         "https://example.ee/en/register.html"})
        self.assertEqual(refs["images"], {"https://example.ee/img/logo.png",
                                          "https://example.ee/img/a.webp",
                                          "https://example.ee/img/a@2x.webp"})

    def test_www_counts_as_internal(self):
        self.assertTrue(sc.same_site("https://www.example.ee/x", BASE))
        self.assertFalse(sc.same_site("https://other.example.com/", BASE))


class TestPhones(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(sc.normalize_phone("5 911 0909"), "+37259110909")
        self.assertEqual(sc.normalize_phone("+372 613 8887"), "+3726138887")
        self.assertEqual(sc.normalize_phone("00372 613 8887"), "+3726138887")
        self.assertIsNone(sc.normalize_phone("2026"))

    def test_extract_ignores_js_and_hidden(self):
        p = sc.extract_phones(GOOD)
        self.assertEqual(p["tel_links"], {"+37259110909": 1})
        self.assertIn("+37259110909", p["text_phones"])
        self.assertNotIn("+37255551234", p["text_phones"])   # in <script>
        self.assertNotIn("+37266667777", p["text_phones"])   # hidden
        self.assertNotIn("+37212345678", p["text_phones"])   # "Registrikood 12345678"

    def test_compare_unchanged_and_changed(self):
        cur = sc.extract_phones(GOOD)
        st, _, _ = sc.compare_phones("registreerumine.html", cur, cur)
        self.assertEqual(st, sc.PASS)
        changed = {"tel_links": {"+37200000000": 1}, "text_phones": cur["text_phones"]}
        st, msg, _ = sc.compare_phones("registreerumine.html", cur, changed)
        self.assertEqual(st, sc.FAIL)
        self.assertIn("tel:", msg)

    def test_homepage_gain_flagged(self):
        base = {"tel_links": {}, "text_phones": {}}
        cur = {"tel_links": {}, "text_phones": {"+3726138887": 1}}
        st, msg, _ = sc.compare_phones("index.html", base, cur)
        self.assertEqual(st, sc.FAIL)
        self.assertIn("HOMEPAGE GAINED", msg)

    def test_no_baseline_warns(self):
        st, _, _ = sc.compare_phones("x.html", None, sc.extract_phones(GOOD))
        self.assertEqual(st, sc.WARN)


class TestStatusLines(unittest.TestCase):
    def test_et_statuses(self):
        lines = {x["doctor"]: x for x in sc.extract_status_lines(GOOD) if x["is_status"]}
        self.assertTrue(lines["Vessel"]["has_currently"])           # "praegu"
        self.assertFalse(lines["Lindström"]["has_currently"])       # missing word
        self.assertEqual(lines["Kolts"]["status"], "Vastuvõtt hetkel suletud")  # sibling
        self.assertNotIn("Gretšenko", lines)                        # bio, not status
        r = sc.check_status_lines(GOOD)
        self.assertEqual(r["status"], sc.WARN)

    def test_ru_statuses(self):
        lines = {x["doctor"]: x for x in sc.extract_status_lines(fx("homepage_ru.html"))}
        self.assertTrue(lines["Lindström"]["has_currently"])
        self.assertFalse(lines["Vessel"]["has_currently"])

    def test_no_doctors(self):
        self.assertEqual(sc.check_status_lines(fx("truncated.html"))["status"], sc.PASS)


class TestTypographyAndManifest(unittest.TestCase):
    def test_em_dash(self):
        hits = sc.find_em_dashes(sc.visible_text(GOOD))
        self.assertEqual(len(hits), 1)
        self.assertIn("Info — kõik", hits[0])
        self.assertEqual(sc.check_typography("a – b").get("status"), sc.PASS)  # en dash ok

    def test_manifest(self):
        b = b"<html></html>"
        good = {"index.html": sc.md5_bytes(b)}
        self.assertEqual(sc.check_manifest("index.html", b, good)["status"], sc.PASS)
        self.assertEqual(sc.check_manifest("index.html", b + b" ", good)["status"], sc.FAIL)
        self.assertEqual(sc.check_manifest("en/", b, good)["status"], sc.WARN)
        self.assertEqual(sc.check_manifest("en/", b, None)["status"], sc.SKIP)

    def test_local_file_mapping(self):
        self.assertTrue(sc.local_file_for("en/", "/s").endswith(os.path.join("en", "index.html")))
        self.assertTrue(sc.local_file_for("index.html", "/s").endswith("index.html"))

    def test_read_pages_ignores_comments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# c\n\nindex.html  # home\nen/\n")
        try:
            self.assertEqual(sc.read_pages(f.name), ["index.html", "en/"])
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
