#!/usr/bin/env python3
"""
linkedin_people_scraper.py
Collects employees listed on a LinkedIn company "People" page and APPENDS them
to a SQLite table with the columns:
    company_name | first_name | last_name | job_title | linkedin_profile_url | email
- job_title: taken directly from the person's card (headline) on the People page.
- email:     always NULL (profile pages are no longer visited). The column is
             nullable so it can be filled later by another process.
Usage:
    python linkedin_people_scraper.py "https://www.linkedin.com/company/acme/people/" \
        --sqlite all_people.db --max-people 50
On first run, a browser window opens; log in to LinkedIn manually, then press
Enter in the terminal. The session is saved in ./li_browser_profile for reuse.
NOTE: Automated scraping violates LinkedIn's User Agreement and may get the
account restricted or banned. Scraped personal data is subject to privacy laws
(GDPR, CCPA, etc.). Use responsibly and at your own risk.
"""
import argparse
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
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
    m = re.search(r"linkedin\.com/in/([^/?#]+)", href or "")
    if not m:
        return None
    return f"https://www.linkedin.com/in/{m.group(1)}/"
def split_name(full_name: str) -> tuple[str, str]:
    """Strip credentials/emoji/parentheticals and split into first/last."""
    name = full_name.strip()
    name = re.sub(r"\(.*?\)", "", name)            # Jane (JJ) Doe -> Jane Doe
    name = name.split(",")[0]                      # Jane Doe, MBA -> Jane Doe
    name = re.sub(r"[^\w\s'\-.]", "", name)        # drop emoji / symbols
    name = re.sub(r"\s+", " ", name).strip()
    parts = name.split(" ")
    if not parts or not parts[0]:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])
def clean_title(title: str | None) -> str:
    """Collapse whitespace and strip the LinkedIn 'Full-time' style suffix."""
    if not title:
        return ""
    title = title.split("\u00b7")[0]   # drop "· Full-time"
    return re.sub(r"\s+", " ", title).strip()
# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def init_sqlite(db_path: Path) -> None:
    """Create the table if it doesn't exist. Email is nullable."""
    con = sqlite3.connect(db_path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS employees (
               company_name        TEXT NOT NULL,
               first_name          TEXT,
               last_name           TEXT,
               job_title           TEXT,
               linkedin_profile_url TEXT NOT NULL,
               email               TEXT,
               PRIMARY KEY (company_name, linkedin_profile_url)
           )"""
    )
    con.commit()
    con.close()
def load_existing(db_path: Path, company_name: str) -> set[str]:
    """Return the set of profile URLs already saved for this company."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT linkedin_profile_url FROM employees WHERE company_name = ?",
        (company_name,),
    ).fetchall()
    con.close()
    return {r[0] for r in rows}
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
# JS run on the People page: find each person card and pull profile URL,
# name, and the headline/title shown on the card.
EXTRACT_PEOPLE_JS = """
() => {
  const out = [];
  const anchors = document.querySelectorAll('main a[href*="/in/"]');
  for (const a of anchors) {
    const card = a.closest('li') || a.parentElement;
    let name = '';
    let title = '';
    const titleEl = card && (
      card.querySelector('.artdeco-entity-lockup__title') ||
      card.querySelector('.org-people-profile-card__profile-title')
    );
    if (titleEl) name = titleEl.innerText;
    if (!name) name = (a.innerText || a.getAttribute('aria-label') || '');
    name = name.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
    const subEl = card && card.querySelector(
      '.artdeco-entity-lockup__subtitle, ' +
      '.org-people-profile-card__profile-headline, ' +
      '.org-people-profile-card__card-subtitle'
    );
    if (subEl) {
      title = subEl.innerText;
    } else if (card) {
      // Fallback: second visible text line of the card is usually the title.
      const lines = (card.innerText || '').split('\\n')
        .map(s => s.trim()).filter(Boolean);
      title = lines.length > 1 ? lines[1] : '';
    }
    out.push({ href: a.href, name: name, title: title });
  }
  return out;
}
"""
def collect_people(page, people_url: str, max_new: int,
                   known_urls: set[str]) -> dict[str, dict]:
    """
    Scroll the People page until `max_new` previously-unseen people are found.
    Returns {profile_url: {"name": ..., "title": ...}}.
    """
    page.goto(people_url, wait_until="domcontentloaded")
    polite_sleep(3, 5)
    people: dict[str, dict] = {}
    stale_rounds = 0
    while stale_rounds < 4:
        before = len(people)
        for item in page.evaluate(EXTRACT_PEOPLE_JS):
            url = clean_profile_url(item["href"])
            name = (item["name"] or "").strip()
            title = clean_title(item["title"])
            # Out-of-network members show as "LinkedIn Member" with no usable URL
            if not url or not name or name.lower() == "linkedin member":
                continue
            if url not in people:
                people[url] = {"name": name, "title": title}
            elif not people[url]["title"] and title:
                people[url]["title"] = title
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
    new_only = {u: p for u, p in people.items() if u not in known_urls}
    return dict(list(new_only.items())[:max_new])
# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape a LinkedIn company People page.")
    ap.add_argument("people_url",
                    help="e.g. https://www.linkedin.com/company/acme/people/")
    ap.add_argument("--sqlite", required=True,
                    help="SQLite DB path (table: employees)")
    ap.add_argument("--max-people", type=int, default=100,
                    help="Max NEW profiles to save per run (default 100)")
    ap.add_argument("--min-delay", type=float, default=4.0)
    ap.add_argument("--max-delay", type=float, default=9.0)
    ap.add_argument("--profile-dir", default="li_browser_profile",
                    help="Where the persistent browser session is stored")
    args = ap.parse_args()
    people_url, slug = normalize_people_url(args.people_url)
    db_path = Path(args.sqlite)
    init_sqlite(db_path)
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
            print(f"Company: {company_name} (slug={slug})")
            known_urls = load_existing(db_path, company_name)
            print(f"{len(known_urls)} profiles for this company already saved.")
            people = collect_people(page, people_url, args.max_people, known_urls)
            print(f"{len(people)} new profiles to process.")
            for i, (url, info) in enumerate(people.items(), 1):
                first, last = split_name(info["name"])
                print(f"[{i}/{len(people)}] {info['name']} | {info['title'] or '(no title)'}")
                row = {
                    "company_name": company_name,
                    "first_name": first,
                    "last_name": last,
                    "job_title": info["title"] or None,
                    "linkedin_profile_url": url,
                    "email": None,
                }
                write_sqlite(db_path, [row])   # saved immediately
        finally:
            ctx.close()
    print(f"\nDone. Results are in {db_path}")
if __name__ == "__main__":
    main()