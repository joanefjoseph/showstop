#!/usr/bin/env python3
"""
linkedin_people_scraper.py
Collects employees listed on a LinkedIn company "People" page and APPENDS them
to a CSV (and optionally a SQLite table) with the columns:
    company_name | first_name | last_name | job_title | linkedin_profile_url | email
- job_title: most recent title held at the queried company, taken from the
  profile's Experience section. Company names are matched fuzzily (suffixes like
  LLC / Inc / Corp / Corporate / Company / Co / Ltd are ignored), so
  "weverse-corporate" matches "WEVERSE COMPANY".
- email: only filled if visible in the profile's "Contact info" overlay for the
  logged-in account.
Usage:
    python linkedin_people_scraper.py "https://www.linkedin.com/company/acme/people/" \
        --out all_people.csv --sqlite all_people.db --max-people 50
    # Check how two company names compare (no network access):
    python linkedin_people_scraper.py --test-match weverse-corporate "WEVERSE COMPANY"
On first run, a browser window opens; log in to LinkedIn manually, then press
Enter in the terminal. The session is saved in ./li_browser_profile for reuse.
NOTE: Automated scraping violates LinkedIn's User Agreement and may get the
account restricted or banned. Scraped personal data is subject to privacy laws
(GDPR, CCPA, etc.). Use responsibly and at your own risk.
"""
import argparse
import csv
import random
import re
import sqlite3
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
COLUMNS = ["company_name", "first_name", "last_name", "job_title",
           "linkedin_profile_url", "email"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# --------------------------------------------------------------------------- #
# General helpers
# --------------------------------------------------------------------------- #
def polite_sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))
def normalize_people_url(url: str) -> tuple[str, str]:
    """Return (people_url, company_slug)."""
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    m = re.search(r"/company/([^/]+)", parsed.path)
    if not m:
        sys.exit(f"Could not find '/company/<name>' in URL: {url}")
    slug = m.group(1)
    return f"https://www.linkedin.com/company/{slug}/people/", slug
def clean_profile_url(href: str) -> str | None:
    """Normalize to https://www.linkedin.com/in/<slug>/"""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", href)
    if not m:
        return None
    return f"https://www.linkedin.com/in/{m.group(1)}/"
def split_name(full_name: str) -> tuple[str, str]:
    """Strip credentials/emoji/parentheticals and split into first/last."""
    name = full_name.strip()
    name = re.sub(r"\(.*?\)", "", name)       # "Jane (JJ) Doe" -> "Jane Doe"
    name = name.split(",")[0]                 # "Jane Doe, MBA" -> "Jane Doe"
    name = re.sub(r"[^\w\s'\-\.]", "", name)  # drop emoji / symbols
    name = re.sub(r"\s+", " ", name).strip()
    parts = name.split(" ")
    if not parts or not parts[0]:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])
# --------------------------------------------------------------------------- #
# Company-name matching
# --------------------------------------------------------------------------- #
# Trailing tokens stripped from company names (repeatedly, from the end only).
# Multi-word entries are matched as whole phrases.
COMPANY_SUFFIXES = [
    # multi-word legal forms
    "public limited company", "limited liability company", "limited liability co",
    "private limited", "co ltd", "pvt ltd", "pte ltd", "pty ltd", "sdn bhd",
    "co inc", "and co", "and company",
    # single-word legal forms
    "incorporated", "corporation", "corporate", "company", "limited",
    "llc", "llp", "lp", "inc", "corp", "co", "ltd", "plc", "pc",
    "gmbh", "ag", "kg", "sa", "sas", "sarl", "srl", "spa", "bv", "nv",
    "ab", "as", "asa", "oy", "kk", "pte", "pvt", "pty", "bhd",
    # common descriptors
    "holdings", "holding", "group", "international", "intl", "global",
]
_SUFFIX_TOKENS = sorted((tuple(s.split()) for s in COMPANY_SUFFIXES),
                        key=len, reverse=True)
