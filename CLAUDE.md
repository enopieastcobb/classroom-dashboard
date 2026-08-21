# CLAUDE.md — classroom-dashboard

Operating manual for this repository. If you are an AI agent, read this first,
then `english_booklet_spec.md`. If you are a person, read this and then the two
tracker modules — the comments in them carry the reasoning, not just the what.

**Service:** classroom-dashboard · **Owner:** Enopi East Cobb (Eye Level
learning centre, Marietta GA) · **Users:** three staff accounts, six teachers,
around sixty children · **Runtime:** Cloud Run behind IAP at
`classroom.enopieastcobb.com`

---

## 1. What it does, and why it exists

Two tools for running the centre's sessions, on one Cloud Run service.

**The session dashboard** (`/dashboard`) shows a teacher every child scheduled in
the hour in front of them, with their outstanding work read live from Google
Classroom. Its most important feature is the **booklet handover alert**: from the
fiftieth minute of a session until midnight, a banner names any child who has not
been handed the booklets they are due.

That alert is the reason the project exists. The centre's core operational
failure was a teacher getting busy and forgetting to hand over the next maths
booklet. Nobody noticed until the child was home; the week was wasted; and a
parent who spotted it before the centre did lost confidence in how the place was
run. Everything about the booklet logic serves catching that while it can still
be put right.

**The attendance register** (`/attendance`) is for an admin walking the rooms
with a tablet, tapping names to mark children present.

---

## 2. Repository map

```
main.py                  FastAPI app: routes, auth, Classroom reading, both pages
booklet_tracker.py       MATHS booklet rules (BTM/CTM)
english_tracker.py       ENGLISH booklet rules (reading + essay writing)
schedule_service.py      Reads the weekly schedule Google Sheet
attendance_service.py    Firestore reads and writes for attendance
templates/
  session_dashboard.html the teacher's session view + handover banner
  attendance.html        the register (tap to mark)
  attendance_report.html attendance history + who was away
  login.html             Google Sign-In, targets /dashboard or /attendance
english_booklet_spec.md  every English rule, and where each came from
booklet_naming_legend.html  the teacher-facing naming standard (published as an Artifact)
test_*.py                regression suites — see §9
Dockerfile               python:3.12-slim, installs requirements, runs uvicorn
.gcloudignore            keeps venv/ and __pycache__ out of the build (see §8)
```

The two tracker modules are **deliberately separate**. The curricula disagree on
almost everything (§6 vs §7), so one shared code path would mean a rule change in
one room quietly altering the other.

---

## 3. Stack

- Python 3.12, FastAPI 0.139.2, Starlette 1.3.1, Jinja2, uvicorn
- `google-api-python-client` for Classroom + Sheets (REST/JSON)
- `google-cloud-firestore` for attendance (gRPC — the only gRPC client here)
- `requirements.txt` is **exactly pinned**, all 47 packages

`protobuf` is held at **6.33.6**, below 7.0, because `google-cloud-firestore`
caps it there while everything else accepts anything under 8.0. A hard pin at
7.35.1 once made the set unsatisfiable and failed the build outright. Raise it
only alongside a Firestore release that permits protobuf 7.

---

## 4. How it runs on GCP

```
browser → HTTPS load balancer → IAP → Cloud Run (classroom-dashboard) → Google APIs
                                 │
                                 └── restricts access to three staff accounts
```

| | |
|---|---|
| Project | `enopieastcobb-studentprogress` (number 812599621244) |
| Region | `us-east1` |
| Domain | `classroom.enopieastcobb.com` (Cloudflare DNS → LB IP, A record) |
| Service account | `classroom-dashboard-sa@enopieastcobb-studentprogress.iam.gserviceaccount.com` |
| Invoker | `--no-allow-unauthenticated`; IAP is the only caller |
| Allowed users | `admin@`, `teacher@`, `administrativestaff@enopieastcobb.com` |

### Identity, and the one thing worth changing

Access is authenticated **twice**, redundantly. IAP authenticates every request
and restricts it to the three accounts. Then the app *also* runs Google Sign-In
and verifies the resulting ID token.

That second sign-in is the source of the "timed out too soon" complaint: Google
ID tokens live one hour, and the token is issued once and echoed into a hidden
field on every form. A lapsed sign-in now returns the sign-in prompt carrying the
day, room and hour, with `data-auto_select` so Google often reissues with no
click. **The real fix, not yet done, is to take the identity from IAP's
`X-Goog-Authenticated-User-Email` header and drop the second sign-in.** With
`--no-allow-unauthenticated`, only IAP can reach the service, so that header
cannot be set by an outside caller.

