#!/usr/bin/env python3
"""sltk_check.py - read-only pre/post-upload checker for https://sltervisekeskus.ee

Fetches public pages over HTTPS and reports PASS / WARN / FAIL per page for:
  1. HTTP 200 + complete HTML          5. phone guard (vs baseline)
  2. JSON-LD validity + FAQPage        6. doctor status lines
  3. hreflang presence/targets/recip.  7. typography (em dash)
  4. internal links + images           8. deploy gate (md5 manifest)

Only GET/HEAD requests to public URLs. No credentials, no writes to the site.
Dependencies: requests, beautifulsoup4.
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "1.0"
DEFAULT_BASE = "https://sltervisekeskus.ee/"
HERE = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = f"sltk-site-checker/{VERSION} (+read-only QA script)"

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
_RANK = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}

HOMEPAGE_KEYS = {"", "index.html", "/", "/index.html"}

# Doctor surname -> spellings to look for (Latin, ASCII-folded, Cyrillic).
DOCTORS = {
    "Lindström": ["Lindström", "Lindstrom", "Линдстрём", "Линдстрем"],
    "Vessel": ["Vessel", "Вессел"],
    "Gretšenko": ["Gretšenko", "Gretsenko", "Gretshenko", "Греченко", "Гретшенко"],
    "Kolts": ["Kolts", "Колтс"],
}
# "currently" and its ET / FI / RU equivalents (matched case-insensitively).
CURRENTLY_WORDS = ["currently", "hetkel", "praegu", "tällä hetkellä",
                   "в настоящее время", "сейчас"]
# Words that mark text next to a doctor name as a *status* (not a bio sentence).
STATUS_HINTS = ["nimistu", "kohti", "kohad", "vastuvõt", "patsient",
                "list", "place", "accept", "patient", "appointment",
                "lista", "potila", "paikko", "vastaanot",
                "список", "мест", "приём", "прием", "пациент"]
SEPARATORS = " \t\r\n·•|:–—-,;()"

EM_DASH = "—"
INVISIBLE_TAGS = ["script", "style", "noscript", "template", "svg", "head"]


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
def make_session(retries: int = 4, backoff: float = 1.0) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries, connect=retries, read=retries, status=retries,
        backoff_factor=backoff,  # 1s, 2s, 4s, 8s ...
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = USER_AGENT
    return s


class Fetcher:
    """GET/HEAD with retries, an outer retry for mid-body resets, and caching."""

    def __init__(self, session: requests.Session, timeout: float = 20.0,
                 outer_attempts: int = 3):
        self.s = session
        self.timeout = timeout
        self.outer_attempts = outer_attempts
        self._pages: dict[str, dict] = {}
        self._status: dict[str, tuple[int, str]] = {}

    def _with_outer_retry(self, fn):
        last = None
        for attempt in range(self.outer_attempts):
            try:
                return fn()
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError,
                    requests.Timeout) as e:
                last = e
                time.sleep(2 ** attempt)
        raise last  # type: ignore[misc]

    def get_page(self, url: str) -> dict:
        """Returns {status, content(bytes), text, final_url, error}. Cached."""
        if url in self._pages:
            return self._pages[url]
        try:
            r = self._with_outer_retry(
                lambda: self.s.get(url, timeout=self.timeout, allow_redirects=True))
            res = {"status": r.status_code, "content": r.content,
                   "text": _decode(r), "final_url": r.url, "error": None}
        except requests.RequestException as e:
            res = {"status": 0, "content": b"", "text": "", "final_url": url,
                   "error": f"{type(e).__name__}: {e}"}
        self._pages[url] = res
        return res

    def url_status(self, url: str) -> tuple[int, str]:
        """HEAD, falling back to GET when HEAD is not 200. Returns (status, note)."""
        if url in self._status:
            return self._status[url]
        if url in self._pages and not self._pages[url]["error"]:
            res = (self._pages[url]["status"], "GET(cached)")
            self._status[url] = res
            return res
        note = "HEAD"
        try:
            r = self._with_outer_retry(
                lambda: self.s.head(url, timeout=self.timeout, allow_redirects=True))
            code = r.status_code
            if code != 200:
                note = f"HEAD {code} -> GET"
                r = self._with_outer_retry(
                    lambda: self.s.get(url, timeout=self.timeout,
                                       allow_redirects=True, stream=True))
                code = r.status_code
                r.close()
        except requests.RequestException as e:
            code, note = 0, f"{type(e).__name__}"
        self._status[url] = (code, note)
        return code, note


def _decode(r: requests.Response) -> str:
    # Prefer UTF-8 (site is UTF-8); requests defaults text/html to ISO-8859-1.
    try:
        return r.content.decode("utf-8")
    except UnicodeDecodeError:
        return r.content.decode(r.apparent_encoding or "latin-1", errors="replace")


# --------------------------------------------------------------------------- #
# Pure parsers (no network) - covered by tests/
# --------------------------------------------------------------------------- #
def html_complete(raw: str) -> bool:
    """True if a closing </html> tag appears near the end of the document."""
    return re.search(r"</html\s*>", raw[-4096:], re.I) is not None


def soup_of(raw: str) -> BeautifulSoup:
    return BeautifulSoup(raw, "html.parser")


def visible_text(raw: str) -> str:
    """Body text as a user would see it (scripts, styles, hidden nodes removed)."""
    s = soup_of(raw)
    root = s.body or s
    for t in root.find_all(INVISIBLE_TAGS):
        t.decompose()
    for t in root.find_all(attrs={"hidden": True}):
        t.decompose()
    for t in root.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        t.decompose()
    for t in root.find_all(attrs={"aria-hidden": "true"}):
        t.decompose()
    return normalize_ws(root.get_text(" "))


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def _norm_cmp(s: str) -> str:
    s = htmllib.unescape(s)
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("„", '"').replace("«", '"').replace("»", '"')
    return normalize_ws(s).casefold()


def extract_jsonld(raw: str) -> list[dict]:
    """Returns [{ok, error, data, types, faq_questions}] per JSON-LD block."""
    out = []
    for script in soup_of(raw).find_all("script", type=re.compile(r"ld\+json", re.I)):
        body = script.get_text().strip()
        body = re.sub(r"^\s*<!--|-->\s*$", "", body)
        body = re.sub(r"^\s*//\s*<!\[CDATA\[|//\s*\]\]>\s*$", "", body).strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            out.append({"ok": False, "error": f"{e.msg} at line {e.lineno} col {e.colno}",
                        "data": None, "types": [], "faq_questions": []})
            continue
        nodes = _top_nodes(data)
        types: list[str] = []
        questions: list[str] = []
        for n in nodes:
            t = n.get("@type")
            tl = t if isinstance(t, list) else [t] if t else []
            types.extend(str(x) for x in tl)
            if "FAQPage" in tl:
                questions.extend(faq_questions(n))
        out.append({"ok": True, "error": None, "data": data,
                    "types": types, "faq_questions": questions,
                    "has_faq": "FAQPage" in types})
    return out


def _top_nodes(data) -> list[dict]:
    items = data if isinstance(data, list) else [data]
    nodes = []
    for it in items:
        if not isinstance(it, dict):
            continue
        nodes.append(it)
        g = it.get("@graph")
        if isinstance(g, list):
            nodes.extend(x for x in g if isinstance(x, dict))
    return nodes


def faq_questions(node: dict) -> list[str]:
    me = node.get("mainEntity", [])
    me = me if isinstance(me, list) else [me]
    qs = []
    for q in me:
        if isinstance(q, dict) and q.get("name"):
            qs.append(normalize_ws(str(q["name"])))
    return qs


def question_visible(question: str, vis_text_norm: str) -> bool:
    q = _norm_cmp(question)
    if q in vis_text_norm:
        return True
    q2 = q.rstrip("?!.: ")
    return bool(q2) and q2 in vis_text_norm


def extract_hreflang(raw: str, page_url: str) -> list[dict]:
    """<link rel="alternate" hreflang="xx" href="..."> -> [{lang, href}] (absolute)."""
    out = []
    for link in soup_of(raw).find_all("link", hreflang=True):
        rel = link.get("rel") or []
        rel = rel if isinstance(rel, list) else [rel]
        if "alternate" not in [r.lower() for r in rel] or not link.get("href"):
            continue
        out.append({"lang": link["hreflang"].strip(),
                    "href": urljoin(page_url, link["href"].strip())})
    return out


def norm_url(url: str) -> str:
    """Canonical form for comparing: https, host without www., no index.html,
    no query/fragment."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path or "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    return urlunsplit(("https", host, path, "", ""))


