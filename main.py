from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.templating import Jinja2Templates
from googleapiclient.discovery import build
import google_auth_httplib2
import httplib2
import threading
from concurrent.futures import ThreadPoolExecutor
from google.oauth2 import id_token as google_id_token, credentials as oauth2_credentials
from google.auth.transport import requests as transport_requests
import google.auth
import json
from markupsafe import escape
import time
import os
import re
import secrets
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional

from schedule_service import ScheduleService
from booklet_tracker import review_student
from english_tracker import review_student as review_english
import attendance_service as attendance
import email_service

# The centre's local clock. Cloud Run runs in UTC, so the handover window has
# to be worked out in local time or it lands hours off.
try:
    from zoneinfo import ZoneInfo
    CENTER_TZ = ZoneInfo("America/New_York")
except Exception as e:  # pragma: no cover - missing tzdata
    logging.getLogger(__name__).error(f"Timezone data unavailable ({e}); using UTC.")
    CENTER_TZ = None

# The booklet alert runs in the last ten minutes of the session: late enough
# that a teacher has had the lesson to hand books over, early enough that it
# can still be put right before the child leaves.
HANDOVER_ALERT_FROM_MINUTE = 50

# TEMPORARY, for testing the handover alert without waiting for a live session.
# Off unless ENABLE_TIME_OVERRIDE=1 is set on the service, so the control cannot
# appear in normal use. Turn it off again with:
#   gcloud run services update classroom-dashboard --region=us-east1 \
#       --remove-env-vars ENABLE_TIME_OVERRIDE
TIME_OVERRIDE_ENABLED = os.environ.get("ENABLE_TIME_OVERRIDE") == "1"


def now_local() -> datetime:
    """The current moment at the centre."""
    return datetime.now(CENTER_TZ) if CENTER_TZ else datetime.now()


def today_local() -> date:
    """
    Today's date at the centre -- NEVER date.today().

    Cloud Run runs in UTC, which rolls into tomorrow at 8pm local through the
    summer. Using it would mean that every evening the dashboard opens on the
    wrong day, work due today reads as past due, and the session date resolves
    a week ahead.
    """
    return now_local().date()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Load and validate configuration at startup
SA_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
SCHEDULE_SPREADSHEET_ID = os.environ.get(
    "SCHEDULE_SPREADSHEET_ID", "1d9XaG6qik3PYSWuu5YvifTm5vGSTheI4X5CS3E2wxNg"
)

if not SA_EMAIL or not GOOGLE_CLIENT_ID:
    logger.error("Missing critical environment variables: SERVICE_ACCOUNT_EMAIL or GOOGLE_CLIENT_ID")
    # In a production environment, you might want to raise an exception here
    # raise EnvironmentError("Application misconfigured: Missing required env vars.")

# Global Auth Context: Initialize once to avoid overhead on every request.
try:
    DEFAULT_CREDS, PROJECT_ID = google.auth.default()
    AUTH_HTTP_REQUEST = transport_requests.Request()
    # Initialize the IAM service once. cache_discovery=False is faster in serverless/cloud environments.
    IAM_SERVICE = build('iamcredentials', 'v1', credentials=DEFAULT_CREDS, cache_discovery=False)
