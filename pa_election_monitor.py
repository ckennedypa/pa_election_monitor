#!/usr/bin/env python3
"""
pa_election_monitor.py

Monitors the PA Department of State newsroom plus all 67 county election
board / voter services pages for changes -- but only notifies you when a
change touches voter protection or a public hearing, and tells you exactly
what the new text says (not just "something changed").

CATEGORIES IT WATCHES FOR
--------------------------
  - Polling Place Changes            (relocations, consolidations)
  - Closures & Emergencies           (closed offices, weather, power outages)
  - Registration & Deadlines         (new/extended registration or voting deadlines)
  - Ballot Curing & Provisional      (curing process, provisional/rejected ballots)
  - Mail & Drop Box Voting           (drop box changes, mail/absentee ballot rules)
  - Accessibility & Language Access  (ADA, interpreters, curbside voting)
  - Legal Action & Election Integrity (lawsuits, recounts, audits, security)
  - Public Hearings & Meetings       (board of elections meetings, canvass/
                                       certification hearings, public comment)

Everything else -- a rotating banner, a staff photo, a font tweak -- is
ignored on purpose, even if the page's text technically changed. Edit
VOTER_PROTECTION_CATEGORIES below to add/remove phrases for what you
personally want flagged.

FIRST RUN just establishes a baseline for every source (nothing to compare
yet). Run it again later -- daily via cron/launchd/GitHub Actions -- to see
what changed since last time.

USAGE
-----
    pip3 install requests beautifulsoup4 --break-system-packages
    python3 pa_election_monitor.py --sources election_monitor_sources.csv

INPUT
-----
--sources CSV: Name, URL, Type
    Type is just a label (e.g. "state"/"county") used for grouping in the
    report.

STATE / SNAPSHOTS
------------------
One JSON snapshot per source is kept under --state-dir (default
./election_monitor_state/). Delete that folder to wipe baselines and start
fresh. Snapshots store the cleaned page text, not raw HTML, so diffs ignore
markup churn.
"""

import argparse
import csv
import hashlib
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import unified_diff
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; DelcoAccentElectionMonitor/1.0; +https://delcoaccent.org)"
REQUEST_TIMEOUT = 20

# Only text added under one of these categories triggers a notification.
# Edit freely -- add phrases, add/remove whole categories.
VOTER_PROTECTION_CATEGORIES = {
    "Polling Place Changes": [
        "polling place", "poll location", "voting location", "relocat", "consolidat",
    ],
    "Closures & Emergencies": [
        "closed", "closure", "emergency", "cancel", "delayed opening",
        "power outage", "evacuat", "inclement weather",
    ],
    "Registration & Deadlines": [
        "deadline", "extend", "extension", "register to vote",
        "registration deadline", "last day to", "voter registration",
    ],
    "Ballot Curing & Provisional Ballots": [
        "curing", "cure your ballot", "provisional ballot", "reject",
        "naked ballot", "signature mismatch", "cured by",
    ],
    "Mail & Drop Box Voting": [
        "drop box", "mail ballot", "mail-in ballot", "absentee ballot", "ballot return",
    ],
    "Accessibility & Language Access": [
        "ada accessib", "language access", "interpreter", "accessible voting", "curbside voting",
    ],
    "Legal Action & Election Integrity": [
        "lawsuit", "litigation", "consent decree", "recount", "audit",
        "security breach", "recall",
    ],
    "Public Hearings & Meetings": [
        "public hearing", "public comment", "board of elections meeting",
        "canvass meeting", "meeting notice", "special meeting", "agenda",
        "certification hearing",
    ],
}

# Boilerplate that changes on every page load and shouldn't count as a "real" change
NOISE_PATTERNS = [
    r"\b\d{1,2}:\d{2}\s?(AM|PM|am|pm)\b",   # timestamps
    r"©\s?\d{4}",                            # copyright years
    r"Last [Uu]pdated:?.*",                  # "Last updated" lines
]


def clean_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = "\n".join(lines)
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, "", text)
    return text


def fetch(name, url):
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return name, url, clean_text(resp.text), None
    except Exception as e:
        return name, url, None, str(e)


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_filename(name):
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


def load_snapshot(state_dir, name):
    path = state_dir / f"{safe_filename(name)}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_snapshot(state_dir, name, url, text):
    path = state_dir / f"{safe_filename(name)}.json"
    path.write_text(json.dumps({
        "url": url,
        "hash": hash_text(text),
        "text": text,
        "fetched_at": datetime.now().isoformat(),
    }))