def same_site(url: str, base: str) -> bool:
    return norm_url(url).split("/")[2] == norm_url(base).split("/")[2]


def extract_internal_refs(raw: str, page_url: str, base: str) -> dict[str, set[str]]:
    """Internal <a href> links and image URLs (img src/srcset, source srcset)."""
    s = soup_of(raw)
    links: set[str] = set()
    images: set[str] = set()
    for a in s.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:",
                                        "sms:", "whatsapp:")):
            continue
        u = _strip_fragment(urljoin(page_url, href))
        if u.startswith(("http://", "https://")) and same_site(u, base):
            links.add(u)
    for img in s.find_all(["img", "source"]):
        cands = []
        if img.get("src"):
            cands.append(img["src"])
        for attr in ("srcset", "data-srcset"):
            if img.get(attr):
                cands.extend(part.strip().split(" ")[0] for part in img[attr].split(","))
        for c in cands:
            c = c.strip()
            if not c or c.startswith("data:"):
                continue
            u = _strip_fragment(urljoin(page_url, c))
            if u.startswith(("http://", "https://")) and same_site(u, base):
                images.add(u)
    return {"links": links, "images": images}


def _strip_fragment(u: str) -> str:
    p = urlsplit(u)
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


# Phone-like: optional +CC, then 2-5 digit groups separated by single space/dash/dot.
_PHONE_RE = re.compile(
    r"(?<![\w+/.-])(?:\+\d{1,3}[ .-]?)?(?:\(?\d{1,4}\)?[ .-]?){1,4}\d{2,4}(?![\w/-])")


