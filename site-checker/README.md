# sltk_check: pre/post-upload checker for sltervisekeskus.ee

A small, read-only Python 3 tool that checks the **public** site
<https://sltervisekeskus.ee> before and after you upload files. It only sends
HTTPS `GET`/`HEAD` requests to public pages. It uses no credentials, no API
keys, no paid services and no patient data, and it never writes to the site.

## Install

```bash
cd site-checker
python3 -m pip install -r requirements.txt
```

## What it checks (PASS / WARN / FAIL per page)

| # | Check | FAIL when | WARN when |
|---|-------|-----------|-----------|
| 1 | HTTP 200 + complete HTML | status ≠ 200, request error, or no closing `</html>` (truncated upload) | |
| 2 | JSON-LD | a block is not valid JSON; an FAQPage question text is not in the visible page text | no JSON-LD; FAQPage with 0 questions |
| 3 | hreflang | no `<link rel="alternate" hreflang>`; a target isn't 200; a language version doesn't link back | page doesn't list itself |
| 4 | Internal links + images | any internal `<a href>`, `<img src/srcset>` or `<source srcset>` doesn't return 200 (tries HEAD, then GET) | |
| 5 | Phone guard | any change in `tel:` links or in phone-number-like text vs `baseline/phones.json`; **homepage gains a visible number** | no baseline yet |
| 6 | Doctor status lines | | a status line for Lindström / Vessel / Gretšenko / Kolts lacks "currently" / "hetkel" / "praegu" / "tällä hetkellä" / "в настоящее время" / "сейчас" |
| 7 | Typography | | em dash (—) in visible text |
| 8 | Deploy gate | live md5 ≠ md5 in `--manifest` | page missing from manifest |

Exit code: **1** if any FAIL, **0** otherwise (WARNs don't block), **2** on
usage error. Reports go to `reports/report-YYYYMMDD-HHMMSS.md` and `.json`.

## Typical workflow

**Once, while the live site is known to be good**, record the phone baseline:

```bash
python3 sltk_check.py --save-baseline
```

**Before the upload**, write a manifest from the local files you are about to upload
(the page paths in `pages.txt` map to files under the folder; `en/` maps to `en/index.html`):

```bash
python3 sltk_check.py --manifest-from-dir ~/site-build manifest.json
```

Run a normal check to see the "before" state:

```bash
python3 sltk_check.py
```

**After the upload**, run the deploy gate: are the live bytes exactly what you uploaded, and is everything still intact?

```bash
python3 sltk_check.py --manifest manifest.json
```

Other options:

```bash
python3 sltk_check.py --write-manifest live-manifest.json   # snapshot live md5s
```

```bash
python3 sltk_check.py --page en/register.html --page ru/ -v  # just two pages, full detail
```

```bash
python3 sltk_check.py --no-links                              # skip the slow link check
```

```bash
python3 sltk_check.py --help
```

## Files

- `pages.txt`: the pages to check, one per line. `#` comments are allowed. The
  same keys are used in the baseline and the manifest.
- `baseline/phones.json`: created by `--save-baseline`. Commit it if you want
  the baseline versioned. A run with `--save-baseline --page X` updates only page X.
- `manifest.json`: `{ "page path": "md5 of the exact bytes" }`.
- `reports/`: generated reports (git-ignored).

## Tests (offline, no network)

```bash
python3 -m unittest discover -s tests -v
```

## How the heuristics work (read this before you trust a WARN)

- **Visible text** is the `<body>` with `script`, `style`, `noscript`,
  `template`, `svg`, `[hidden]`, `aria-hidden="true"` and inline
  `display:none` removed. Elements hidden by CSS classes can't be detected
  without a browser, so they count as visible.
- **Phone-like strings** are digit groups that normalize to 7–8 digits (taken as
  Estonian, so `+372` is added) or to `+CC…` / `00CC…` numbers. Other 7–8-digit
  numbers, such as the company registry code, are also caught. That's
  intentional: the guard compares against the baseline, so stable false positives
  don't matter, but any *new* number does. `tel:` strings inside JavaScript
  are ignored; only `<a href="tel:">` counts. The guard compares occurrence
  counts, so the same number appearing one more time is also a change.
- **Status lines**: the text after a doctor's name in the same element, or else the next
  sibling element (for example `<h3>Name</h3><p>status</p>`), cut at the next doctor
  name. It counts as a *status* only if it contains a status word (nimistu, kohti,
  vastuvõtt, list, places, patients, potilas, список, мест, …). A
  bio sentence next to a name is reported, but it doesn't WARN.
- **Hosts**: `www.sltervisekeskus.ee` and `sltervisekeskus.ee` are treated as
  the same site. `…/index.html` equals `…/`.
- **md5** is taken from the response body after HTTP decompression. That
  equals the uploaded file unless the host or CDN rewrites HTML (for example by
  injecting scripts or obfuscating e-mail addresses). In that case the deploy gate
  will FAIL on every page, so use `--write-manifest` after a known-good upload as
  the reference.
- **Network**: up to 4 retries with exponential backoff (1, 2, 4, 8 s) on connection
  errors and 429/5xx, plus an outer retry for connections reset mid-transfer.
  Link checks run four at a time and are cached across pages.