def normalize_company(name: str) -> str:
    """
    Reduce a company name to its 'core' for comparison, e.g.
        'WEVERSE COMPANY'         -> 'weverse'
        'weverse-corporate'       -> 'weverse'
        'Weverse Co., Ltd.'       -> 'weverse'
        'The Walt Disney Company' -> 'walt disney'
    Unicode letters (e.g. Korean, Japanese) are preserved.
    """
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)      # drop parentheticals
    s = s.replace("&", " and ")
    s = re.sub(r"[\W_]+", " ", s)               # punctuation/hyphens -> space
    tokens = s.split()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    changed = True
    while changed and len(tokens) > 1:
        changed = False
        for suf in _SUFFIX_TOKENS:
            n = len(suf)
            if len(tokens) > n and tuple(tokens[-n:]) == suf:
                tokens = tokens[:-n]
                changed = True
                break
    return " ".join(tokens)
def slug_to_name(slug: str) -> str:
    """'weverse-corporate' -> 'weverse corporate'; 'acme-inc-2' -> 'acme inc'"""
    parts = [p for p in re.split(r"[-_]+", slug.lower()) if p]
    if len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(parts)
class CompanyMatcher:
    """Decides whether an experience entry refers to the target company."""
    def __init__(self, names: list[str], company_id: str | None, slug: str | None,
                 threshold: float = 0.88, min_fuzzy_len: int = 5,
                 allow_prefix: bool = False):
        self.company_id = company_id
        self.slug = (slug or "").lower()
        self.threshold = threshold
        self.min_fuzzy_len = min_fuzzy_len
        self.allow_prefix = allow_prefix
        cores = {normalize_company(n) for n in names if n}
        cores.discard("")
        # list of (core, compact) e.g. ('walt disney', 'waltdisney')
        self.targets = [(c, c.replace(" ", "")) for c in sorted(cores)]
    def describe(self) -> str:
        return ", ".join(core for core, _ in self.targets) or "(none)"
    def score_name(self, name: str) -> float:
        """Similarity in [0, 1] between `name` and the best target core."""
        core = normalize_company(name)
        if not core:
            return 0.0
        compact = core.replace(" ", "")
        best = 0.0
        for t_core, t_compact in self.targets:
            # Exact match after normalization (spaces ignored: 'we verse' == 'weverse')
            if compact == t_compact:
                return 1.0
            # Too short for fuzzy matching -> exact only
            if min(len(compact), len(t_compact)) < self.min_fuzzy_len:
                continue
            best = max(best, SequenceMatcher(None, compact, t_compact).ratio())
            # Optional: 'samsung' matches 'samsung electronics' (or vice versa)
            if self.allow_prefix:
                a, b = core.split(), t_core.split()
                shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
                if longer[:len(shorter)] == shorter:
                    best = max(best, 0.90)
        return best
    def match_name(self, name: str) -> str | None:
        """Return a human-readable match reason, or None."""
        score = self.score_name(name)
        if score >= self.threshold:
            return f"name '{name}' (score {score:.2f})"
        return None
    def match_link(self, href: str) -> str | None:
        """Match on a /company/<id-or-slug>/ link. Returns reason or None."""
        m = re.search(r"/company/([^/?#]+)", href)
        if not m:
            return None
        seg = m.group(1).lower()
        if self.company_id and seg == self.company_id:
            return f"company id {seg}"
        if self.slug and seg == self.slug:
            return f"company slug {seg}"
        if not seg.isdigit():
            score = self.score_name(slug_to_name(seg))
            if score >= self.threshold:
                return f"company slug '{seg}' (score {score:.2f})"
        return None
# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def migrate_csv(csv_path: Path) -> None:
    """If an existing CSV has an older header, rewrite it with the current COLUMNS."""
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == COLUMNS:
            return
        rows = list(reader)
    print(f"Migrating {csv_path} to new column layout: {COLUMNS}")
    tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(csv_path)