# Text right before a number that marks it as a company registry code.
_REGCODE_BEFORE = re.compile(
    r"(reg\.?|registrikood|registry code|reg\.? ?nr|y-tunnus|рег\.?|регистрационный код)"
    r"[\s:.#№-]*$", re.I)


def normalize_phone(s: str) -> str | None:
    plus = s.strip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if plus:
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if digits.startswith("00") and 10 <= len(digits) <= 17:
        return "+" + digits[2:]
    if 7 <= len(digits) <= 8:          # Estonian national number
        return "+372" + digits
    if 9 <= len(digits) <= 15 and digits.startswith("372"):
        return "+" + digits
    return None


def extract_phones(raw: str) -> dict:
    """tel: hrefs (normalized) and phone-like strings in visible text, as counts."""
    s = soup_of(raw)
    tel: dict[str, int] = {}
    for a in s.find_all("a", href=True):
        h = a["href"].strip()
        if h.lower().startswith("tel:"):
            n = normalize_phone(h[4:]) or h[4:].strip()
            tel[n] = tel.get(n, 0) + 1
    text: dict[str, int] = {}
    vis = visible_text(raw)
    for m in _PHONE_RE.finditer(vis):
        if _REGCODE_BEFORE.search(vis[max(0, m.start() - 25):m.start()]):
            continue  # company registry code, not a phone number
        n = normalize_phone(m.group(0))
        if n:
            text[n] = text.get(n, 0) + 1
    return {"tel_links": dict(sorted(tel.items())), "text_phones": dict(sorted(text.items()))}