def find_category_hits(diff_lines):
    """Returns {category: [matched added lines]} for lines that were newly added."""
    added_lines = [
        ln[1:].strip() for ln in diff_lines
        if ln.startswith("+") and not ln.startswith("+++")
    ]
    added_lines = [ln for ln in added_lines if ln]

    hits = {}
    for category, keywords in VOTER_PROTECTION_CATEGORIES.items():
        matched = [ln for ln in added_lines if any(kw in ln.lower() for kw in keywords)]
        if matched:
            hits[category] = matched[:5]  # cap per-category snippet length
    return hits


def check_source(state_dir, name, url, text):
    """Returns ('new',), ('unchanged',), or ('changed', {category: [lines]})."""
    prior = load_snapshot(state_dir, name)
    if prior is None:
        save_snapshot(state_dir, name, url, text)
        return ("new",)

    new_hash = hash_text(text)
    if new_hash == prior["hash"]:
        return ("unchanged",)

    diff_lines = list(unified_diff(prior["text"].splitlines(), text.splitlines(), lineterm="", n=0))
    category_hits = find_category_hits(diff_lines)

    save_snapshot(state_dir, name, url, text)  # always advance the snapshot

    if category_hits:
        return ("changed", category_hits)

    # page content changed, but nothing voter-protection/hearing relevant -- stay quiet
    return ("unchanged",)


def print_report(new_baselines, changed, unchanged, errors):
    total = len(new_baselines) + len(changed) + len(unchanged) + len(errors)
    print("=" * 70)
    print(f"PA ELECTION BOARD MONITOR -- {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)
    print(f"Checked: {total}  |  Voter-protection/hearing changes: {len(changed)}  |  "
          f"No notable change: {len(unchanged)}  |  New baselines: {len(new_baselines)}  |  "
          f"Errors: {len(errors)}")
    print()

    if changed:
        print("-- VOTER PROTECTION / PUBLIC HEARING CHANGES DETECTED --")
        for name, url, type_, category_hits in changed:
            print(f"\n* {name} ({type_})")
            print(f"  {url}")
            for category, lines in category_hits.items():
                print(f"  [{category}]")
                for line in lines:
                    print(f"    + {line[:200]}")
    else:
        print("No voter-protection or public-hearing changes detected since last run.")

    if new_baselines:
        print(f"\n-- New baselines established for {len(new_baselines)} source(s) "
              "-- nothing to compare yet, changes will show on the next run")

    if errors:
        print(f"\n-- Fetch errors ({len(errors)}) --")
        for name, url, err in errors:
            print(f"  {name}: {err}")


def write_csv_report(changed, outdir):
    if not changed:
        return None
    path = outdir / f"election_monitor_changes_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "URL", "Type", "Category", "ChangeText"])
        for name, url, type_, category_hits in changed:
            for category, lines in category_hits.items():
                for line in lines:
                    writer.writerow([name, url, type_, category, line])
    return path


def main():
    parser = argparse.ArgumentParser(
        description="Monitor PA county election boards + DOS for voter-protection/hearing changes"
    )
    parser.add_argument("--sources", required=True, help="CSV of Name,URL,Type to monitor")
    parser.add_argument("--state-dir", default="./election_monitor_state", help="Where snapshots are stored")
    parser.add_argument("--outdir", default="./election_monitor_reports", help="Where reports are saved")
    parser.add_argument("--workers", type=int, default=5, help="Concurrent fetches (keep this modest -- be polite)")
    parser.add_argument("--delay", type=float, default=0.5, help="Extra random delay (0-x sec) staggering requests")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    sources = []
    with open(args.sources, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sources.append((row["Name"].strip(), row["URL"].strip(), row.get("Type", "").strip()))

    fetch_results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {}
        for name, url, type_ in sources:
            time.sleep(random.uniform(0, args.delay))
            futures[ex.submit(fetch, name, url)] = (name, url, type_)
        for fut in as_completed(futures):
            name, url, text, err = fut.result()
            fetch_results[name] = (url, text, err)

    new_baselines, changed, unchanged, errors = [], [], [], []

    for name, url, type_ in sources:
        _, text, err = fetch_results[name]
        if err:
            errors.append((name, url, err))
            continue

        status = check_source(state_dir, name, url, text)
        if status[0] == "new":
            new_baselines.append((name, url, type_))
        elif status[0] == "unchanged":
            unchanged.append((name, url, type_))
        else:
            _, category_hits = status
            changed.append((name, url, type_, category_hits))

    print_report(new_baselines, changed, unchanged, errors)
    csv_path = write_csv_report(changed, outdir)
    if csv_path:
        print(f"\nSaved change log: {csv_path}")


if __name__ == "__main__":
    main()
