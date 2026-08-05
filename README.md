# scalable_auditing_s1

Scalable auditing pipeline for JLexAI legislation — laws (قوانين), bylaws (أنظمة), and any audit type added later.

Volunteers audit sampled legislation through a web UI. Results land in a database, mirror to Google Sheets, and roll up to a quality score with a confidence interval and an issue breakdown chart.

---

## Setup (Windows 10, Python 3.11)

```bat
py -3.11 -m venv s1
s1\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
py scripts\bootstrap.py
py scripts\doctor.py
```

`doctor.py` must exit 0 before anything else is built on top. Set `PYTHONUTF8=1` permanently (`setx PYTHONUTF8 1`, then reopen VS Code) — without it, Arabic breaks in the console and eventually inside the logging handler.

---

## Layout

| path | purpose |
|---|---|
| `configs/` | one YAML spec per audit type — adding an audit type is a config change, not a code change |
| `data/raw/` | source data exactly as received; never edited in place |
| `data/interim/` | working copies |
| `data/processed/` | pipeline output |
| `logs/` | `s1.log` (human, rotating) and `s1.jsonl` (structured, queryable) |
| `src/core/` | configuration and logging |
| `src/db/` | schema and migrations |
| `src/ingest/` | source adapters |
| `src/sampling/` | seeded, reproducible sample drawing |
| `src/scoring/` | quality scores, confidence intervals, inter-auditor agreement |
| `src/sync/` | Google Sheets mirror |
| `src/web/` | FastAPI + Jinja2/HTMX auditor UI |
| `scripts/` | operational entry points |
| `tests/` | pytest suite |

---

## Design decisions

**The database is the source of truth. Google Sheets is a synced mirror.**
Sheets has no transactions, no row locking, and a hard write quota. With concurrent volunteers over months, Sheets-as-backend produces silent lost updates. Managers still get their sheet; integrity stays in the DB.

**No hardcoded paths.** Everything comes from `.env`. The same code runs on Windows locally and Linux on the server; moving is a config edit.

**Every stage is logged.** Start, duration, outcome and row counts, under one `run_id` per execution. Isolate a run with `findstr "<run_id>" logs\s1.jsonl`.

**Sampling is seeded.** `RANDOM_SEED` never changes mid-project — changing it invalidates comparison against earlier audit rounds.

**One account per auditor.** No shared logins. Per-auditor identity is what makes inter-auditor agreement, golden-set scoring, and session resume possible; a shared account silently invalidates all three.

---

## Audit design

### Sample sizes
100 laws + 100 bylaws per audit type, drawn independently per type.

### Audit types

| type | unit | notes |
|---|---|---|
| **metadata** | one legislation | status (الحالة) is the highest-weighted field, then name, year, number |
| **chain** (التسلسل) | one legislation's full amendment chain | relational defects (a missing amendment) are invisible from a single link |
| **reflection** | one article | only articles an amendment actually touches, plus a control set of untouched ones |
| **article integrity** | one article | for legislation with no amendments — a different question with its own rubric |

### Sampling rules

- **Amended legislation** — census of all amendment-touched articles, not a sample.
- **Unamended legislation, 7+ articles** — 4 articles drawn segment-randomly from the middle range (20th–80th percentile), split into 4 segments, one random draw per segment, fixed seed.
- **Unamended legislation, ≤ 6 articles** — census.
- Positional sampling (first two / last two) is deliberately avoided: those articles are formulaic (العنوان، التعريفات، النفاذ) and would bias the score upward. Each sampled article's rubric therefore includes an explicit check that the article number matches the source, catching numbering drift.

### Scoring

- Weighted rubric per audit type, defined in the YAML spec — a wrong status cannot cost the same as a typo in a name.
- Score computed **per legislation first, then averaged across legislations** — otherwise heavily-amended items dominate the result.
- Reported with a confidence interval, adjusted for clustering where articles are drawn within legislations.
- ~10–15% of items double-audited to report inter-auditor agreement alongside the score.
- ~5% golden-set items with known answers to detect an auditor clicking through.

### Answer options

Every item offers **correct / incorrect / cannot determine**, plus structured defect fields and a free-text note. "Cannot determine" is mandatory — without it auditors guess, and guesses become data. Structured defect fields are what produce the chart; free text alone cannot be aggregated.

---

## Conventions

- Versioning: semantic tags (`v0.1.0` = config + logging).
- Every step documented in `docs/` before it is considered done.
- `data/`, `logs/` and `.env` are gitignored. Check `git status` before the first commit.