def diff_counts(old: dict[str, int], new: dict[str, int]) -> dict:
    added = {k: new[k] - old.get(k, 0) for k in new if new[k] > old.get(k, 0)}
    removed = {k: old[k] - new.get(k, 0) for k in old if old[k] > new.get(k, 0)}
    return {"added": added, "removed": removed}


def compare_phones(page: str, baseline: dict | None, current: dict) -> tuple[str, str, dict]:
    if baseline is None:
        return WARN, "no baseline for this page (run with --save-baseline)", current
    dt = diff_counts(baseline.get("tel_links", {}), current["tel_links"])
    dx = diff_counts(baseline.get("text_phones", {}), current["text_phones"])
    details = {"tel_links": dt, "text_phones": dx, "current": current}
    msgs = []
    if is_homepage(page) and dx["added"]:
        msgs.append(f"HOMEPAGE GAINED visible phone number(s): {dx['added']}")
    for label, d in (("tel:", dt), ("text", dx)):
        if d["added"] or d["removed"]:
            msgs.append(f"{label} changed (+{d['added']} -{d['removed']})")
    if msgs:
        return FAIL, "; ".join(msgs), details
    n_tel = sum(current["tel_links"].values())
    n_txt = sum(current["text_phones"].values())
    return PASS, f"unchanged vs baseline ({n_tel} tel: links, {n_txt} text numbers)", details


def is_homepage(page: str) -> bool:
    return page.strip() in HOMEPAGE_KEYS


def _all_variants():
    for canon, variants in DOCTORS.items():
        for v in variants:
            yield canon, v


def _cut_at_next_doctor(text: str) -> str:
    cut = len(text)
    for _, v in _all_variants():
        i = text.find(v)
        if i != -1:
            cut = min(cut, i)
    return text[:cut]


def _remainder_after(text: str, name: str) -> str:
    i = text.find(name)
    if i == -1:
        return ""
    rest = text[i + len(name):]
    return _cut_at_next_doctor(rest).strip(SEPARATORS)


def has_currently(text: str) -> bool:
    t = text.casefold()
    return any(w in t for w in CURRENTLY_WORDS)


def looks_like_status(text: str) -> bool:
    t = text.casefold()
    return has_currently(text) or any(h in t for h in STATUS_HINTS)


def extract_status_lines(raw: str) -> list[dict]:
    """For each doctor-name occurrence, find nearby status text.

    Strategy: text after the name in the same element; if empty, the next
    sibling element(s); if still empty, climb one or two parents.
    """
    s = soup_of(raw)
    root = s.body or s
    for t in root.find_all(["script", "style", "noscript", "template", "svg"]):
        t.decompose()
    found: list[dict] = []
    seen = set()
    for canon, variant in _all_variants():
        for node in root.find_all(string=re.compile(re.escape(variant))):
            status = ""
            el = node.parent
            for _ in range(3):
                if el is None:
                    break
                txt = normalize_ws(el.get_text(" "))
                status = _remainder_after(txt, variant)
                if len(status) >= 4:
                    break
                sib = el.find_next_sibling()
                hops = 0
                while sib is not None and hops < 3 and len(status) < 4:
                    st = normalize_ws(sib.get_text(" "))
                    if st and len(st) <= 300:
                        status = _cut_at_next_doctor(st).strip(SEPARATORS)
                    sib = sib.find_next_sibling()
                    hops += 1
                if len(status) >= 4:
                    break
                el = el.parent
            status = status[:200]
            key = (canon, status)
            if key in seen:
                continue
            seen.add(key)
            is_status = bool(status) and looks_like_status(status)
            found.append({"doctor": canon, "matched": variant, "status": status,
                          "is_status": is_status,
                          "has_currently": has_currently(status) if status else False})
    return found


def find_em_dashes(vis_text: str, ctx: int = 40) -> list[str]:
    out = []
    for m in re.finditer(EM_DASH, vis_text):
        a, b = max(0, m.start() - ctx), min(len(vis_text), m.end() + ctx)
        out.append(("…" if a else "") + vis_text[a:b] + ("…" if b < len(vis_text) else ""))
    return out