except Exception as e:
    logger.error(f"Failed to initialize global Google Auth context: {e}")
    DEFAULT_CREDS = None

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware to apply Content Security Policy and modern security headers."""
    async def dispatch(self, request, call_next):
        # Generate a unique nonce for every request
        nonce = secrets.token_urlsafe(16)
        request.state.nonce = nonce

        response = await call_next(request)
        # CSP prevents XSS by restricting where scripts and styles can be loaded from.
        # frame-ancestors: Allows the Add-on to be displayed inside Google Classroom.
        # script-src: 'unsafe-inline' is required for Plotly's initialization scripts.
        csp_directives = [
            "default-src 'self' https://classroom.google.com",
            f"script-src 'self' https://cdn.plot.ly https://accounts.google.com 'unsafe-eval' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "img-src 'self' data: https://*.googleusercontent.com",
            "frame-ancestors 'self' https://classroom.google.com https://accounts.google.com https://enopieastcobb.com https://classroom.enopieastcobb.com",
            "frame-src https://accounts.google.com",
            "object-src 'none'"
        ]
        response.headers["Content-Security-Policy"] = "; ".join(csp_directives)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Never let a browser or proxy serve a stale roster. A student can turn
        # work in at any moment, so every load must reach the app.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(SecurityHeadersMiddleware)

def nonce_processor(request: Request):
    """Context processor to make the CSP nonce available in all templates."""
    return {"nonce": getattr(request.state, "nonce", "")}

templates = Jinja2Templates(directory="templates", context_processors=[nonce_processor])

class _TTLCache:
    """
    Tiny in-process cache. Cloud Run may run several instances, each keeping
    its own copy -- fine here, since every entry is either derivable again or
    short-lived.
    """
    def __init__(self):
        self._data: Dict[Any, tuple] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            value, expires_at = hit
            if time.time() >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key, value, ttl_seconds: float):
        with self._lock:
            self._data[key] = (value, time.time() + ttl_seconds)


_CACHE = _TTLCache()

# NOTHING ABOUT AN ASSIGNMENT IS EVER CACHED. Coursework, topics and
# submissions are fetched live on every request: a student can turn work in at
# any moment and this screen must show that immediately. Only auth tokens and
# slow-moving roster metadata are cached, with short windows.

# Minted tokens live an hour; re-use for 50 minutes. Each mint costs a signJwt
# call plus a token exchange, and there are two per request (Sheets +
# Classroom) -- four sequential round trips before any real work begins.
_TOKEN_TTL = 50 * 60
# The schedule Sheet only changes when an admin reschedules someone, which is
# infrequent, so this window can be generous -- it makes flipping the dropdowns
# feel instant instead of re-reading the Sheet every time.
_SCHEDULE_TTL = 5 * 60
# Which Classroom classes exist -- changes when a student joins the centre.
# Note this is the class LIST only, never its contents.
_COURSES_TTL = 60


def _build_service(name: str, version: str, credentials):
    """
    Builds an API client without fetching the discovery document over the
    network. The Classroom discovery doc is around a megabyte, and fetching it
    on every request was a large share of page latency; static_discovery uses
    the copy shipped inside the client library instead.
    """
    try:
        return build(name, version, credentials=credentials,
                     cache_discovery=False, static_discovery=True)
    except Exception as e:
        logger.warning(f"static discovery unavailable for {name} {version} ({e}); fetching over network.")
        return build(name, version, credentials=credentials, cache_discovery=False)


def get_scoped_creds(scopes: List[str], subject: Optional[str] = None):
    """
    Mints an access token by having the IAM Credentials API sign a JWT --
    no Service Account JSON key on disk.

    With `subject`, this is Domain-Wide Delegation: the token acts AS that
    domain user (how the Classroom calls read a teacher's own courses).
    Without it, the token acts as the service account itself -- which is what
    the schedule Sheet needs, since the Sheet is shared directly with the
    service account.

    Either way the token carries exactly `scopes`. This matters: Cloud Run's
    ambient (ADC) credentials carry only `cloud-platform`, which the Sheets
    API rejects, so ADC cannot be used to read the Sheet directly.
    """
    if not DEFAULT_CREDS:
        raise RuntimeError("Google Default Credentials not initialized.")

    cache_key = ('token', subject or '', tuple(sorted(scopes)))
    cached = _CACHE.get(cache_key)
    if cached:
        return oauth2_credentials.Credentials(cached)

    # Refresh default creds if expired
    if not DEFAULT_CREDS.valid:
        DEFAULT_CREDS.refresh(AUTH_HTTP_REQUEST)

    iat = int(time.time())
    exp = iat + 3600
    payload = {
        "iss": SA_EMAIL,
        "aud": "https://oauth2.googleapis.com/token",
        "iat": iat,
        "exp": exp,
        "scope": " ".join(scopes)
    }
    if subject:
        payload["sub"] = subject

    # Request Google to sign this JWT payload
    name = f"projects/-/serviceAccounts/{SA_EMAIL}"
    response = IAM_SERVICE.projects().serviceAccounts().signJwt(
        name=name,
        body={"payload": json.dumps(payload)}
    ).execute()

    signed_jwt = response["signedJwt"]

    # Exchange the signed JWT for an access token
    resp = AUTH_HTTP_REQUEST.session.post(
        "https://oauth2.googleapis.com/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": signed_jwt}
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    _CACHE.put(cache_key, access_token, _TOKEN_TTL)
    return oauth2_credentials.Credentials(access_token)


def get_teacher_creds_remote_signing(teacher_email: str, scopes: List[str]):
    """Domain-Wide Delegation: act as `teacher_email`."""
    return get_scoped_creds(scopes, subject=teacher_email)


SHEETS_SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']


def get_schedule_service() -> ScheduleService:
    """Reads the weekly schedule Sheet as the service account it's shared with."""
    return ScheduleService(SCHEDULE_SPREADSHEET_ID, get_scoped_creds(SHEETS_SCOPES))


def get_day_schedule_cached(day: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Day roster plus its make-ups, from one Sheets read, cached briefly.
    Without this every dropdown change re-reads the Sheet, and each read is a
    token mint plus a Sheets round trip.
    """
    key = ('schedule', day)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    data = get_schedule_service().get_day_schedule(day)
    _CACHE.put(key, data, _SCHEDULE_TTL)
    return data


def resolve_session_date(day: str, today: Optional[date] = None) -> Optional[date]:
    """
    The actual calendar date of the selected weekday -- today if today is that
    day, otherwise its next occurrence.

    Make-ups are one-off and dated, while this screen is a recurring weekly
    view, so a date is needed to tell one Tuesday's make-ups from another's.
    """
    weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    if day not in weekdays:
        return None
    today = today or today_local()
    return today + timedelta(days=(weekdays.index(day) - today.weekday()) % 7)

class DataProcessor:
    """
    Turns Classroom API responses into the item shape the UI renders.
    Ported from enopi-fic-pwa/src/data/progress.js (parseStrand, parseLevel,
    todo, sessions, grid), widened where that file's regexes were written
    against mock titles rather than the real Classroom ones.
    """

    # FIC = "Fix-In-Class": an item returned to Classwork with a fix-by date.
    # The grader marks it in the assignment title, e.g. "DGP Gr5 week 1. fic Jul1".
    # The tag stays in the title forever so FIC history is preserved, so it only
    # counts as an ACTIVE fic while the work still sits in Classwork/Homework.
    FIC_RE = re.compile(r'\bfic\b', re.IGNORECASE)
    # Matched by real month name rather than any 3 letters, so noise words
    # survive: "... fic inc Jul18" must still yield Jul 18.
    FIC_DATE_RE = re.compile(
        r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*(\d{1,2})\b',
        re.IGNORECASE)

    # "To Be Graded" and "Graded" carry no subject in their name, so subject is
    # inferred from the assignment title instead (pull-classroom-data.mjs).
    ENGLISH_HINT = re.compile(
        r'dgp|reading|root words|roots|grammar|vocab|critical reading|'
        r'classic series|comprehension|selection', re.IGNORECASE)
    MATH_HINT = re.compile(
        r'logic|basic thinking|critical thinking|number sense|fraction|'
        r'algebra|ratio|multiplication|division|extra practice|'
        # A maths booklet reference is itself a maths signal: "7 - 13 HC",
        # "BTM 19-5". Needed because completed booklets sit in a Graded topic,
        # which names no subject -- without this they were read as English and
        # dropped from the booklet history entirely.
        r'\bbtm\b|\bctm\b|^\s*l?\d{1,2}\s*-\s*\d{1,2}\b', re.IGNORECASE)

    # Title -> curriculum strand. Order matters: the first match wins, so more
    # specific patterns come before the ones that would also match them.
    STRAND_MAP = [
        (re.compile(r'\broots?\b|root words', re.I), 'Roots', 'Roots'),
        (re.compile(r'\bdgp\b', re.I), 'DGP', 'DGP'),
        # Anything starting "prob" and ending "of the Day" -- "Proble of the
        # Day" is already in the live data, and one missing letter must not
        # move a Maths item into the English room.
        (re.compile(r'\bprob\w*\s+of\s+the\s+day', re.I), 'Problem of the Day', 'POD'),
        (re.compile(r'proof\s*reading|\bpr\b', re.I), 'Proof Reading', 'PR'),
        (re.compile(r'\bwriting\b|\bessay\b|\bwrite\b', re.I), 'Writing', 'Writing'),
        (re.compile(r'classic series|\bcs\b', re.I), 'Classic Series', 'CS'),
        (re.compile(r'comprehension|\bcomp\b|critical reading', re.I), 'Comprehension', 'Comp'),
        (re.compile(r'grammar', re.I), 'Grammar', 'GR'),
        (re.compile(r'reading selection|\bselection\b|\bsel\b', re.I), 'Selection', 'Sel'),
        (re.compile(r'vocab', re.I), 'Vocabulary', 'Vocab'),
        (re.compile(r'basic thinking|\bbt\b', re.I), 'Basic thinking', 'BT'),
        (re.compile(r'critical thinking|\bct\b', re.I), 'Critical thinking', 'CT'),
        (re.compile(r'\blogic\b', re.I), 'Logic', 'Logic'),
        (re.compile(r'number sense|\bns\b', re.I), 'Number sense', 'NS'),
        # Maths strand, per the centre's vocabulary.
        (re.compile(r'extra practice|\bep\b', re.I), 'Extra practice', 'EP'),
        (re.compile(r'fraction', re.I), 'Fractions', 'Fr'),
        (re.compile(r'algebra', re.I), 'Algebra', 'Alg'),
        (re.compile(r'ratio', re.I), 'Ratios', 'Ratio'),
        (re.compile(r'multiplication', re.I), 'Multiplication', 'Mult'),
        (re.compile(r'writing', re.I), 'Writing', 'Writing'),
        # "G-4 HC" / "R29 HC" = Hard Copy: a record that a physical book was
        # handed to the student, not a curriculum strand. Grouped under one
        # column so these don't each invent a junk column ("G", "R29", ...).
        (re.compile(r'\bhc\b|hard copy', re.I), 'Hard Copy', 'HC'),
    ]

    @staticmethod
    def parse_strand(title: str) -> Dict[str, str]:
        for pattern, label, code in DataProcessor.STRAND_MAP:
            if pattern.search(title or ''):
                return {"label": label, "code": code}
        # Unrecognized title: fall back to its first word so it still gets its
        # own grid column instead of being dropped.
        first = (title or '').strip().split(' ')[0] or '?'
        return {"label": first, "code": first}

    @staticmethod
    def parse_level(title: str) -> str:
        title = title or ''
        for pattern, fmt in (
            (r'\b([A-Z]\d?-\d+)\b', '{}'),
            (r'\bpart\s*(\d+)', 'pt {}'),
            (r'\bpt\s*(\d+)', 'pt {}'),
            (r'\bweek\s*(\d+)', 'wk {}'),
            (r'\bW(\d+)\b', 'W{}'),
            (r'\bset\s*(\d+)', 'set {}'),
            (r'\bU\s*(\d+)', 'U{}'),
        ):
            m = re.search(pattern, title, re.I if '[A-Z]' not in pattern else 0)
            if m:
                return fmt.format(m.group(1))
        return ''

    @staticmethod
    def _due(meta: Dict[str, Any]) -> Optional[date]:
        due = meta.get('dueDate')
        if not due or not all(k in due for k in ('year', 'month', 'day')):
            return None
        try:
            return date(due['year'], due['month'], due['day'])
        except ValueError:
            return None

    @staticmethod
    def _posted(meta: Dict[str, Any]) -> Optional[date]:
        raw = meta.get('creationTime') or ''
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00')).date()
        except ValueError:
            return None

    @staticmethod
    def _subject_and_topic(topic_name: str, title: str):
        """
        Subject (English/Math), workflow topic, and the C/H badge letter.

        Topics are named "English Classwork", "Maths Homework", etc. -- but
        "To Be Graded" and "Graded" name no subject, so those fall back to
        matching the title (pull-classroom-data.mjs mapSubjectTopic).
        """
        t = topic_name or ''
        if re.search(r'english', t, re.I):
            subject = 'English'
            topic = re.sub(r'english\s*', '', t, flags=re.I).strip() or 'Classwork'
        elif re.search(r'math', t, re.I):
            subject = 'Math'
            topic = re.sub(r'maths?\s*', '', t, flags=re.I).strip() or 'Classwork'
        else:
            subject = 'Math' if DataProcessor.MATH_HINT.search(title or '') else 'English'
            topic = t or 'Classwork'

        low = topic.lower()
        category = 'H' if 'homework' in low else ('C' if 'classwork' in low else None)
        return subject, topic, category

    @staticmethod
    def _status(topic_name: str, title: str) -> str:
        """
        Status comes from WHICH TOPIC the work sits in -- the grader moves work
        Classwork/Homework -> To Be Graded -> Graded, and that movement is the
        signal (pull-classroom-data.mjs statusFor). Order matters here: "To Be
        Graded" also contains the word "Graded".
        """
        t = topic_name or ''
        if re.search(r'to be graded', t, re.I):
            return 'submitted'
        if re.search(r'graded', t, re.I):
            return 'done'
        return 'fic' if DataProcessor.FIC_RE.search(title or '') else 'notdone'

    @staticmethod
    def _fix_by(title: str) -> str:
        """
        The fix-by date carried in a FIC title: "... . fic Jul1" -> "Jul 1".

        Real titles carry filler and repeats, so this looks only at the part
        after "fic" and reports the LAST date found -- an item returned several
        times reads "fic May10,May 30,31,Jun7", where the latest date is the
        one that's actually still owed.
        """
        title = title or ''
        m = DataProcessor.FIC_RE.search(title)
        if not m:
            return ''
        matches = DataProcessor.FIC_DATE_RE.findall(title[m.end():])
        if not matches:
            return ''
        month, day = matches[-1]
        return f"{month.title()} {int(day)}"

    @staticmethod
    def build_item(
        meta: Dict[str, Any],
        topic_by_id: Dict[str, str],
        sub: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        title = meta.get('title') or ''
        topic_name = topic_by_id.get(meta.get('topicId')) or ''
        subject, topic, category = DataProcessor._subject_and_topic(topic_name, title)
        # Whether the topic NAMED the subject, or it was guessed from the title.
        # "Graded Assignments" holds both rooms' work and names neither, so the
        # subject there is inference and defaults to English -- which quietly
        # handed maths booklets at levels 6, 7 and 8 to the English checker.
        subject_stated = bool(re.search(r'english|math', topic_name or '', re.I))
        strand = DataProcessor.parse_strand(title)
        # Problem of the Day is Maths classwork wherever it happens to be
        # filed, so the strand overrides whatever the topic name implied.
        if strand["code"] == 'POD':
            subject = 'Math'
            category = 'C'
        status = DataProcessor._status(topic_name, title)

        due = DataProcessor._due(meta)
        posted = DataProcessor._posted(meta)
        done = status in ('done', 'submitted')
        # "With the grader", i.e. nothing for the student to do right now.
        #
        # Only work that is TURNED_IN and ungraded qualifies. A FIC reaches the
        # student one of two ways -- RETURNED to them, or GRADED and sent back
        # to rework -- and while it sits in Classwork or Homework it is still
        # owed either way. So a grade does NOT mean finished here: it means the
        # grader has looked at it, and if it's still in Classwork/Homework the
        # student has to work it again.
        _state = (sub or {}).get('state')
        _graded = (sub or {}).get('assignedGrade') is not None
        # TURNED_IN alone decides it, grade or no grade. A resubmission comes
        # back as TURNED_IN while still carrying the PREVIOUS round's grade --
        # Classroom shows that as "Resubmitted" with the old mark -- so the
        # work is with the grader awaiting re-grading, not with the student.
        # Treating a grade as "handed back" kept those on the list.
        turned_in = _state == 'TURNED_IN'

        _max = meta.get('maxPoints')
        score_percent = None
        if _graded and _max:
            try:
                score_percent = round(float(sub['assignedGrade']) / float(_max) * 100)
            except (TypeError, ValueError, ZeroDivisionError):
                score_percent = None

        below_passing = (
            score_percent is not None
            and score_percent < DataProcessor.PASSING_PERCENT
        )
        # A graded item under the pass mark, still sitting in Classwork or
        # Homework and not with the grader, IS a fix-in-class -- marked, but
        # not mastered. Promoting it to the fic status rather than tracking a
        # parallel concept means the red badge, the top sort tier and the FIC
        # count all follow on their own.
        if status == 'notdone' and below_passing and not turned_in:
            status = 'fic'
            done = False

        materials = meta.get('materials') or []
        material = ''
        if materials:
            material = (materials[0].get('driveFile', {}).get('driveFile', {}).get('title') or '')

        return {
            "title": title,
            "subject": subject,
            "subject_stated": subject_stated,
            "topic": topic,
            "category": category,
            "status": status,
            # The .fic tag is kept even once cleared, so FIC history survives.
            "was_fic": bool(DataProcessor.FIC_RE.search(title)),
            "fix_by": DataProcessor._fix_by(title),
            # "inc" in a title means the work came back incomplete.
            "incomplete": bool(re.search(r'\binc\b', title, re.I)),
            # A test needs to catch a teacher's eye, so it's flagged for the UI.
            # Word-boundary, so "Testing"/"Contest" don't trip it.
            "is_test": bool(re.search(r'\btests?\b', title, re.I)),
            "strand_code": strand["code"],
            "strand_label": strand["label"],
            "level": DataProcessor.parse_level(title),
            "posted_key": posted.isoformat() if posted else '',
            "posted_label": posted.strftime('%b %-d') if posted else '',
            "month_label": posted.strftime('%b %Y') if posted else '',
            "due_label": due.strftime('%b %-d') if due else '',
            # Drives the colour of the C/H badge, as a traffic light:
            #   red    past due
            #   orange due TODAY -- set last class and owed in this one, the
            #          thing a teacher has to act on while the student is here
            #   green  due beyond today, so there is still time
            # Neutral when nothing is owed: already handed in, or no due date.
            "due_state": (
                '' if (done or turned_in or not due)
                else 'past_due' if due < today_local()
                else 'due_today' if due == today_local()
                else 'due_later'
            ),
            # Sortable form, so items can be ordered most-urgent-first within
            # a priority tier. Undated sorts last.
            "due_key": due.isoformat() if due else '9999-12-31',
            "overdue": bool(due and not done and due < today_local()),
            "material": material,
            # "Given" is the physical notebook, recorded in the assignment header.
            "given": re.sub(r'^given:\s*', '', meta.get('description') or '', flags=re.I),
            # Deep-link straight to the item in Classroom.
            "link": meta.get('alternateLink') or '',
            # Score lives on the SUBMISSION, not the assignment -- the only
            # place a graded item's marks can come from. Status still comes
            # from the topic; this is display detail only.
            "score": (sub or {}).get('assignedGrade'),
            "draft_score": (sub or {}).get('draftGrade'),
            "max_points": meta.get('maxPoints'),
            "score_percent": score_percent,
            # Graded but under the pass mark: marked, yet not mastered, so the
            # student still has to rework it.
            "below_passing": below_passing,
            "late": bool((sub or {}).get('late')),
            "submission_state": (sub or {}).get('state') or '',
            # Is the work currently with the grader? Status is driven by the
            # topic, but a student can turn work in before the grader moves it
            # out of Classwork -- in which case the topic still says "not done"
            # while the work is in fact submitted. This keeps it off the
            # teacher's action list without disturbing the topic-based status.
            # RETURNED is NOT this: that work is back with the student.
            "turned_in": turned_in,
        }

    # Strands kept off this screen entirely.
    EXCLUDED_STRAND_CODES: set = set()

    @staticmethod
    def mark_superseded_pods(items: List[Dict[str, Any]]) -> None:
        """
        Only ONE Problem of the Day belongs on screen: the next one still owed,
        i.e. the earliest with a due date of today or later.

        They're issued as a long running series ("Problem of the Day 28, 29,
        30..."), so without this a student's card fills with every past one and
        buries the work that actually matters. Marks the rest in place.
        """
        pods = [i for i in items if i["strand_code"] == 'POD']
        if not pods:
            return
        today_key = today_local().isoformat()
        upcoming = sorted(
            (p for p in pods if p["due_label"] and p["due_key"] >= today_key),
            key=lambda p: p["due_key"],
        )
        keep = upcoming[0] if upcoming else None
        for p in pods:
            p["pod_superseded"] = p is not keep

    # Reference material the grader posts for lookup, not to be worked:
    # "Proof Reading Guide", "DGP Gr2 Guide".
    REFERENCE_RE = re.compile(r'\bguides?\b', re.IGNORECASE)

    # Anything filed under an Announcement topic is a notice, not work.
    ANNOUNCEMENT_RE = re.compile(r'announcement', re.IGNORECASE)

    # "Graded Assignments" is a TOPIC the grader moves finished work into --
    # not the same thing as a submission carrying a grade. Work that has
    # reached it is done with, so it is out of scope for the session view
    # entirely: not on the badges, not in "assignments not shown", not in
    # "What Classroom returned". It still appears in the progress grid, which
    # is the full record.
    GRADED_TOPIC_RE = re.compile(r'graded', re.IGNORECASE)

    @staticmethod
    def in_session_scope(item: Dict[str, Any]) -> bool:
        """False for work parked in a Graded topic -- finished and out of scope."""
        topic = item.get("topic") or ''
        if DataProcessor.ANNOUNCEMENT_RE.search(topic):
            return True  # announcements are reported as withheld, not dropped
        return not DataProcessor.GRADED_TOPIC_RE.search(topic)

    # The centre's pass mark. A graded item below this hasn't been mastered,
    # so it still needs reworking with the student even though it carries a
    # grade -- the same signal a FIC gives, arrived at by score.
    PASSING_PERCENT = 80

    @staticmethod
    def hidden_reason(item: Dict[str, Any]) -> str:
        """Why an item is being withheld, phrased for the person who wrote it."""
        if item["strand_code"] in DataProcessor.EXCLUDED_STRAND_CODES:
            return "excluded strand"
        if item.get("pod_superseded"):
            return "superseded Problem of the Day (only the next one is shown)"
        if DataProcessor.ANNOUNCEMENT_RE.search(item.get("topic") or ''):
            return "posted under Announcements, not an assignment"
        if DataProcessor.REFERENCE_RE.search(item["title"] or ''):
            return 'reference material ("guide" in the title)'
        return ""

    @staticmethod
    def is_trackable(item: Dict[str, Any]) -> bool:
        """
        Whether an item belongs on this screen.

        Published work is shown unless it isn't work at all: filed under an
        Announcement topic (a notice), reference material with "guide" in the
        title, or a Problem of the Day that a later one has superseded (see
        mark_superseded_pods -- only the next one still owed is shown).

        A missing due date is NOT grounds for hiding anything: a teacher may
        assign real work and leave the date blank, and hiding it would mean a
        student's work never reaches the teacher's list. Drafts never arrive
        here at all, since coursework is read PUBLISHED-only.
        """
        if item["strand_code"] in DataProcessor.EXCLUDED_STRAND_CODES:
            return False
        if item.get("pod_superseded"):
            return False
        if DataProcessor.ANNOUNCEMENT_RE.search(item.get("topic") or ''):
            return False
        if DataProcessor.REFERENCE_RE.search(item["title"] or ''):
            return False
        return True

    @staticmethod
    def counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
        return {
            "done": sum(1 for i in items if i["status"] in ('done', 'submitted')),
            "fic": sum(1 for i in items if i["status"] == 'fic'),
            "notdone": sum(1 for i in items if i["status"] == 'notdone'),
        }

    @staticmethod
    def priority(item: Dict[str, Any]) -> int:
        """
        Urgency tier, in the order teachers work a session. Deliberately mirrors
        the C/H badge colours so the ordering and the colours say the same thing.

          0  FIC       - turned in, graded, too many mistakes: must be reworked
                         with the student in class. The alert condition.
          1  INC       - came back incomplete
          2  Past due  - owed and the due date has gone by      (red)
          3  Due today - set last class, owed in this one       (orange)
          4  Due later - still has time                          (green)
        """
        if item["status"] == 'fic':
            return 0
        if item["incomplete"]:
            return 1
        if item["overdue"]:
            return 2
        if item["due_state"] == 'due_today':
            return 3
        return 4

    @staticmethod
    def _section_rank(item: Dict[str, Any]) -> int:
        """Classwork before Homework -- the order a session is actually worked."""
        return {'C': 0, 'H': 1}.get(item.get("category"), 2)

    @staticmethod
    def not_outstanding_reason(item: Dict[str, Any]) -> str:
        """
        Why a real assignment isn't on the action list. Kept separate from
        hidden_reason, which covers things that were never work at all.
        """
        if DataProcessor._is_outstanding(item):
            return ""
        if item["status"] == 'submitted':
            return "in To Be Graded — with the grader"
        if item["status"] == 'done':
            return "in Graded — finished"
        if item.get("turned_in"):
            # Deliberately doesn't say "not yet graded": a resubmission is
            # TURNED_IN while still carrying the previous round's mark.
            return "turned in — with the grader"
        return "not currently outstanding"

    @staticmethod
    def _is_outstanding(item: Dict[str, Any]) -> bool:
        """
        Whether the student still owes this, i.e. whether it belongs on the
        teacher's session list.

        Work sitting in To Be Graded or Graded is finished. Anything else the
        student has handed back is with the grader and needs nothing in class
        right now -- and that includes a FIC: once it has been turned in there
        is nothing to rework this session, even though the .fic tag stays in
        the title forever.
        """
        if item["status"] not in ('notdone', 'fic'):
            return False
        return not item.get("turned_in")

    @staticmethod
    def todo(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Outstanding work only: all Classwork first, then all Homework, and
        within each section FIC -> INC -> past due -> due later, then by due
        date so the most urgent leads its group.

        Turned-in work drops off here but stays in the progress grid, which is
        the full record.

        This deliberately replaces progress.js todo(), which had only two
        tiers, ignored the section, and put not-started work ahead of FICs.
        """
        return sorted(
            (i for i in items if DataProcessor._is_outstanding(i)),
            key=lambda i: (
                DataProcessor._section_rank(i),
                DataProcessor.priority(i),
                i["due_key"],
            ),
        )

    @staticmethod
    def grid(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Rows = session dates (newest first), columns = strands (progress.js grid())."""
        strands: List[Dict[str, str]] = []
        seen = set()
        for i in items:
            if i["strand_code"] not in seen:
                seen.add(i["strand_code"])
                strands.append({"code": i["strand_code"], "label": i["strand_label"]})

        by_date: Dict[str, Dict[str, Any]] = {}
        for i in items:
            key = i["posted_key"]
            slot = by_date.setdefault(
                key, {"cells": {}, "label": i["posted_label"], "month": i["month_label"]})
            # A list, not a single item: two assignments can share the same
            # date AND strand (e.g. Classwork and Homework the same session),
            # and assigning would silently drop one from the history.
            slot["cells"].setdefault(i["strand_code"], []).append(i)

        rows = []
        for key in sorted(by_date, reverse=True):
            slot = by_date[key]
            rows.append({
                "date": slot["label"],
                "month": slot["month"],
                "cells": [slot["cells"].get(s["code"], []) for s in strands],
            })
        return {"strands": strands, "rows": rows}

class ClassroomService:
    """Handles all interactions with the Google Classroom API."""
    def __init__(self, teacher_email: str):
        self.scopes = [
            'https://www.googleapis.com/auth/classroom.courses.readonly',
            'https://www.googleapis.com/auth/classroom.rosters.readonly',
            'https://www.googleapis.com/auth/classroom.student-submissions.me.readonly',
            'https://www.googleapis.com/auth/classroom.student-submissions.students.readonly',
            'https://www.googleapis.com/auth/classroom.topics.readonly',
        ]
        
        if not teacher_email:
            logger.error("ClassroomService initialized with empty teacher_email")
            raise ValueError("Teacher email is required for ClassroomService impersonation.")
            
        logger.info(f"Impersonating Google Classroom user via Remote Signing: {teacher_email}")
        self.creds = get_teacher_creds_remote_signing(teacher_email, self.scopes)
        self._local = threading.local()
        try:
            # Classroom API discovery is cached internally by the library, but we disable file cache for Cloud Shell
            self.service = _build_service('classroom', 'v1', self.creds)
        except Exception as e:
            logger.error(f"Failed to build Classroom service (likely identity/Gaia issue): {e}")
            raise

    def _http(self):
        """
        A separate authorized Http per thread.

        httplib2 is not thread-safe, and the service object built by build()
        carries a single shared Http. Loading students concurrently on that
        shared object corrupts responses -- which is exactly how a session
        loses an unpredictable subset of its students. Passing a per-thread
        http to execute() is the documented way to use one service from
        several threads.
        """
        if not hasattr(self._local, 'http'):
            self._local.http = google_auth_httplib2.AuthorizedHttp(
                self.creds, http=httplib2.Http(timeout=60))
        return self._local.http

    def _execute(self, request):
        """
        Runs a request on this thread's http, retrying transient failures.

        num_retries applies the client's exponential backoff to 429 and 5xx --
        without it a single rate-limit blip silently drops one student.
        """
        return request.execute(http=self._http(), num_retries=4)

    def _all_pages(self, collection, key: str, **kwargs) -> List[Dict[str, Any]]:
        """
        Drains every page of a Classroom list call.

        The API caps page size on its own when none is given, so a single
        .execute() silently returns a PARTIAL list -- which would drop
        students off the end of the course list and truncate a student's
        assignment history.
        """
        out: List[Dict[str, Any]] = []
        request = collection.list(pageSize=200, **kwargs)
        while request is not None:
            response = self._execute(request)
            out.extend(response.get(key, []))
            request = collection.list_next(request, response)
        return out

    def get_course_details(self, course_id: str):
        return self._execute(self.service.courses().get(id=course_id))

    def list_teacher_courses(self) -> List[Dict[str, Any]]:
        """Lists all active courses where the user is a teacher."""
        return self._all_pages(
            self.service.courses(), 'courses',
            teacherId='me', courseStates=['ACTIVE'],
        )

    def get_student_roster(self, course_id: str) -> Dict[str, str]:
        students_list = self._all_pages(
            self.service.courses().students(), 'students', courseId=course_id,
        )
        return {
            s.get('userId'): s.get('profile', {}).get('name', {}).get('fullName', 'Unknown Student')
            for s in students_list
        }

    def get_coursework(self, course_id: str) -> List[Dict[str, Any]]:
        # PUBLISHED only -- a draft hasn't been assigned to the student yet.
        return self._all_pages(
            self.service.courses().courseWork(), 'courseWork',
            courseId=course_id, courseWorkStates=['PUBLISHED'],
        )

    def get_topics(self, course_id: str) -> Dict[str, str]:
        topics = self._all_pages(
            self.service.courses().topics(), 'topic', courseId=course_id,
        )
        return {t['topicId']: t.get('name', '') for t in topics}

    def get_submissions_by_coursework(self, course_id: str) -> Dict[str, Dict[str, Any]]:
        """
        Every submission in the course, keyed by courseWorkId.

        courseWorkId='-' is the API's wildcard for "all coursework", so this
        is one paged call for the whole course rather than one per assignment.
        Each course is a single student's container, so the first submission
        per assignment is that student's.
        """
        subs = self._all_pages(
            self.service.courses().courseWork().studentSubmissions(),
            'studentSubmissions',
            courseId=course_id, courseWorkId='-',
        )
        by_cw: Dict[str, Dict[str, Any]] = {}
        for s in subs:
            by_cw.setdefault(s.get('courseWorkId'), s)
        return by_cw

    def load_student(self, course_id: str) -> "tuple[List[Dict[str, Any]], str]":
        """
        All assignment items for one student's Classroom course.

        Three calls per student -- coursework, topics and all submissions --
        issued CONCURRENTLY. None depends on another's result, and run in
        sequence they tripled the wall time of every student: measured on a
        4-student session, this phase was 7.95s of a 10.6s page.

        Status comes from the topic the work sits in, but the SCORE only
        exists on the submission, so graded items need it to show their marks.

        Never cached. A student can turn work in at any moment and the screen
        has to reflect it on the next load.
        """
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_work = pool.submit(self.get_coursework, course_id)
            f_topics = pool.submit(self.get_topics, course_id)
            f_subs = pool.submit(self.get_submissions_by_coursework, course_id)

            assignments = f_work.result()
            try:
                topic_by_id = f_topics.result()
            except Exception as e:
                # Without topics every item loses its status and subject, so
                # this one is fatal for the student -- let it surface.
                logger.error(f"Course {course_id}: could not load topics ({e}).")
                raise
            subs_error = ''
            try:
                subs_by_cw = f_subs.result()
            except Exception as e:
                # Losing submissions must not lose the items -- but it silently
                # costs every score AND the turned-in flag, so it is reported
                # back rather than left as a log line nobody reads.
                logger.warning(f"Course {course_id}: could not load submissions ({e}).")
                subs_by_cw = {}
                subs_error = f"{type(e).__name__}: {e}"

        if not assignments:
            return [], subs_error
        # Returns EVERY item, unfiltered. The caller decides what to show, so
        # that anything held back can be reported rather than disappearing --
        # "my new assignment isn't showing" has to be answerable from the page.
        items = [
            DataProcessor.build_item(a, topic_by_id, subs_by_cw.get(a.get('id')))
            for a in assignments
        ]
        logger.info(
            f"Course {course_id}: {len(items)} items, {len(subs_by_cw)} submissions matched, "
            f"{sum(1 for i in items if i['turned_in'])} turned in."
        )
        return items, subs_error


def _normalize_name(name: str) -> str:
    name = re.sub(r'\(.*?\)', '', name)  # strip "(online)" etc.
    return re.sub(r'\s+', ' ', name).strip().lower()


def split_course_name(course_name: str) -> Dict[str, str]:
    """
    "Aarav Mehta Gr3" -> name "Aarav Mehta", grade "Grade 3".

    Tolerates every real-world variant seen: "Nathan W Gr 1[2026-2027]",
    "Sanora S Gr 5 [2026-2027]", "Anush Patel Gr3 [...]", and compound
    grades like "Gr7/8".
    """
    m = re.match(r'^(.*?)\s+Gr\s*([\d/]+)', course_name or '', re.IGNORECASE)
    if m:
        return {"name": m.group(1).strip(), "grade": f"Grade {m.group(2)}"}
    return {"name": (course_name or '').strip(), "grade": ""}


def find_course_for_student(courses: List[Dict[str, Any]], student_name: str) -> Optional[Dict[str, Any]]:
    """
    Matches a schedule-sheet student name (often first-name-only, e.g. "Krish")
    against a Classroom course name (e.g. "Krish Patel") -- each course is an
    individual student's own tracking container, so course name == student name.
    """
    target = _normalize_name(student_name)
    for course in courses:
        cname = _normalize_name(course.get('name', ''))
        if cname == target or cname.startswith(target + ' ') or target.startswith(cname + ' '):
            return course
    return None

@app.get("/")
def read_root():
    return {"status": "Secure Dashboard Backend is Active"}

@app.get("/launch")
async def launch_entry(request: Request, courseId: str = None, addOnToken: str = None):
    """
    The entry point from Google Classroom. 
    Teachers link to: https://your-app.com/launch?courseId=ID&addOnToken=TOKEN
    """
    return templates.TemplateResponse(request, "login.html", {
        "courseId": courseId,
        "google_client_id": GOOGLE_CLIENT_ID,
        "addOnToken": addOnToken
    })

def _verify_and_get_email(id_token: str) -> str:
    if not id_token:
        raise ValueError("Authentication required. Please sign in via the dashboard.")
    id_info = google_id_token.verify_oauth2_token(id_token, AUTH_HTTP_REQUEST, GOOGLE_CLIENT_ID)
    teacher_email = id_info.get("email", "").strip().lower()
    if not teacher_email:
        raise ValueError("Authentication token is missing user identity (email).")
    if not DEFAULT_CREDS:
        logger.critical("Server-side Google Credentials (ADC) are missing. Check environment setup.")
        raise RuntimeError("Backend authentication service is unavailable.")
    return teacher_email


ROOM_LABEL = {"English": "English Room", "Math": "Math Room", "Weenopi": "Weenopi"}

# Walk-ins aren't on any teacher's scheduled list, so they get their own group.
WALKIN_GROUP = "Walk-ins"
# Make-ups come from the schedule Sheet's own dated make-up table.
MAKEUP_GROUP = "Make-ups"
# Extra help sessions live in the same dated table as make-ups, told apart by
# "Help" in its Notes column -- they are for children carrying more FICs than
# their own hour can clear. Same table, different reason to be in the room, so
# the register names them separately: an admin counting heads wants to know
# which of these children is catching up a missed session and which is here for
# extra support.
HELP_GROUP = "Help session"
HELP_NOTE_RE = re.compile(r'\bhelp\b', re.IGNORECASE)
# Said plainly on the register, because it tells the admin where to walk. The
# exception -- a class teacher with two or fewer students that hour keeps the
# child with them instead -- is left to the person doing the walking rather
# than guessed at from a roster count here.
HELP_ROOM_LABEL = "Help session · separate room"


def simulated_now(raw: str) -> Optional[datetime]:
    """
    A pretend clock for testing, as "HH:MM" on the centre's TODAY.

    Only honoured when TIME_OVERRIDE_ENABLED, and it moves the clock only --
    the session-date check still applies, so a simulated time still has to be
    paired with a day that actually runs today.
    """
    if not (TIME_OVERRIDE_ENABLED and raw):
        return None
    try:
        hh, mm = (int(p) for p in raw.strip().split(":")[:2])
    except (ValueError, TypeError):
        return None
    today = now_local()
    return today.replace(hour=hh, minute=mm, second=0, microsecond=0)


def in_handover_window(session_time: str, session_date: Optional[date],
                       now: Optional[datetime] = None) -> bool:
    """
    Whether this session's handover alert should be showing.

    It opens ten minutes before the session ends -- late enough that a teacher
    has had the lesson to hand books over, early enough to put right before the
    child leaves -- and then stays up for the REST OF THE DAY. A booklet still
    missing at 6pm is just as missing at 8pm, and closing the alert when the
    hour ends would hide the problem from whoever could still fix it.

    Checking the date as well as the clock matters: the schedule repeats
    weekly, so without it a Tuesday 5pm session would raise its alert every
    weekday evening.
    """
    now = now or (now_local())
    if session_date and now.date() != session_date:
        return False
    try:
        start = datetime.strptime(session_time.strip(), "%I:%M %p")
    except (ValueError, AttributeError):
        return False
    if now.hour > start.hour:
        return True
    return now.hour == start.hour and now.minute >= HANDOVER_ALERT_FROM_MINUTE


def _time_key(t: str) -> tuple:
    """Sort '9:00 AM' before '12:00 PM' before '3:00 PM'."""
    try:
        return (0, datetime.strptime(t.strip(), "%I:%M %p").time())
    except ValueError:
        return (1, t)


def _roster_for(on: date) -> List[Dict[str, str]]:
    """
    Everyone expected on a date: the weekly roster, plus that date's make-ups
    and help sessions.

    Shared by the register and the absence report so both agree on who was due.
    Computed the same way in both places, they could drift apart and have the
    report contradict the screen the marks were made on.
    """
    day = on.strftime('%A')
    if day not in ScheduleService.DAYS:
        return []
    schedule = get_day_schedule_cached(day)
    out: List[Dict[str, str]] = []
    for e in schedule["entries"]:
        name = (e.get("student_name") or "").strip()
        if name:
            out.append({"name": name, "time": e.get("time") or '',
                        "room": e.get("subject") or '',
                        "group": e.get("teacher") or '', "note": ''})
    # Make-ups are dated, so they belong to THIS date only.
    for mu in schedule.get("makeups") or []:
        if mu.get("date") != on:
            continue
        name = (mu.get("student_name") or "").strip()
        if not name:
            continue
        is_help = bool(HELP_NOTE_RE.search(mu.get("notes") or ''))
        # A make-up child sits in the ordinary class, so they belong to that
        # room. A help child usually does not -- they are put in a separate
        # room with a grader overseeing them -- so filing them under Maths
        # would send the admin to a room the child is not in. Their subject
        # follows as a note, since whoever supervises needs to know it.
        out.append({"name": name, "time": mu.get("time") or '',
                    "room": HELP_GROUP if is_help else (mu.get("subject") or ''),
                    "group": '' if is_help else MAKEUP_GROUP,
                    "note": (mu.get("subject") or '') if is_help else ''})
    return out


def _attendance_context(request: Request, form, teacher_email: str):
    """
    The day's roster, grouped for someone walking the rooms with a tablet.

    Deliberately does NOT touch Classroom. The attendance page needs names and
    nothing else, so it reads only the schedule Sheet -- which is cached -- and
    loads in a fraction of the time the dashboard takes. Waiting three seconds
    at each doorway would make the tool unusable for its actual purpose.
    """
    raw = (form.get("on") or "").strip()
    try:
        on = date.fromisoformat(raw) if raw else today_local()
    except ValueError:
        on = today_local()
    day = on.strftime('%A')

    ctx = {
        "on": on.isoformat(),
        "on_label": on.strftime('%a %b %-d'),
        "day": day,
        "is_session_day": day in ScheduleService.DAYS,
        "hours": [],
        "present": set(),
        "marked": 0,
        "expected": 0,
        "teacher_email": teacher_email,
        "idToken": form.get("credential") or form.get("idToken") or "",
        "error": "",
    }
    if not ctx["is_session_day"]:
        return ctx

    roster = _roster_for(on)

    try:
        # Full records, not just names: a walk-in exists only as an attendance
        # record, so its hour and room must be read back to place the tile.
        records = attendance.records_on(on.isoformat())
        ctx["present"] = {attendance.slug(r.get('student') or '') for r in records}
    except Exception as e:
        # The roster is still worth showing: an admin can mark from a page that
        # could not read back what was already marked, and the marks will land.
        records = []
        ctx["error"] = f"Could not read what has already been marked ({type(e).__name__})."

    # Grouped hour -> room -> teacher, which is the order the rooms are walked
    # rather than the order the Sheet happens to list them in.
    by_hour: Dict[str, Dict[str, Dict[str, List[Any]]]] = {}
    for e in roster:
        (by_hour.setdefault(e["time"], {})
               .setdefault(e["room"], {})
               .setdefault(e["group"], [])).append((e["name"], e["note"]))

    # Anyone marked present who is not on the schedule turned up unannounced.
    # They need no storage of their own -- the attendance record IS the walk-in,
    # carrying the hour and room it was added in -- so they reappear on reload
    # with no second place for the data to drift out of step.
    on_roster = {attendance.slug(e["name"]) for e in roster}
    for r in records:
        name = (r.get('student') or '').strip()
        if not name or attendance.slug(name) in on_roster:
            continue
        (by_hour.setdefault(r.get('time') or '', {})
               .setdefault(r.get('subject') or '', {})
               .setdefault(WALKIN_GROUP, [])).append((name, ''))

    for hour in sorted(by_hour, key=_time_key):
        rooms = []
        # Subject rooms first, the help room last -- it is the odd one out on
        # the walk, and putting it after Maths and English matches the order
        # the rooms actually get visited in.
        for room in sorted(by_hour[hour], key=lambda r: (r == HELP_GROUP, r)):
            groups = []
            # The room's own teacher first, then the children who are here for
            # another reason. Plain alphabetical order put "Help session" above
            # the teacher whose class it is, which reads as though the room
            # belonged to it.
            def _group_order(t):
                return ({HELP_GROUP: 1, MAKEUP_GROUP: 2, WALKIN_GROUP: 3}.get(t, 0), t)
            for teacher, names in sorted(by_hour[hour][room].items(),
                                         key=lambda kv: _group_order(kv[0])):
                students = [{"name": n, "slug": attendance.slug(n),
                             "note": note,
                             "present": attendance.slug(n) in ctx["present"]}
                            for n, note in sorted(set(names))]
                groups.append({"teacher": teacher, "students": students})
                ctx["expected"] += len(students)
                ctx["marked"] += sum(1 for s in students if s["present"])
            # A make-up row with no subject recorded would otherwise render an
            # unlabelled block, which reads as a glitch rather than as missing
            # information in the Sheet.
            rooms.append({"room": room,
                          "label": (HELP_ROOM_LABEL if room == HELP_GROUP
                                    else ROOM_LABEL.get(room) or room
                                    or "Room not recorded"),
                          "groups": groups})
        by_room_total = sum(len(g["students"]) for r in rooms for g in r["groups"])
        by_room_marked = sum(1 for r in rooms for g in r["groups"]
                             for s in g["students"] if s["present"])
        # Who is still unaccounted for in this hour -- the "who's missing right
        # now" question, which is the one an admin asks mid-session.
        missing = [s["name"] for r in rooms for g in r["groups"]
                   for s in g["students"] if not s["present"]]
        ctx["hours"].append({"time": hour, "rooms": rooms,
                             "total": by_room_total, "marked": by_room_marked,
                             "missing": missing,
                             "is_now": _is_current_hour(hour, on)})
    return ctx


def _hour_finished(session_time: str, on: date, now: Optional[datetime] = None) -> bool:
    """
    Whether this session hour is over, so absence can be judged.

    An hour that has not finished tells you nothing about who is away: at four
    o'clock the five and six o'clock children simply have not arrived, and a
    child can still walk in at ten past. Counting them absent would report most
    of the centre as missing every afternoon.

    A past date is entirely finished; a future one entirely unfinished.
    """
    today = today_local()
    if on < today:
        return True
    if on > today:
        return False
    try:
        start = datetime.strptime(session_time.strip(), "%I:%M %p")
    except (ValueError, AttributeError):
        return False
    return (now or now_local()).hour > start.hour


def _is_current_hour(session_time: str, on: date) -> bool:
    """Whether this hour is the one running now, so it can lead the page."""
    if on != today_local():
        return False
    try:
        start = datetime.strptime(session_time.strip(), "%I:%M %p")
    except (ValueError, AttributeError):
        return False
    return now_local().hour == start.hour


@app.get("/attendance")
async def attendance_entry(request: Request):
    """Reached directly, so it starts at the sign-in prompt."""
    return templates.TemplateResponse(request, "login.html", {
        "google_client_id": GOOGLE_CLIENT_ID,
        "login_action": "/attendance",
    })


@app.post("/attendance")
async def attendance_page(request: Request):
    form = await request.form()
    try:
        teacher_email = _verify_and_get_email(
            form.get("credential") or form.get("idToken"))
    except Exception as auth_err:
        logger.info("Sign-in lapsed on attendance: %s", auth_err)
        return templates.TemplateResponse(request, "login.html", {
            "google_client_id": GOOGLE_CLIENT_ID,
            "login_action": "/attendance",
            "signed_out": True,
        })
    ctx = _attendance_context(request, form, teacher_email)
    return templates.TemplateResponse(request, "attendance.html", ctx)


DIGEST_SENDER = os.environ.get("DIGEST_SENDER", "admin@enopieastcobb.com")
DIGEST_TO = os.environ.get("DIGEST_TO", "info@enopieastcobb.com")
# No DIGEST_TOKEN and no /digest route: the digest runs as a Cloud Run Job
# (digest_job.py), so it has no network surface to authenticate. An endpoint
# would have needed ingress opened for Cloud Scheduler to reach it, adding a
# second front door to a design whose point is that IAP is the only one.


def _digest_sections(on: date, teacher_email: str):
    """
    Every outstanding booklet finding for a date, across all rooms and hours.

    More complete than any single banner view: the dashboard shows one room and
    hour at a time, so a problem can hide behind a dropdown nobody opened. This
    walks all of them.

    Each student's Classroom data is loaded ONCE for the whole day, not once per
    session. A child in 4pm English and 5pm Maths would otherwise be fetched
    twice, and fetching is nearly all of the time this takes.
    """
    roster = _roster_for(on)
    if not roster:
        return [], 0

    classroom_svc = ClassroomService(teacher_email)
    courses_key = ('courses', teacher_email)
    courses = _CACHE.get(courses_key)
    if courses is None:
        courses = classroom_svc.list_teacher_courses()
        _CACHE.put(courses_key, courses, _COURSES_TTL)

    # name -> (items, grade), fetched once each.
    loaded: Dict[str, Any] = {}

    def _load(name: str):
        if name in loaded:
            return loaded[name]
        course = find_course_for_student(courses, name)
        if not course:
            loaded[name] = None
            return None
        try:
            items, _ = classroom_svc.load_student(course["id"])
        except Exception as e:
            logger.error("Digest could not load %r: %s", name, e)
            loaded[name] = None
            return None
        grade = ((course.get("section") or "").strip()
                 or split_course_name(course.get("name", "")).get("grade", ""))
        DataProcessor.mark_superseded_pods(items)
        loaded[name] = (items, grade)
        return loaded[name]

    by_slot: Dict[Any, List[Dict[str, str]]] = {}
    for e in roster:
        by_slot.setdefault((e["time"], e["room"]), []).append(e)

    sections, checked = [], 0
    for (hour, room) in sorted(by_slot, key=lambda k: (_time_key(k[0]), k[1])):
        checker = {"Math": review_student, "English": review_english}.get(room)
        if checker is None:
            # Help sessions and rooms with no booklet curriculum of their own.
            continue
        checked += 1
        items_out = []
        for e in sorted(by_slot[(hour, room)], key=lambda x: x["name"]):
            got = _load(e["name"])
            if not got:
                continue
            student_items, grade = got
            try:
                found = checker(student_items, on, grade)
            except Exception as ex:
                logger.error("Digest check failed for %r: %s", e["name"], ex,
                             exc_info=True)
                continue
            severe = set(found.get("severe") or [])
            for booklet in found.get("missing") or []:
                items_out.append({"student": e["name"],
                                  "detail": f"did not receive {booklet}",
                                  "severe": False})
            for note in found.get("notes") or []:
                items_out.append({"student": e["name"], "detail": note,
                                  "severe": note in severe})
        if items_out:
            sections.append({"time": hour,
                             "room": ROOM_LABEL.get(room, room),
                             "items": items_out})
    return sections, checked


@app.post("/attendance/walkin")
async def attendance_walkin(request: Request):
    """
    Add a child who turned up unannounced, in the room the admin is standing in.

    Marked present in the same act as being added: there is no reason to record
    a walk-in who is not here. A full re-render follows rather than a partial
    update -- this happens rarely, and the reload is what proves the record
    landed rather than a tile that merely looks added.
    """
    form = await request.form()
    try:
        teacher_email = _verify_and_get_email(
            form.get("credential") or form.get("idToken"))
    except Exception as auth_err:
        logger.info("Sign-in lapsed adding a walk-in: %s", auth_err)
        return templates.TemplateResponse(request, "login.html", {
            "google_client_id": GOOGLE_CLIENT_ID,
            "login_action": "/attendance", "signed_out": True,
        })

    name = (form.get("student") or "").strip()
    failed = ""
    if name:
        raw = (form.get("on") or "").strip()
        try:
            on = date.fromisoformat(raw) if raw else today_local()
        except ValueError:
            on = today_local()
        try:
            attendance.mark_present(
                on.isoformat(), name, session_day=on.strftime('%A'),
                subject=form.get("subject") or '', time=form.get("time") or '',
                teacher=WALKIN_GROUP, marked_by=teacher_email)
        except Exception as e:
            logger.error("Walk-in write failed for %r: %s", name, e, exc_info=True)
            failed = f"Could not add {name} ({type(e).__name__})."

    ctx = _attendance_context(request, form, teacher_email)
    if failed:
        ctx["error"] = failed
    return templates.TemplateResponse(request, "attendance.html", ctx)


@app.post("/attendance/report")
async def attendance_report(request: Request):
    """A child's record over a period, and the totals for everyone in it."""
    form = await request.form()
    try:
        teacher_email = _verify_and_get_email(
            form.get("credential") or form.get("idToken"))
    except Exception as auth_err:
        logger.info("Sign-in lapsed on the attendance report: %s", auth_err)
        return templates.TemplateResponse(request, "login.html", {
            "google_client_id": GOOGLE_CLIENT_ID,
            "login_action": "/attendance", "signed_out": True,
        })

    today = today_local()
    def _d(field, default):
        try:
            return date.fromisoformat((form.get(field) or '').strip())
        except ValueError:
            return default
    # A month back by default: long enough to see a pattern, short enough that
    # nobody waits for it.
    frm = _d("from", today - timedelta(days=30))
    to = _d("to", today)
    if frm > to:
        frm, to = to, frm

    ctx = {"from": frm.isoformat(), "to": to.isoformat(),
           "from_label": frm.strftime('%a %b %-d'),
           "to_label": to.strftime('%a %b %-d'),
           "idToken": form.get("credential") or form.get("idToken") or "",
           "rows": [], "sessions": 0, "error": "", "absent_days": [],
           "student": (form.get("student") or "").strip()}
    try:
        records = attendance.records_between(frm.isoformat(), to.isoformat())
        if ctx["student"]:
            want = attendance.slug(ctx["student"])
            records = [r for r in records
                       if attendance.slug(r.get('student') or '') == want]
        ctx["rows"] = attendance.summarise(records)
        ctx["sessions"] = len({r.get('date') for r in records if r.get('date')})

        # Who was away, hour by hour. Only hours that have FINISHED count: an
        # hour still running, or still to come, says nothing about absence.
        # Filtered per hour rather than per day so today's early sessions can be
        # reported while the later ones stay open.
        present_by_date: Dict[str, set] = {}
        for r in records:
            d = r.get('date') or ''
            if d:
                present_by_date.setdefault(d, set()).add(
                    attendance.slug(r.get('student') or ''))
        if not ctx["student"]:
            d = frm
            while d <= to:
                if d.strftime('%A') in ScheduleService.DAYS:
                    seen = present_by_date.get(d.isoformat(), set())
                    by_hour: Dict[str, List[Dict[str, str]]] = {}
                    for e in _roster_for(d):
                        by_hour.setdefault(e["time"], []).append(e)
                    hours = []
                    for hour in sorted(by_hour, key=_time_key):
                        if not _hour_finished(hour, d):
                            continue
                        away = [e for e in by_hour[hour]
                                if attendance.slug(e["name"]) not in seen]
                        if not away:
                            continue
                        hours.append({
                            "time": hour,
                            "count": len(away),
                            "expected": len(by_hour[hour]),
                            "students": sorted(
                                ({"name": e["name"],
                                  "room": HELP_ROOM_LABEL if e["room"] == HELP_GROUP
                                          else (ROOM_LABEL.get(e["room"]) or e["room"]
                                                or '')}
                                 for e in away),
                                key=lambda a: a["name"])})
                    if hours:
                        ctx["absent_days"].append({
                            "date": d.isoformat(),
                            "label": d.strftime('%a %b %-d'),
                            "count": sum(h["count"] for h in hours),
                            "hours": hours})
                d += timedelta(days=1)
    except Exception as e:
        logger.error("Attendance report failed: %s", e, exc_info=True)
        ctx["error"] = f"Could not read attendance ({type(e).__name__})."
    return templates.TemplateResponse(request, "attendance_report.html", ctx)


@app.post("/attendance/toggle")
async def attendance_toggle(request: Request):
    """
    Mark or unmark one student, for the tap on the tablet.

    Answers with JSON rather than a page so the tile flips immediately: a full
    reload between every child would make walking a room slower than paper.
    """
    form = await request.form()
    try:
        teacher_email = _verify_and_get_email(
            form.get("credential") or form.get("idToken"))
    except Exception as auth_err:
        return JSONResponse({"ok": False, "signed_out": True,
                             "error": "Sign-in has timed out."}, status_code=401)

    student = (form.get("student") or "").strip()
    on = (form.get("on") or "").strip()
    want_present = (form.get("present") or "") == "1"
    if not student or not on:
        return JSONResponse({"ok": False, "error": "Missing student or date."},
                            status_code=400)
    try:
        if want_present:
            attendance.mark_present(
                on, student, session_day=form.get("day") or '',
                subject=form.get("subject") or '', time=form.get("time") or '',
                teacher=form.get("teacher") or '', marked_by=teacher_email)
        else:
            attendance.clear(on, student)
    except Exception as e:
        logger.error("Attendance write failed for %r on %s: %s", student, on, e,
                     exc_info=True)
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}"},
                            status_code=500)
    return JSONResponse({"ok": True, "present": want_present,
                         "student": student})


@app.post("/dashboard")
async def classroom_dashboard(request: Request):
    """
    The single session view: pick a day / room / time, see every student
    scheduled in that slot (grouped by their teacher) with their outstanding
    work, and open any student's full progress grid.
    """
    form_data = await request.form()
    id_token = form_data.get("credential") or form_data.get("idToken")
    sel_day = form_data.get("day") or ""
    sel_subject = form_data.get("subject") or ""
    sel_time = form_data.get("time") or ""

    # Walk-ins ride along in one pipe-delimited field. They deliberately do NOT
    # survive a change of day/room/time -- that form omits them, so switching
    # session clears the group, matching the prototype's resetGroup().
    walkins = [n.strip() for n in (form_data.get("walkins") or "").split("|") if n.strip()]
    adding = (form_data.get("add_walkin") or "").strip()
    removing = (form_data.get("remove_walkin") or "").strip()
    if adding and adding not in walkins:
        walkins.append(adding)
    if removing:
        walkins = [n for n in walkins if n != removing]

    ctx: Dict[str, Any] = {
        "idToken": id_token, "days": ScheduleService.DAYS,
        "subjects": [], "times": [], "groups": [], "unmatched": [],
        "day": sel_day, "subject": sel_subject, "time": sel_time,
        "room_label": ROOM_LABEL,
        "walkins": walkins, "walkins_field": "|".join(walkins),
        "all_students": [], "upcoming_makeups": [], "session_date": "",
        "handover_alert": [],
        "time_override_enabled": TIME_OVERRIDE_ENABLED,
        "simulate_time": (form_data.get("simulate_time") or "") if TIME_OVERRIDE_ENABLED else "",
    }

    timings: Dict[str, float] = {}
    t_request = time.perf_counter()

    def _mark(label: str, since: float) -> float:
        timings[label] = round(time.perf_counter() - since, 2)
        return time.perf_counter()

    # Sign-in is checked on its own, ahead of everything else, because a lapsed
    # one is not a fault -- it is a Google ID token reaching the end of its
    # hour. Folded into the general handler it produced "Could not load this
    # session right now", which reads like a breakage and tells a teacher
    # nothing about what to do. Instead they go back to the sign-in prompt with
    # the session they were looking at carried along, so one click -- often
    # none, since One Tap can reissue silently -- returns them to that screen.
    try:
        t = time.perf_counter()
        teacher_email = _verify_and_get_email(id_token)
        t = _mark('verify_token', t)
    except Exception as auth_err:
        logger.info("Sign-in lapsed, returning to the prompt: %s", auth_err)
        return templates.TemplateResponse(request, "login.html", {
            "google_client_id": GOOGLE_CLIENT_ID,
            "courseId": form_data.get("courseId") or "",
            "addOnToken": form_data.get("addOnToken") or "",
            "sel_day": sel_day,
            "sel_subject": sel_subject,
            "sel_time": sel_time,
            "walkins_raw": form_data.get("walkins") or "",
            "signed_out": True,
        })

    try:

        # Default to today when it's a session day, else the first one.
        if sel_day not in ScheduleService.DAYS:
            today = today_local().strftime('%A')
            sel_day = today if today in ScheduleService.DAYS else ScheduleService.DAYS[0]
        schedule_data = get_day_schedule_cached(sel_day)
        entries = schedule_data["entries"]
        all_makeups = schedule_data["makeups"]
        t = _mark('read_schedule', t)

        subjects = sorted({e["subject"] for e in entries})
        if sel_subject not in subjects:
            sel_subject = "English" if "English" in subjects else (subjects[0] if subjects else "")

        in_subject = [e for e in entries if e["subject"] == sel_subject]
        times = sorted({e["time"] for e in in_subject}, key=_time_key)
        if sel_time not in times:
            sel_time = times[0] if times else ""

        ctx.update({"day": sel_day, "subject": sel_subject, "time": sel_time,
                    "subjects": subjects, "times": times})

        # Students in this slot, grouped by their teacher.
        by_teacher: Dict[str, List[str]] = {}
        for e in in_subject:
            if e["time"] == sel_time and e["teacher"]:
                by_teacher.setdefault(e["teacher"], [])
                if e["student_name"] not in by_teacher[e["teacher"]]:
                    by_teacher[e["teacher"]].append(e["student_name"])

        classroom_svc = ClassroomService(teacher_email)
        t = _mark('init_classroom', t)

        # The course list changes only when a student joins the centre, so it's
        # cached -- it is one large paged response otherwise fetched every time.
        courses_key = ('courses', teacher_email)
        courses = _CACHE.get(courses_key)
        if courses is None:
            courses = classroom_svc.list_teacher_courses()
            _CACHE.put(courses_key, courses, _COURSES_TTL)
        t = _mark('list_courses', t)
        logger.info(f"{sel_day} {sel_subject} {sel_time}: {len(courses)} active courses visible.")

        # Every student who has a Classroom class, for the walk-in picker --
        # a walk-in may be any student in the centre, not just one on today's
        # schedule (students come late, swap, or do make-ups).
        all_students = sorted({
            split_course_name(c.get("name", "")).get("name", "")
            for c in courses
        } - {""})

        # The schedule is the source of truth for WHO should be on screen, so
        # the slate is built from it first. Every scheduled student gets a card
        # no matter what their Classroom lookup does -- a teacher must never
        # have to wonder whether the list is complete. Walk-ins are appended.
        scheduled_names = {n for names in by_teacher.values() for n in names}

        # Make-ups for THIS occurrence of the selected day, in this room and
        # slot. A dated make-up on another Tuesday must not appear on this one.
        # A row whose date isn't a real date can't be placed, so it still shows
        # here (marked undated) rather than being lost.
        session_date = resolve_session_date(sel_day)
        makeups = [
            m for m in all_makeups
            if m["subject"] == sel_subject
            and m["time"] == sel_time
            and (m["date"] == session_date or m["date"] is None)
        ]

        # Advance notice only, deliberately kept OUT of the roster above: that
        # list has to stay exactly who is in the room right now. Not filtered
        # by room or slot -- anything coming up on this day is worth seeing --
        # and no Classroom data is fetched for these, so it costs nothing.
        upcoming_makeups = sorted(
            (m for m in all_makeups if m["date"] and session_date and m["date"] > session_date),
            key=lambda m: (m["date"], _time_key(m["time"]), m["student_name"]),
        )
        upcoming_view = [{
            "date_label": m["date"].strftime('%a %b %-d'),
            "student_name": m["student_name"],
            "time": m["time"],
            "subject": m["subject"],
            "room": ROOM_LABEL.get(m["subject"], m["subject"]),
            "notes": m["notes"],
        } for m in upcoming_makeups]

        slate = [(teacher, n, False)
                 for teacher in sorted(by_teacher)
                 for n in by_teacher[teacher]]
        slate += [(WALKIN_GROUP, n, True)
                  for n in walkins if n not in scheduled_names]
        slate += [(MAKEUP_GROUP, m["student_name"], False) for m in makeups]
        makeup_note = {}
        for m in makeups:
            parts = []
            if not m["date"]:
                parts.append(f"date not specified ({m['date_raw']})")
            if m["notes"]:
                parts.append(m["notes"])
            makeup_note[m["student_name"]] = " · ".join(parts)

        def load_one(entry):
            teacher, name, is_walkin = entry
            card = {
                "teacher": teacher,
                "name": name,
                "walkin": is_walkin,
                "makeup": teacher == MAKEUP_GROUP,
                "makeup_note": makeup_note.get(name, '') if teacher == MAKEUP_GROUP else '',
                "slug": re.sub(r'[^a-z0-9]+', '-', f"{teacher}-{name}".lower()).strip('-'),
                "grade": "", "todo": [], "counts": DataProcessor.counts([]),
                "grids": {s: DataProcessor.grid([]) for s in ("English", "Math")},
                "state": "ok", "load_error": "", "hidden": [],
                "booklets": {"findings": [], "missing": []},
                "subs_error": "",
            }
            course = find_course_for_student(courses, name)
            if not course:
                card["state"] = "unmatched"
                return card
            # The class's Section field carries the grade and academic year
            # verbatim -- "Gr 3 [2026-2027]" -- so it's shown as written rather
            # than reformatted. Falls back to parsing the class name for any
            # class that hasn't had a section set.
            card["grade"] = (
                (course.get("section") or "").strip()
                or split_course_name(course.get("name", "")).get("grade", "")
            )
            try:
                items, card["subs_error"] = classroom_svc.load_student(course["id"])
            except Exception as student_err:
                logger.error(
                    f"Failed to load Classroom data for '{name}' "
                    f"(course {course.get('id')}): {student_err}", exc_info=True)
                card["state"] = "error"
                card["load_error"] = f"{type(student_err).__name__}: {student_err}"
                return card

            # Needs the whole set to decide which Problem of the Day survives,
            # so it runs before anything is filtered out.
            DataProcessor.mark_superseded_pods(items)

            # Booklet handover check. Runs on the UNFILTERED set, since the
            # booklets it reasons about include work that never reaches the
            # badges. Each room has its own checker: the two curricula agree on
            # almost nothing, so english_tracker is a separate module and a
            # change to one room cannot disturb the other.
            checker = {"Math": review_student, "English": review_english}.get(sel_subject)
            if checker:
                try:
                    card["booklets"] = checker(items, today_local(), card["grade"])
                except Exception as e:
                    logger.error(f"Booklet check failed for '{name}' "
                                 f"({sel_subject}): {e}", exc_info=True)

            # Work parked in a Graded topic is finished and out of scope for
            # the session view -- not on the badges and not listed as withheld
            # either, because there is nothing to explain. The grids below
            # still get the full set, since that IS the record.
            session_items = [i for i in items if DataProcessor.in_session_scope(i)]

            shown = [i for i in session_items if DataProcessor.is_trackable(i)]

            # Account for EVERY item in this room that isn't on the badges.
            # Two different things can withhold one: the non-work filters
            # (guide / announcement / superseded POD), or being finished or
            # with the grader. Items dropped for the second reason used to
            # vanish with no trace at all, which is how a real assignment
            # could disappear without even appearing in this list.
            on_badges = {id(i) for i in DataProcessor.todo(
                [i for i in shown if i["subject"] == sel_subject])}
            card["hidden"] = []
            for i in session_items:
                if i["subject"] != sel_subject or id(i) in on_badges:
                    continue
                reason = DataProcessor.hidden_reason(i)
                if not reason:
                    reason = DataProcessor.not_outstanding_reason(i)
                if reason:
                    card["hidden"].append({"title": i["title"], "reason": reason})

            # The raw topic and submission state per item go to the log rather
            # than onto the card: useful when something needs explaining, but
            # meaningless to a teacher mid-session.
            logger.info(
                "course=%s items=%s | %s", course.get('id'), len(session_items),
                " ; ".join(
                    f"{i['title'][:40]}|{i['topic']}|{i['submission_state'] or 'no-sub'}"
                    f"|{i['status']}" for i in session_items if i["subject"] == sel_subject
                ),
            )

            subject_items = [i for i in shown if i["subject"] == sel_subject]
            card["todo"] = DataProcessor.todo(subject_items)
            card["counts"] = DataProcessor.counts(subject_items)
            # The grid is the full record, so it keeps the Graded topic --
            # only the non-work filters apply here.
            grid_items = [i for i in items if DataProcessor.is_trackable(i)]
            card["grids"] = {
                s: DataProcessor.grid([i for i in grid_items if i["subject"] == s])
                for s in ("English", "Math")
            }
            return card

        # Students load concurrently -- each is 3 independent API calls, and
        # sequentially a full session took long enough to risk a timeout.
        # Live every time: assignment data is never cached.
        if slate:
            with ThreadPoolExecutor(max_workers=min(16, len(slate))) as pool:
                cards = list(pool.map(load_one, slate))
        else:
            cards = []
        t = _mark('load_students', t)

        groups = []
        for teacher in sorted(by_teacher) + [WALKIN_GROUP, MAKEUP_GROUP]:
            in_group = [c for c in cards if c["teacher"] == teacher]
            if in_group:
                groups.append({"teacher": teacher, "students": in_group})

        summary = {
            "scheduled": sum(1 for c in cards if not c["walkin"] and not c["makeup"]),
            "walkins": sum(1 for c in cards if c["walkin"]),
            "makeups": sum(1 for c in cards if c["makeup"]),
            "loaded": sum(1 for c in cards if c["state"] == "ok"),
            "unmatched": sum(1 for c in cards if c["state"] == "unmatched"),
            "errors": sum(1 for c in cards if c["state"] == "error"),
        }
        # A child rostered twice in one hour -- usually the same name entered
        # under two teachers in the schedule Sheet. The banner merges their
        # findings, but silently absorbing it would leave the Sheet wrong
        # indefinitely, so it is named where an admin will see it.
        _names = [c["name"] for c in cards]
        ctx["duplicate_students"] = sorted(
            {n for n in _names if _names.count(n) > 1})
        # Only raised in the last ten minutes of the session, so it reads as a
        # last call before the child leaves rather than nagging all lesson --
        # a teacher legitimately hands the booklets over at the end.
        handover_alert = []
        fake_now = simulated_now(form_data.get("simulate_time") or "")
        # The pretend clock moves the time but not the date, so the session-date
        # guard would confine every test to the four days the centre actually
        # runs -- and the override exists precisely so a session need not be
        # waited for. While a pretend time is in play the day dropdown stands in
        # for the date. Nothing changes in production: with the override off
        # fake_now is None and the real date is enforced.
        window_open = in_handover_window(
            sel_time, None if fake_now else session_date, fake_now)

        if window_open:
            for c in cards:
                if c["state"] != "ok":
                    continue
                b = c.get("booklets") or {}
                missing, notes = b.get("missing") or [], b.get("notes") or []
                # A few findings need to stand out from the rest -- a level test
                # never started, or the same booklet issued twice. They are
                # carried separately so the banner can mark them rather than
                # letting them read as one more line among many.
                severe = b.get("severe") or []
                if not (missing or notes):
                    continue
                # One line per student, however many times the schedule lists
                # them. A child rostered under two teachers in the same hour
                # gets two cards, and printing their findings once per card put
                # Niyam Shah's three duplicate booklets in the banner twice --
                # doubling the length of the very list a teacher has to scan.
                seen = next((a for a in handover_alert
                             if a["name"] == c["name"]), None)
                if seen is None:
                    handover_alert.append({"name": c["name"], "missing": [],
                                           "notes": [], "severe": []})
                    seen = handover_alert[-1]
                for key, values in (("missing", missing), ("notes", notes),
                                    ("severe", severe)):
                    for v in values:
                        if v not in seen[key]:
                            seen[key].append(v)

        # Logged on EVERY request, not just when it fires. "The banner didn't
        # appear" has several possible causes -- outside the window, the wrong
        # room, no booklets recognised -- and without this line none of them
        # can be told apart after the fact.
        logger.info(
            "HANDOVER CHECK day=%s time=%s subject=%s | now=%s%s session_date=%s "
            "window_open=%s | %s",
            sel_day, sel_time, sel_subject,
            (fake_now or now_local()).strftime('%Y-%m-%d %H:%M'),
            " (SIMULATED)" if fake_now else "",
            session_date, window_open,
            " ; ".join(
                # The grade rides along: it decides whether a student is on the
                # booklet curriculum at all, and an unset Section reads as
                # unknown and is checked anyway. Without it in the line there
                # is no way to tell a wrongly-flagged older student from one
                # whose class simply has no grade recorded.
                f"{c['name']} [{c.get('grade') or 'NO GRADE'}]: " + (
                    "; ".join(
                        f"{f['kind']}/{f['series'] or '-'}"
                        + (f"->{','.join(f['expected'])}" if f['expected'] else "")
                        for f in (c.get("booklets") or {}).get("findings", [])
                    ) or "no findings")
                for c in cards if c["state"] == "ok"
            ) or "no students",
        )

        if handover_alert:
            logger.warning(
                "HANDOVER ALERT %s %s %s: %s", sel_day, sel_time, sel_subject,
                "; ".join(
                    f"{a['name']} "
                    + " / ".join(
                        ([f"missing {', '.join(a['missing'])}"] if a['missing'] else [])
                        + a['notes'])
                    for a in handover_alert))

        timings['total'] = round(time.perf_counter() - t_request, 2)
        logger.info(f"{sel_day} {sel_subject} {sel_time}: {summary} timings={timings}")

        ctx.update({
            "groups": groups,
            "summary": summary,
            "timings": timings,
            "all_students": all_students,
            "handover_alert": handover_alert,
            "upcoming_makeups": upcoming_view,
            "session_date": session_date.strftime('%a %b %-d') if session_date else '',
            "unmatched": [c["name"] for c in cards if c["state"] == "unmatched"],
        })
        return templates.TemplateResponse(request, "session_dashboard.html", ctx)

    except Exception as e:
        logger.error(f"Failed to load dashboard: {e}", exc_info=True)
        # This page is reachable only by the three IAP-authorized staff
        # accounts, so showing the real error beats a generic message that
        # forces a log dig on every failure.
        ctx["error"] = "Could not load this session right now."
        ctx["error_detail"] = f"{type(e).__name__}: {escape(str(e))}"
        return templates.TemplateResponse(request, "session_dashboard.html", ctx)