### Reading Classroom as the teacher

Classroom data is read with **domain-wide delegation** using IAM `signJwt` — there
is no service-account key file anywhere. `get_scoped_creds(scopes, subject=...)`
mints a token for the impersonated user. Scopes:

```
classroom.courses.readonly
classroom.rosters.readonly
classroom.student-submissions.me.readonly
classroom.student-submissions.students.readonly
classroom.topics.readonly
spreadsheets.readonly          (schedule Sheet, minted separately with NO subject)
```

The Sheets token must be minted separately: Application Default Credentials carry
only `cloud-platform`, which Sheets rejects.

### Environment variables

| | |
|---|---|
| `SERVICE_ACCOUNT_EMAIL` | required |
| `GOOGLE_CLIENT_ID` | required, for Sign-In verification |
| `SCHEDULE_SPREADSHEET_ID` | optional; defaults to the live sheet |
| `ENABLE_TIME_OVERRIDE` | `1` shows a "pretend the time is" box for testing. **Off in production** — while on, entering a time also stands down the session-date check, so a teacher could see a banner for a session that is not today |

Env vars survive `gcloud run deploy --image`. Only `--set-env-vars` and
`--clear-env-vars` wipe them.

### Caching

Nothing about an assignment is ever cached — a child can turn work in at any
moment. Only tokens (50 min), the schedule Sheet (5 min) and the Classroom class
*list* (60 s).

---

## 5. Building it from nothing

```bash
gcloud services enable run.googleapis.com iap.googleapis.com \
  classroom.googleapis.com sheets.googleapis.com iamcredentials.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com

gcloud artifacts repositories create classroom-repo --repository-format=docker --location=us-east1

gcloud iam service-accounts create classroom-dashboard-sa
gcloud projects add-iam-policy-binding PROJECT --member=serviceAccount:SA --role=roles/logging.logWriter
gcloud projects add-iam-policy-binding PROJECT --member=serviceAccount:SA --role=roles/datastore.user

gcloud firestore databases create --location=nam5     # Native mode, free tier
```

Then, by hand: enable domain-wide delegation for the service account in the
Workspace admin console with the scopes in §4; create an OAuth client ID for
Sign-In; verify the domain in Search Console; map the domain; enable IAP on the
backend service and add the three accounts as IAP-secured Web App Users.

**Cost:** Cloud Run scales to zero; Firestore's free tier (20k writes, 50k reads
a day) dwarfs this usage — around 60 writes a day, a few MB a year. Cloud SQL was
rejected at $10–25/month idle.

---

## 6. Maths booklet rules — `booklet_tracker.py`

24 levels. Within a level, booklets **1–18 are Basic Thinking (BTM)** and **19–30
are Critical Thinking (CTM)**.

| | |
|---|---|
| Weekly issue | **2 BTM + 1 CTM** |
| Counted by | what the child is **currently holding**, not what was issued today, and by **booklet** not by record |
| Open FIC | blocks the next booklet in that series; the other series carries on |
| Level test | gates the next level; pass mark **80** |
| Supplementary packet | one per level, expected from booklet 4, must be finished before the test |
| Grade cap | checked to grade 5; above that, silent |

### Level ordering is not always ascending

- **15/16 may be taken in either order.** Coming out of level 14 a younger child
  is often given 16 (order of operations) before 15 (long division), because long
  division frustrates them and stalling there costs more than the sequence.
  `SWAPPABLE_LEVELS = {15, 16}` — for that pair only, **recency** decides which
  level the child is on, not the higher number. Everywhere else the highest
  number is the position, and no other level is ever skipped.
- **17/18 may run as two live tracks at once**, for a child who has passed 15 or
  16 and is already ahead in school maths. Detected, never predicted — it depends
  on how far ahead they are at school, which Classroom does not record. The
  signal is level 17 unfinished while 18 has started, which sequential
  progression cannot produce. The run **ends when either track reaches booklet
  18**: the child moves to that level's test and the other track is closed.
- The weekly quota stays 2 BTM however the booklets split across tracks.

### Failed level tests

`REDO: 12-19, 12-20` in the test's **description** lists booklets to redo; those
drive the week's issue until cleared. `ADVANCE: <reason>` records a deliberate
decision to move on despite a fail. A failing score with neither is flagged.
Comments are never read — the Classroom API exposes no comment endpoint.