def md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def local_file_for(page: str, root: str) -> str:
    p = page.lstrip("/")
    if p == "" or p.endswith("/"):
        p += "index.html"
    return os.path.join(root, *p.split("/"))


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def result(check: str, status: str, message: str, details=None) -> dict:
    return {"check": check, "status": status, "message": message, "details": details or {}}


def check_http(page_res: dict) -> dict:
    if page_res["error"]:
        return result("1 http", FAIL, f"request failed: {page_res['error']}")
    code = page_res["status"]
    complete = html_complete(page_res["text"])
    d = {"status": code, "bytes": len(page_res["content"]),
         "final_url": page_res["final_url"], "has_closing_html": complete}
    if code != 200:
        return result("1 http", FAIL, f"HTTP {code}", d)
    if not complete:
        return result("1 http", FAIL, f"HTTP 200 but no closing </html> ({d['bytes']} bytes, "
                                      "truncated upload?)", d)
    return result("1 http", PASS, f"HTTP 200, {d['bytes']} bytes, complete", d)


def check_jsonld(raw: str, vis_norm: str) -> dict:
    blocks = extract_jsonld(raw)
    if not blocks:
        return result("2 json-ld", WARN, "no JSON-LD blocks found")
    status, msgs, det = PASS, [], []
    for i, b in enumerate(blocks, 1):
        if not b["ok"]:
            status = FAIL
            msgs.append(f"block {i}: INVALID JSON ({b['error']})")
            det.append({"block": i, "ok": False, "error": b["error"]})
            continue
        entry = {"block": i, "ok": True, "types": b["types"]}
        if b.get("has_faq"):
            qs = b["faq_questions"]
            missing = [q for q in qs if not question_visible(q, vis_norm)]
            entry.update({"faq_questions": len(qs), "faq_missing_in_text": missing})
            if not qs:
                status = max(status, WARN, key=_RANK.get)
                msgs.append(f"block {i}: FAQPage with 0 questions")
            elif missing:
                status = FAIL
                msgs.append(f"block {i}: FAQPage {len(qs)} Qs, {len(missing)} not in visible text")
            else:
                msgs.append(f"block {i}: FAQPage {len(qs)} Qs all visible")
        det.append(entry)
    types = sorted({t for b in blocks for t in b["types"]})
    head = f"{len(blocks)} block(s), types: {', '.join(types) or '-'}"
    return result("2 json-ld", status, head + ("; " + "; ".join(msgs) if msgs else ""),
                  {"blocks": det})


def check_hreflang(page_url: str, raw: str, fetcher: Fetcher) -> dict:
    tags = extract_hreflang(raw, page_url)
    if not tags:
        return result("3 hreflang", FAIL, "no <link rel=alternate hreflang> tags")
    self_n = norm_url(page_url)
    status, problems, det = PASS, [], {"tags": tags, "targets": []}
    if self_n not in {norm_url(t["href"]) for t in tags}:
        status = WARN
        problems.append("page does not list itself in hreflang")
    for t in tags:
        tgt = {"lang": t["lang"], "href": t["href"]}
        res = fetcher.get_page(t["href"])
        tgt["status"] = res["status"]
        if res["status"] != 200:
            status = FAIL
            problems.append(f"{t['lang']} -> {t['href']} returned {res['status'] or res['error']}")
        elif t["lang"].lower() != "x-default" and norm_url(t["href"]) != self_n:
            back = {norm_url(x["href"]) for x in extract_hreflang(res["text"], res["final_url"])}
            tgt["links_back"] = self_n in back
            if self_n not in back:
                status = FAIL
                problems.append(f"not reciprocal: {t['lang']} page does not link back")
        det["targets"].append(tgt)
    langs = ", ".join(t["lang"] for t in tags)
    msg = f"{len(tags)} tags ({langs})" + ("; " + "; ".join(problems) if problems
                                            else "; all 200 and reciprocal")
    return result("3 hreflang", status, msg, det)


