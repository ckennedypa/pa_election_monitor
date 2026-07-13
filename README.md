# pa_election_monitor
script that monitors major changes or announcements from the 67 county boards of election in pennsylvania as well as the pa department of state

DISCLOSURE: this is a volunteer-made python script created with Claude, and might make mistakes. This should not be used as the sole source of information in its current iteration, as testing of it is still necessary. To send feedback, please email ckennedypa@proton.me.

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