### New joiners

A level test is expected only once the record stretches back
`HISTORY_REQUIRED_WEEKS = 6`. Below that nothing about a level boundary is
asserted — no test, no packet, no push into the next level. A child who joined a
fortnight ago crossed that boundary somewhere else.

---

## 7. English booklet rules — `english_tracker.py`

Full detail and provenance: **`english_booklet_spec.md`**. Summary:

| | Reading | Essay writing |
|---|---|---|
| Levels | `A B C D E F G H I 6 7 8` | `A`–`M` (nothing past E/F reached) |
| Booklets per level | 30 | 1 for A–E; 4 from F |
| Cadence | **1 a week** | not weekly — one booklet spans a level (A–E) or 6–8 weeks (F+) |
| Grades | checked to 4; **flagged** above | grade 1 and below only |
| Level test | at booklet 28, runs **across** the boundary | none |

- Levels `6 7 8` are **digits, and exist in both curricula.** An unprefixed
  `6-17 HC` is undecidable, so it is **maths unless labelled `ENG`**.
- The level test is due at booklet 28, tolerated through booklet 2 of the next
  level, then prominent. A *missing* record stops being reported ten booklets on
  — a level takes seven or eight months, and old tests do not survive in
  Classroom, so absence stops being evidence.
- Writing progression waits for the **due date**, not just completion: F1 went to
  every first grader with a September due date, and "finished" alone would have
  demanded F2 from all of them in August.
- Grades below 1 are **named, not numbered**: `Gr K`→0, `Pre-K`→−1,
  `Weenopi`→−2 (the three- and four-year-olds). All get booklets in all four
  series. `Gr. 1`, `Grade-1` and `1st Grade` also parse.

---

## 8. Naming standard, and the topic model

Teacher-facing legend: `booklet_naming_legend.html` (published as an Artifact).

```
[REDO|REISSUE|FINISH] SERIES level-booklet [HC] [FIC]
TEST: SERIES Level level
Essay Writing: level-booklet [HC]
```

`SERIES` is one of `BTM` `CTM` `ENG` `Essay Writing:` — **always required**,
because `6-12 HC` alone could be either room. `REDO` (after a failed test) and
`REISSUE` (lost book, a second copy really handed over) create a second record
and are exempt from the duplicate check. `FINISH` (came back unfinished, another
week) **edits the existing row**, so two `FINISH` rows for one booklet is a real
mistake and is *not* exempt.

The modifier goes **in front of** the series, never instead of it: `REDO 12-9 HC`
makes the booklet invisible to the parser, which is worse than no prefix.

### Classroom's topic is the state machine

`Math Classwork` / `Math Homework` → `To Be Graded` → `Graded Assignments`.
Status comes from the **topic**, not the submission state. `main.py` splits
`"Math Classwork"` into `subject="Math"` + `topic="Classwork"` before the
trackers see it.

**`Graded Assignments` is one topic shared by both rooms and names no subject**,
so `main.py` guesses from the title and defaults to English. Items therefore
carry `subject_stated` — only a topic that *names* the subject settles the room.
Trusting the guess is what once handed maths booklets at levels 6/7/8 to the
English checker.

**Duplicates** are flagged when the same booklet appears twice, the child is
still holding a copy, and one copy was posted within `DUPLICATE_LOOKBACK_WEEKS =
10`. Drafts never arrive: coursework is read `PUBLISHED`-only, because a draft is
invisible to the child and so counts as not handed over.

---

## 9. The attendance register

`/attendance` (marking) and `/attendance/report` (history + absence).

- **Presence only.** No absent flag — a scheduled child with no record was not
  there. Tapping again clears a mis-tap.
- **Firestore**, collection `attendance`, document id **`{date}_{name-slug}`** —
  deterministic, so two devices marking the same child write one row.
- One screen per day, grouped **hour → room → teacher**, so nobody changes a
  dropdown at each doorway. Tiles are 64px buttons; a tap posts and flips in
  place, reverting if the write fails.
- **Make-ups** sit in the ordinary class. **Help sessions** (same dated table in
  the Sheet, marked `Help` in its Notes column — for children carrying more FICs
  than their own hour can clear) get **their own room**, because they are usually
  supervised separately by a grader. **Walk-ins** are added in the room, and need
  no storage of their own: the attendance record *is* the walk-in.
- **Absence is judged per hour, and only for hours that have finished.** At four
  o'clock the five and six o'clock children have not arrived. Computed against
  the *current* schedule, so it is reliable for recent dates and stated as such
  on the page.