def check_links(page_url: str, raw: str, base: str, fetcher: Fetcher, workers: int) -> dict:
    refs = extract_internal_refs(raw, page_url, base)
    urls = sorted(refs["links"] | refs["images"])
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        statuses = dict(zip(urls, ex.map(fetcher.url_status, urls)))
    broken = [{"url": u, "status": statuses[u][0], "note": statuses[u][1],
               "kind": "image" if u in refs["images"] else "link"}
              for u in urls if statuses[u][0] != 200]
    d = {"links": len(refs["links"]), "images": len(refs["images"]), "broken": broken}
    if broken:
        return result("4 links", FAIL, f"{len(broken)} broken of {len(urls)} "
                                       f"({len(refs['links'])} links, {len(refs['images'])} images)", d)
    return result("4 links", PASS, f"all {len(urls)} OK ({len(refs['links'])} links, "
                                   f"{len(refs['images'])} images)", d)


def check_status_lines(raw: str) -> dict:
    lines = extract_status_lines(raw)
    statuses = [x for x in lines if x["is_status"]]
    if not lines:
        return result("6 status", PASS, "no doctor names on page")
    if not statuses:
        return result("6 status", PASS, f"{len(lines)} doctor mention(s), no status lines",
                      {"mentions": lines})
    bad = [x for x in statuses if not x["has_currently"]]
    summary = "; ".join(f"{x['doctor']}: \"{x['status'][:70]}\"" for x in statuses)
    st = WARN if bad else PASS
    prefix = (f"{len(bad)} status line(s) lack 'currently'/'praegu'/... | " if bad else "")
    return result("6 status", st, prefix + summary, {"mentions": lines})


def check_typography(vis: str) -> dict:
    hits = find_em_dashes(vis)
    if hits:
        return result("7 typography", WARN, f"{len(hits)} em dash(es) in visible text",
                      {"em_dash_contexts": hits})
    return result("7 typography", PASS, "no em dashes")


def check_manifest(page: str, content: bytes, manifest: dict | None) -> dict:
    if manifest is None:
        return result("8 deploy-gate", SKIP, "no --manifest given")
    live = md5_bytes(content)
    exp = manifest.get(page)
    if exp is None:
        return result("8 deploy-gate", WARN, "page not in manifest", {"live_md5": live})
    if exp.lower() != live:
        return result("8 deploy-gate", FAIL, f"md5 mismatch: expected {exp}, live {live}",
                      {"expected": exp, "live_md5": live})
    return result("8 deploy-gate", PASS, f"md5 matches {live}", {"live_md5": live})


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def read_pages(path: str) -> list[str]:
    pages = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                pages.append(line)
    return pages


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def run_page(page: str, base: str, fetcher: Fetcher, args, baseline, manifest) -> dict:
    url = urljoin(base, page)
    res = fetcher.get_page(url)
    checks = [check_http(res)]
    phones = None
    if res["error"] or res["status"] != 200:
        for name in ("2 json-ld", "3 hreflang", "4 links", "5 phones", "6 status",
                     "7 typography", "8 deploy-gate"):
            checks.append(result(name, SKIP, "page not fetched"))
    else:
        raw = res["text"]
        vis = visible_text(raw)
        checks.append(check_jsonld(raw, _norm_cmp(vis)))
        checks.append(check_hreflang(res["final_url"], raw, fetcher))
        if args.no_links:
            checks.append(result("4 links", SKIP, "--no-links"))
        else:
            checks.append(check_links(res["final_url"], raw, base, fetcher, args.workers))
        phones = extract_phones(raw)
        if args.save_baseline:
            checks.append(result("5 phones", PASS, "baseline saved", {"current": phones}))
        else:
            st, msg, det = compare_phones(page, (baseline or {}).get(page), phones)
            checks.append(result("5 phones", st, msg, det))
        checks.append(check_status_lines(raw))
        checks.append(check_typography(vis))
        checks.append(check_manifest(page, res["content"], manifest))
    worst = max((c["status"] for c in checks), key=_RANK.get)
    return {"page": page, "url": url, "status": worst, "checks": checks,
            "_phones": phones, "_md5": md5_bytes(res["content"]) if not res["error"]
            and res["status"] == 200 else None}


