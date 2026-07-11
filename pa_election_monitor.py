#!/usr/bin/env python3
"""
pa_election_monitor.py

Monitors the PA Department of State newsroom plus all 67 county election
board / voter services pages for changes, flags noteworthy announcements
(deadline changes, polling place closures, audits, recounts, litigation,
etc.), and keeps a running snapshot so each run only shows what's new.

FIRST RUN just establishes a baseline for every source (nothing to compare
yet). Run it again later -- daily via cron/launchd is typical -- to see
what changed since last time.

USAGE
-----
    pip3 install requests beautifulsoup4 --break-system-packages

    python3 pa_election_monitor.py --sources election_monitor_sources.csv

    # tune concurrency / politeness / sensitivity
    python3 pa_election_monitor.py --sources election_monitor_sources.csv \
        --workers 6 --delay 0.75 --similarity-threshold 0.98

INPUT
-----
--sources CSV: Name, URL, Type
    Type is just a label (e.g. "state"/"county") used for grouping in the
    report -- edit or extend election_monitor_sources.csv freely, it already
    ships with the PA DOS newsroom + all 67 county pages pre-filled from
    pa.gov's official county contact directory.

STATE / SNAPSHOTS
------------------
One JSON snapshot per source is kept under --state-dir (default
./election_monitor_state/). Delete that folder to wipe baselines and start
fresh. Snapshots store the cleaned page text, not raw HTML, so diffs ignore
markup churn.

WHY SIMILARITY THRESHOLD + KEYWORDS
------------------------------------
Government sites change constantly in trivial ways (a "last updated" date,
a rotating banner). Rather than flag every twitch, a page only gets flagged
as CHANGED if either:
  (a) the overall text similarity to the last snapshot drops below
      --similarity-threshold (default 0.985), or
  (b) any added text contains one of the PRIORITY_KEYWORDS below --
      these always get flagged even if the change is small, since a single
      new line like "Polling place moved" matters far more than its length
      suggests.
Edit PRIORITY_KEYWORDS to match what you personally care about tracking.
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
from difflib import SequenceMatcher, unified_diff
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; DelcoAccentElectionMonitor/1.0; +https://delcoaccent.org)"
REQUEST_TIMEOUT = 20

# Always-flag keywords -- edit freely to match what you care about tracking.
PRIORITY_KEYWORDS = [
    "recount", "audit", "lawsuit", "litigation", "consent decree",
    "polling place", "poll location", "closed", "closure", "relocat",
    "emergency", "extend", "extension", "deadline", "cancel",
    "recall", "special election", "vacan", "certif", "provisional",
    "drop box", "ballot return", "curing", "cure", "reject", "security breach",
    "resign", "appoint", "hire", "hearing", "meeting notice",
]

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


def find_priority_hits(diff_lines):
    added_text = " ".join(
        ln[1:] for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++")
    ).lower()
    return sorted({kw for kw in PRIORITY_KEYWORDS if kw in added_text})


def check_source(state_dir, name, url, text, similarity_threshold):
    """Returns a tuple describing this source's status: 'new', 'unchanged', or a change record."""
    prior = load_snapshot(state_dir, name)
    if prior is None:
        save_snapshot(state_dir, name, url, text)
        return ("new",)

    new_hash = hash_text(text)
    if new_hash == prior["hash"]:
        return ("unchanged",)

    ratio = SequenceMatcher(None, prior["text"], text).ratio()
    diff_lines = list(unified_diff(prior["text"].splitlines(), text.splitlines(), lineterm="", n=0))
    hits = find_priority_hits(diff_lines)

    save_snapshot(state_dir, name, url, text)  # always advance the snapshot

    if ratio < similarity_threshold or hits:
        added = [ln[1:].strip() for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++")]
        added = [ln for ln in added if ln][:8]
        return ("changed", ratio, hits, added)

    return ("unchanged",)


def print_report(new_baselines, changed, unchanged, errors):
    total = len(new_baselines) + len(changed) + len(unchanged) + len(errors)
    print("=" * 70)
    print(f"PA ELECTION BOARD MONITOR -- {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)
    print(f"Checked: {total}  |  Changed: {len(changed)}  |  Unchanged: {len(unchanged)}"
          f"  |  New baselines: {len(new_baselines)}  |  Errors: {len(errors)}")
    print()

    if changed:
        print("-- CHANGES DETECTED (flagged keyword matches shown first) --")
        for name, url, type_, ratio, hits, added in sorted(changed, key=lambda c: -len(c[4])):
            flag = f"  [FLAGGED: {', '.join(hits)}]" if hits else ""
            print(f"\n* {name} ({type_}){flag}")
            print(f"  {url}")
            print(f"  text similarity to last check: {ratio:.3f}")
            for line in added[:5]:
                print(f"    + {line[:120]}")
    else:
        print("No changes detected since last run.")

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
        writer.writerow(["Name", "URL", "Type", "SimilarityRatio", "FlaggedKeywords", "SampleAddedText"])
        for name, url, type_, ratio, hits, added in changed:
            writer.writerow([name, url, type_, f"{ratio:.3f}", "; ".join(hits), " | ".join(added[:3])])
    return path


def main():
    parser = argparse.ArgumentParser(description="Monitor PA county election boards + DOS for changes")
    parser.add_argument("--sources", required=True, help="CSV of Name,URL,Type to monitor")
    parser.add_argument("--state-dir", default="./election_monitor_state", help="Where snapshots are stored")
    parser.add_argument("--outdir", default="./election_monitor_reports", help="Where reports are saved")
    parser.add_argument("--workers", type=int, default=5, help="Concurrent fetches (keep this modest -- be polite)")
    parser.add_argument("--delay", type=float, default=0.5, help="Extra random delay (0-x sec) staggering requests")
    parser.add_argument("--similarity-threshold", type=float, default=0.985,
                         help="Below this text-similarity ratio (0-1), a page counts as changed")
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

        status = check_source(state_dir, name, url, text, args.similarity_threshold)
        if status[0] == "new":
            new_baselines.append((name, url, type_))
        elif status[0] == "unchanged":
            unchanged.append((name, url, type_))
        else:
            _, ratio, hits, added = status
            changed.append((name, url, type_, ratio, hits, added))

    print_report(new_baselines, changed, unchanged, errors)
    csv_path = write_csv_report(changed, outdir)
    if csv_path:
        print(f"\nSaved change log: {csv_path}")


if __name__ == "__main__":
    main()