---

## 10. Tests

No pytest runner is wired in; each suite is a script that prints `all pass`.

```bash
python3 test_english_tracker.py     # 60 parser cases + 14 writing titles
python3 test_english_review.py      # English review, real students
python3 test_maths_duplicates.py    # duplicate rules, both exemptions
python3 test_new_joiner.py          # 6-week rule, named grades, writing levels
```

Every fixture uses **real titles and real students** from Classroom screenshots.
That is deliberate: the parsers broke repeatedly on shapes I had invented.
`test_integration.py`, `test_processing.py` and `verify_dwd.py` predate this and
are unverified.

**When adding a fixture, match the real item shape**: `topic` is the *stripped*
form (`"Classwork"`, not `"Math Classwork"`), `subject` is set, and
`subject_stated` is `False` for the shared Graded topic. A fixture built from the
same wrong assumption as the code will agree with it and prove nothing.

---

## 11. Deploy — and the traps that cost real time

```bash
# laptop
git push origin main

# Cloud Shell (~/classroom-dashboard)
git pull && git log --oneline -1        # confirm the commit you expect
gcloud builds submit --tag $FULL_IMAGE_PATH .
gcloud run deploy classroom-dashboard --image $FULL_IMAGE_PATH --region us-east1 \
  --service-account=classroom-dashboard-sa@enopieastcobb-studentprogress.iam.gserviceaccount.com \
  --no-allow-unauthenticated
```

Three copies of the code exist — laptop, GitHub, Cloud Shell — and each of these
has already happened once:

1. **Cloud Shell not pulled.** A whole session chased phantom bugs because every
   build rebuilt the same stale code. Always check the commit hash *and* grep for
   a symbol from the newest change before building.
2. **The deploy named the wrong image.** `classroom-repo` holds two images:
   `classroom-dashboard` (what every build updates) and `dashboard-app`
   (abandoned, from the first week). Deploying `dashboard-app` succeeds and
   silently serves month-old code. Tracebacks citing `launch_entry` at line 215
   or `course_picker.html` are its fingerprint. **Delete it.**
3. **Reading unfiltered logs as current.** `gcloud logging read` with no
   `--freshness` returns years of history. Always bound it.

The decisive proof that new code is live is a `HANDOVER CHECK` line in the logs,
or the running revision's `status.imageDigest` matching the digest the build
printed. "The deploy succeeded" is not the same as "the right code deployed".

**Tag builds by commit** (`:$(git rev-parse --short HEAD)`) and a wrong image name
fails loudly instead of silently. Still not done.

---

## 12. Debugging the banner

Every `/dashboard` request logs one line, whether or not the banner fires:

```
HANDOVER CHECK day=Saturday time=9:00 AM subject=Math | now=... session_date=... window_open=False | Dhir [Gr 7/8]: no findings ; ...
```

It exists because "the banner didn't appear" has several causes — outside the
window, wrong room, no booklets recognised, grade capped — and without the line
none of them can be told apart afterwards. The grade in brackets is there because
an unset Section reads as unknown and is checked anyway.

---

## 13. Open items

- Take identity from IAP and drop the second sign-in (§4)
- Delete the `dashboard-app` image; tag builds by commit (§11)
- Artifact Registry cleanup policy is set to **dry-run**, `keepCount: 2` — decide
  before enabling; 2 leaves no rollback
- `venv/` and ~5,000 `.pyc` files are still tracked in git. Ignored now, so they
  no longer reach the image, but the repo carries 88 MB of history
- Absence compares against the *current* schedule; snapshotting each session's
  roster at marking time would make old figures exact
- A child with several open FICs is the signal a help session is needed, and no
  banner line says so

---

## 14. How to work on this

**Ask before assuming a curriculum rule.** Almost every wrong alert in this
project came from a plausible inference: that levels ascend, that a booklet
issued today is this week's issue, that "furthest" means highest-numbered, that a
subject named on an item was stated rather than guessed. The rules are specific,
occasionally counter-intuitive, and only the centre knows them.

**Prefer silence to a confident guess.** A wrong position names the wrong booklet
*and* invents a missing test; a missing position just says less. Where the data
cannot settle something — an unreadable grade, a boundary a new joiner crossed
elsewhere, an hour that has not finished — say nothing.

**Verify against real data before reporting success.** Several fixes were
declared done on the strength of fixtures that shared the code's own wrong
assumption. If a claim can be checked against a real student, check it.