ICON = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "·"}


def print_console(report: dict, verbose: bool) -> None:
    for p in report["pages"]:
        print(f"\n[{p['status']}] {p['page'] or '/'}  ({p['url']})")
        for c in p["checks"]:
            if c["status"] == SKIP and not verbose:
                continue
            print(f"   {ICON[c['status']]} {c['status']:<4} {c['check']:<14} {c['message']}")
            if verbose or c["status"] in (FAIL, WARN):
                for line in _detail_lines(c):
                    print(f"          - {line}")
    s = report["summary"]
    print("\n" + "=" * 72)
    print(f"Pages: {s['pages']}  |  checks PASS {s['PASS']}  WARN {s['WARN']}  "
          f"FAIL {s['FAIL']}  SKIP {s['SKIP']}  |  overall: {s['overall']}")


def _detail_lines(c: dict) -> list[str]:
    d = c["details"]
    out = []
    if c["check"].startswith("4") and d.get("broken"):
        out += [f"{b['kind']} {b['url']} -> {b['status']} ({b['note']})" for b in d["broken"][:25]]
    if c["check"].startswith("7"):
        out += d.get("em_dash_contexts", [])[:10]
    if c["check"].startswith("2"):
        for b in d.get("blocks", []):
            for q in b.get("faq_missing_in_text", []):
                out.append(f"FAQ not visible: {q}")
    if c["check"].startswith("6"):
        out += [f"{m['doctor']}: {m['status'] or '(no text found)'}"
                for m in d.get("mentions", []) if m["is_status"] and not m["has_currently"]]
    return out