def load_existing(csv_path: Path) -> dict[tuple[str, str], dict]:
    """Return {(company_name, profile_url): row} for rows already in the CSV."""
    if not csv_path.exists():
        return {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        return {
            (row["company_name"], row["linkedin_profile_url"]): row
            for row in csv.DictReader(f)
        }
def append_csv(csv_path: Path, row: dict) -> None:
    """Append one row, writing the header only if the file is new/empty."""
    is_new = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
def init_sqlite(db_path: Path) -> None:
    """Create the table if needed and add any missing columns (e.g. job_title)."""
    con = sqlite3.connect(db_path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS employees (
               company_name TEXT NOT NULL,
               first_name TEXT,
               last_name TEXT,
               job_title TEXT,
               linkedin_profile_url TEXT NOT NULL,
               email TEXT,
               PRIMARY KEY (company_name, linkedin_profile_url)
           )"""
    )
    existing_cols = {r[1] for r in con.execute("PRAGMA table_info(employees)")}
    if "job_title" not in existing_cols:
        print(f"Adding job_title column to {db_path}")
        con.execute("ALTER TABLE employees ADD COLUMN job_title TEXT")
    con.commit()
    con.close()
def write_sqlite(db_path: Path, rows: list[dict]) -> None:
    con = sqlite3.connect(db_path)
    con.executemany(
        """INSERT INTO employees
               (company_name, first_name, last_name, job_title, linkedin_profile_url, email)
           VALUES (:company_name, :first_name, :last_name, :job_title,
                   :linkedin_profile_url, :email)
           ON CONFLICT(company_name, linkedin_profile_url) DO UPDATE SET
               first_name = excluded.first_name,
               last_name  = excluded.last_name,
               job_title  = COALESCE(excluded.job_title, employees.job_title),
               email      = COALESCE(excluded.email, employees.email)""",
        rows,
    )
    con.commit()
    con.close()
# --------------------------------------------------------------------------- #
# Browser steps
# --------------------------------------------------------------------------- #
def ensure_logged_in(page) -> None:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    polite_sleep(2, 3)
    if any(x in page.url for x in ("login", "authwall", "checkpoint", "signup")):
        print("\n>>> Please log in to LinkedIn in the opened browser window.")
        input(">>> Press Enter here once you see your LinkedIn feed... ")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        if "feed" not in page.url:
            sys.exit("Still not logged in; aborting.")
def get_company_name(page, fallback: str) -> str:
    try:
        h1 = page.locator("h1").first
        h1.wait_for(timeout=8000)
        text = h1.inner_text().strip()
        return text or fallback
    except PWTimeout:
        return fallback
def get_company_id(page) -> str | None:
    """
    Best-effort extraction of the company's numeric ID from the company page.
    Experience entries on profiles usually link to /company/<numeric-id>/.
    """
    html = page.content()
    for pattern in (r"urn:li:fsd_company:(\d+)",
                    r"urn:li:company:(\d+)",
                    r"f_C=(\d+)",
                    r'"companyId"\s*:\s*(\d+)'):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None
# JS run on the People page: find each person card and pull profile URL + name.
EXTRACT_PEOPLE_JS = """
() => {
  const out = [];
  const anchors = document.querySelectorAll('main a[href*="/in/"]');
  for (const a of anchors) {
    const card = a.closest('li') || a.parentElement;
    let name = '';
    const titleEl = card && (
      card.querySelector('.artdeco-entity-lockup__title') ||
      card.querySelector('.org-people-profile-card__profile-title')
    );
    if (titleEl) name = titleEl.innerText;
    if (!name) name = (a.innerText || a.getAttribute('aria-label') || '');
    name = name.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
    out.push({ href: a.href, name });
  }
  return out;
}
"""
# JS run on /in/<slug>/details/experience/: return raw top-level experience
# entries. Matching against the target company happens in Python.
EXTRACT_EXPERIENCE_JS = """
() => {
  // Visible text lines. LinkedIn duplicates text in visually-hidden spans for
  // screen readers; aria-hidden spans are the visible copies.
  const lines = el => {
    const spans = Array.from(el.querySelectorAll('span[aria-hidden="true"]'))
      .map(s => s.innerText.trim()).filter(Boolean);
    if (spans.length) return spans;
    return (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
  };
  // Date-range line, e.g. "Jan 2021 - Present · 3 yrs 2 mos"
  const isDateLine = s =>
    /(\\b(19|20)\\d{2}\\b|present)/i.test(s) && /\\s[-\\u2013]\\s/.test(s);
  const topItems = Array.from(document.querySelectorAll('main section li'))
    .filter(li => !li.parentElement.closest('main section li'));
  return topItems.map(li => {
    // Nested roles (grouped layout): direct child <li>s containing a date range
    const nested = Array.from(li.querySelectorAll('li')).filter(sub =>
      sub.parentElement.closest('li') === li &&
      lines(sub).slice(0, 4).some(isDateLine)
    );
    return {
      lines: lines(li).slice(0, 6),
      hrefs: Array.from(li.querySelectorAll('a[href*="/company/"]')).map(a => a.href),
      nested_titles: nested.map(sub => lines(sub)[0]).filter(Boolean),
    };
  }).filter(e => e.lines.length);
}
"""
def collect_people(page, people_url: str, max_new: int, known_urls: set[str]) -> dict[str, str]:
    """Scroll the People page until `max_new` previously-unseen people are found."""
    page.goto(people_url, wait_until="domcontentloaded")
    polite_sleep(3, 5)
    people: dict[str, str] = {}
    stale_rounds = 0
    while stale_rounds < 4:
        before = len(people)
        for item in page.evaluate(EXTRACT_PEOPLE_JS):
            url = clean_profile_url(item["href"])
            name = item["name"].strip()
            # Out-of-network members show as "LinkedIn Member" with no usable URL
            if url and name and name.lower() != "linkedin member":
                people.setdefault(url, name)
        new_count = sum(1 for u in people if u not in known_urls)
        print(f"  ...{len(people)} seen, {new_count} new")
        if new_count >= max_new:
            break
        show_more = page.locator("button:has-text('Show more results')")
        if show_more.count() and show_more.first.is_visible():
            show_more.first.click()
        else:
            page.mouse.wheel(0, 3000)
        polite_sleep(2.5, 4.5)
        stale_rounds = stale_rounds + 1 if len(people) == before else 0
    new_only = {u: n for u, n in people.items() if u not in known_urls}
    return dict(list(new_only.items())[:max_new])
def pick_title(entries: list[dict], matcher: CompanyMatcher) -> tuple[str | None, str | None]:
    """
    Walk experience entries (newest first) and return (title, match_reason)
    for the first entry that refers to the target company.
    """
    for e in entries:
        lines = e.get("lines") or []
        nested = e.get("nested_titles") or []
        grouped = bool(nested)
        # Where the company name lives depends on the layout:
        #   grouped: line 0 = company, roles are nested
        #   single:  line 0 = title,   line 1 = "Company · Full-time"
        company_lines = lines[:1] if grouped else lines[1:3]
        reason = None
        for href in e.get("hrefs") or []:
            reason = matcher.match_link(href)
            if reason:
                break
        if not reason:
            for line in company_lines:
                company_text = line.split("\u00b7")[0].strip()   # drop "· Full-time"
                reason = matcher.match_name(company_text)
                if reason:
                    break
        if not reason:
            continue
        title = nested[0] if grouped else (lines[0] if lines else None)
        if title:
            return re.sub(r"\s+", " ", title).strip(), reason
    return None, None
def get_job_title(page, profile_url: str, matcher: CompanyMatcher) -> str | None:
    """Return the most recent job title at the target company from the Experience page."""
    try:
        page.goto(profile_url + "details/experience/", wait_until="domcontentloaded")
        page.locator("main section li").first.wait_for(timeout=8000)
    except PWTimeout:
        return None
    page.mouse.wheel(0, 1500)   # nudge lazy-loaded content
    polite_sleep(1.0, 2.0)
    try:
        entries = page.evaluate(EXTRACT_EXPERIENCE_JS)
    except Exception as e:
        print(f"    ! error reading experience: {e}")
        return None
    title, reason = pick_title(entries, matcher)
    if title:
        print(f"    matched via {reason}")
    return title
def get_email(page, profile_url: str) -> str | None:
    """Open the Contact info overlay and return a visible email, if any."""
    try:
        page.goto(profile_url + "overlay/contact-info/", wait_until="domcontentloaded")
        mailto = page.locator("a[href^='mailto:']").first
        mailto.wait_for(timeout=6000)
        href = mailto.get_attribute("href") or ""
        email = href.replace("mailto:", "").split("?")[0].strip()
        if EMAIL_RE.fullmatch(email):
            return email
    except PWTimeout:
        pass
    except Exception as e:
        print(f"    ! error reading contact info: {e}")
    # Fallback: scan the dialog text
    try:
        dialog = page.locator("[role='dialog']").first
        if dialog.count():
            m = EMAIL_RE.search(dialog.inner_text())
            if m:
                return m.group(0)
    except Exception:
        pass
    return None
# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape a LinkedIn company People page.")
    ap.add_argument("people_url", nargs="?",
                    help="e.g. https://www.linkedin.com/company/acme/people/")
    ap.add_argument("--out", default="people_data.csv", help="CSV output path (appended to)")
    ap.add_argument("--sqlite", help="Optional SQLite DB path (table: people_data)")
    ap.add_argument("--max-people", type=int, default=100,
                    help="Max NEW profiles to process per run (default 100)")
    ap.add_argument("--skip-emails", action="store_true",
                    help="Don't open each profile's Contact info overlay")
    ap.add_argument("--skip-titles", action="store_true",
                    help="Don't open each profile's Experience section")
    ap.add_argument("--alias", action="append", default=[],
                    help="Extra company name to match in Experience (repeatable), "
                         "e.g. --alias 'beNX' for a former name")
    ap.add_argument("--match-threshold", type=float, default=0.88,
                    help="Fuzzy similarity threshold 0-1 for company names (default 0.88)")
    ap.add_argument("--allow-prefix-match", action="store_true",
                    help="Also match when one name is a word-prefix of the other "
                         "(e.g. 'Samsung' ~ 'Samsung Electronics'). Riskier.")
    ap.add_argument("--test-match", nargs=2, metavar=("TARGET", "CANDIDATE"),
                    help="Print normalization/score for two company names and exit")
    ap.add_argument("--min-delay", type=float, default=4.0)
    ap.add_argument("--max-delay", type=float, default=9.0)
    ap.add_argument("--profile-dir", default="li_browser_profile",
                    help="Where the persistent browser session is stored")
    args = ap.parse_args()
    # ---- Offline name-matching test ----
    if args.test_match:
        target, candidate = args.test_match
        m = CompanyMatcher([target, slug_to_name(target)], None, None,
                           threshold=args.match_threshold,
                           allow_prefix=args.allow_prefix_match)
        print(f"target    -> {m.describe()}")
        print(f"candidate -> {normalize_company(candidate)!r}")
        print(f"score     =  {m.score_name(candidate):.2f} "
              f"({'MATCH' if m.match_name(candidate) else 'no match'} "
              f"at threshold {args.match_threshold})")
        return
    if not args.people_url:
        ap.error("people_url is required (unless using --test-match)")
    people_url, slug = normalize_people_url(args.people_url)
    csv_path = Path(args.out)
    db_path = Path(args.sqlite) if args.sqlite else None
    migrate_csv(csv_path)
    if db_path:
        init_sqlite(db_path)
    existing = load_existing(csv_path)  # {(company, url): row}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=args.profile_dir,
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page)
            print(f"Loading {people_url}")
            page.goto(people_url, wait_until="domcontentloaded")
            company_name = get_company_name(page, fallback=slug)
            company_id = get_company_id(page)
            print(f"Company: {company_name} (slug={slug}, id={company_id or 'unknown'})")
            matcher = CompanyMatcher(
                names=[company_name, slug_to_name(slug), *args.alias],
                company_id=company_id,
                slug=slug,
                threshold=args.match_threshold,
                allow_prefix=args.allow_prefix_match,
            )
            print(f"Matching experience entries against: {matcher.describe()}")
            known_urls = {u for (c, u) in existing if c == company_name}
            print(f"{len(known_urls)} profiles for this company already saved.")
            people = collect_people(page, people_url, args.max_people, known_urls)
            print(f"{len(people)} new profiles to process.")
            for i, (url, full_name) in enumerate(people.items(), 1):
                first, last = split_name(full_name)
                print(f"[{i}/{len(people)}] {full_name}")
                job_title = None
                if not args.skip_titles:
                    job_title = get_job_title(page, url, matcher)
                    print(f"    title: {job_title or '(not found)'}")
                    polite_sleep(args.min_delay, args.max_delay)
                email = None
                if not args.skip_emails:
                    email = get_email(page, url)
                    if email:
                        print(f"    email: {email}")
                    polite_sleep(args.min_delay, args.max_delay)
                row = {
                    "company_name": company_name,
                    "first_name": first,
                    "last_name": last,
                    "job_title": job_title or "",
                    "linkedin_profile_url": url,
                    "email": email or "",
                }
                append_csv(csv_path, row)                 # saved immediately
                if db_path:
                    write_sqlite(db_path, [{**row, "job_title": job_title, "email": email}])
        finally:
            ctx.close()
    print(f"\nDone. Results are in {csv_path}" + (f" and {db_path}" if db_path else ""))
if __name__ == "__main__":
    main()