def render_markdown(report: dict) -> str:
    s = report["summary"]
    L = [f"# SL Tervisekeskus site check - {report['timestamp']}", "",
         f"- Base URL: {report['base_url']}",
         f"- Overall: **{s['overall']}** (PASS {s['PASS']}, WARN {s['WARN']}, "
         f"FAIL {s['FAIL']}, SKIP {s['SKIP']})",
         f"- Baseline: {report['baseline_file'] or 'none'}; manifest: {report['manifest_file'] or 'none'}",
         "", "| Page | Overall | " + " | ".join(str(i) for i in range(1, 9)) + " |",
         "|---|---|" + "---|" * 8]
    for p in report["pages"]:
        L.append(f"| `{p['page'] or '/'}` | {p['status']} | "
                 + " | ".join(c["status"] for c in p["checks"]) + " |")
    L += ["", "Checks: 1 http, 2 json-ld, 3 hreflang, 4 links, 5 phones, 6 status, "
              "7 typography, 8 deploy-gate", ""]
    for p in report["pages"]:
        L += [f"## `{p['page'] or '/'}` - {p['status']}", "", f"<{p['url']}>", ""]
        for c in p["checks"]:
            L.append(f"- **{c['status']}** {c['check']}: {c['message'].replace('|', '/')}")
            for line in _detail_lines(c):
                L.append(f"  - {line}")
        L.append("")
    return "\n".join(L)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Read-only checker for sltervisekeskus.ee (run before/after uploads).")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--pages", default=os.path.join(HERE, "pages.txt"),
                    help="file with one page path per line (default: pages.txt)")
    ap.add_argument("--page", action="append", help="check only this page (repeatable)")
    ap.add_argument("--baseline", default=os.path.join(HERE, "baseline", "phones.json"))
    ap.add_argument("--save-baseline", action="store_true",
                    help="record current phone numbers as the new baseline")
    ap.add_argument("--manifest", help="manifest.json {page: md5}; FAIL on mismatch")
    ap.add_argument("--write-manifest", nargs="?", const="manifest.json", metavar="PATH",
                    help="write live md5s of all pages to PATH (default manifest.json)")
    ap.add_argument("--manifest-from-dir", nargs=2, metavar=("DIR", "OUT"),
                    help="write a manifest from LOCAL files you are about to upload, then exit")
    ap.add_argument("--reports-dir", default=os.path.join(HERE, "reports"))
    ap.add_argument("--no-links", action="store_true", help="skip check 4 (faster)")
    ap.add_argument("--workers", type=int, default=4, help="parallel link checks (default 4)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    base = args.base_url if args.base_url.endswith("/") else args.base_url + "/"
    if not base.startswith("https://"):
        print("error: --base-url must be https://", file=sys.stderr)
        return 2
    pages = args.page or read_pages(args.pages)

    if args.manifest_from_dir:
        root, out = args.manifest_from_dir
        man, missing = {}, []
        for p in pages:
            f = local_file_for(p, root)
            if os.path.isfile(f):
                with open(f, "rb") as fh:
                    man[p] = md5_bytes(fh.read())
            else:
                missing.append(f)
        write_json(out, man)
        print(f"wrote {out} with {len(man)} entries" +
              (f"; missing locally: {missing}" if missing else ""))
        return 0

    baseline = None
    if not args.save_baseline and os.path.isfile(args.baseline):
        baseline = load_json(args.baseline).get("pages", {})
    manifest = load_json(args.manifest) if args.manifest else None
    if args.manifest and args.write_manifest and \
            os.path.abspath(args.manifest) == os.path.abspath(args.write_manifest):
        print("error: --manifest and --write-manifest point to the same file", file=sys.stderr)
        return 2

    fetcher = Fetcher(make_session(args.retries), timeout=args.timeout)
    started = datetime.now()
    print(f"sltk_check {VERSION}: {len(pages)} pages from {base}")
    page_results = []
    for p in pages:
        print(f"  checking {p or '/'} ...", flush=True)
        page_results.append(run_page(p, base, fetcher, args, baseline, manifest))

    if args.save_baseline:
        prev = load_json(args.baseline).get("pages", {}) if os.path.isfile(args.baseline) else {}
        for r in page_results:
            if r["_phones"] is not None:
                prev[r["page"]] = r["_phones"]
        write_json(args.baseline, {"saved_at": started.isoformat(timespec="seconds"),
                                   "base_url": base, "pages": prev})
        print(f"baseline written: {args.baseline}")
    if args.write_manifest:
        man = {r["page"]: r["_md5"] for r in page_results if r["_md5"]}
        write_json(args.write_manifest, man)
        print(f"manifest written: {args.write_manifest} ({len(man)} pages)")

    counts = {k: 0 for k in (PASS, WARN, FAIL, SKIP)}
    for r in page_results:
        for c in r["checks"]:
            counts[c["status"]] += 1
    overall = FAIL if counts[FAIL] else WARN if counts[WARN] else PASS
    report = {
        "tool": f"sltk_check {VERSION}", "timestamp": started.isoformat(timespec="seconds"),
        "duration_s": round((datetime.now() - started).total_seconds(), 1),
        "base_url": base,
        "baseline_file": None if args.save_baseline or baseline is None else args.baseline,
        "manifest_file": args.manifest,
        "summary": {"pages": len(page_results), **counts, "overall": overall},
        "pages": [{k: v for k, v in r.items() if not k.startswith("_")} for r in page_results],
    }
    print_console(report, args.verbose)

    stamp = started.strftime("%Y%m%d-%H%M%S")
    os.makedirs(args.reports_dir, exist_ok=True)
    md_path = os.path.join(args.reports_dir, f"report-{stamp}.md")
    js_path = os.path.join(args.reports_dir, f"report-{stamp}.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report) + "\n")
    write_json(js_path, report)
    print(f"reports: {md_path}\n         {js_path}")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
