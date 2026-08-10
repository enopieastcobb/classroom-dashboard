from fastapi import FastAPI, Request
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
from datetime import date, datetime
from typing import List, Dict, Any, Optional

from schedule_service import ScheduleService

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
        return response

app.add_middleware(SecurityHeadersMiddleware)

def nonce_processor(request: Request):
    """Context processor to make the CSP nonce available in all templates."""
    return {"nonce": getattr(request.state, "nonce", "")}

templates = Jinja2Templates(directory="templates", context_processors=[nonce_processor])

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

    return oauth2_credentials.Credentials(access_token)


def get_teacher_creds_remote_signing(teacher_email: str, scopes: List[str]):
    """Domain-Wide Delegation: act as `teacher_email`."""
    return get_scoped_creds(scopes, subject=teacher_email)


SHEETS_SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']


def get_schedule_service() -> ScheduleService:
    """Reads the weekly schedule Sheet as the service account it's shared with."""
    return ScheduleService(SCHEDULE_SPREADSHEET_ID, get_scoped_creds(SHEETS_SCOPES))

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
        r'algebra|ratio|multiplication|division', re.IGNORECASE)

    # Title -> curriculum strand. Order matters: the first match wins, so more
    # specific patterns come before the ones that would also match them.
    STRAND_MAP = [
        (re.compile(r'\broots?\b|root words', re.I), 'Roots', 'Roots'),
        (re.compile(r'\bdgp\b', re.I), 'DGP', 'DGP'),
        (re.compile(r'problem of the day', re.I), 'Problem of the Day', 'POD'),
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
        strand = DataProcessor.parse_strand(title)
        status = DataProcessor._status(topic_name, title)

        due = DataProcessor._due(meta)
        posted = DataProcessor._posted(meta)
        done = status in ('done', 'submitted')

        materials = meta.get('materials') or []
        material = ''
        if materials:
            material = (materials[0].get('driveFile', {}).get('driveFile', {}).get('title') or '')

        return {
            "title": title,
            "subject": subject,
            "topic": topic,
            "category": category,
            "status": status,
            # The .fic tag is kept even once cleared, so FIC history survives.
            "was_fic": bool(DataProcessor.FIC_RE.search(title)),
            "fix_by": DataProcessor._fix_by(title),
            # "inc" in a title means the work came back incomplete.
            "incomplete": bool(re.search(r'\binc\b', title, re.I)),
            "strand_code": strand["code"],
            "strand_label": strand["label"],
            "level": DataProcessor.parse_level(title),
            "posted_key": posted.isoformat() if posted else '',
            "posted_label": posted.strftime('%b %-d') if posted else '',
            "month_label": posted.strftime('%b %Y') if posted else '',
            "due_label": due.strftime('%b %-d') if due else '',
            # Sortable form, so items can be ordered most-urgent-first within
            # a priority tier. Undated sorts last.
            "due_key": due.isoformat() if due else '9999-12-31',
            "overdue": bool(due and not done and due < date.today()),
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
            "late": bool((sub or {}).get('late')),
            "submission_state": (sub or {}).get('state') or '',
        }

    # Strands kept off this screen entirely. "Problem of the Day" is tracked
    # separately for now -- dropping 'POD' from this set brings it straight back.
    EXCLUDED_STRAND_CODES = {'POD'}

    @staticmethod
    def is_trackable(item: Dict[str, Any]) -> bool:
        """
        Whether an item is real assignable work belonging on this screen.

        Everything in Homework is real assigned work and is ALWAYS kept, due
        date or not -- a teacher may set an essay with no date on it, and that
        is still work the student owes.

        Elsewhere, no due date means it isn't owed: reference material ("Proof
        Reading Guide", "DGP Gr2 Guide") and notices ("Summer Hours for 2026")
        sit in a Classwork topic with no due date and would otherwise read as
        permanently "not done", padding every alert list.

        Hard-copy handouts ("G-4 HC") DO carry a due date -- the physical book
        is due back the following week -- so they are tracked work regardless.
        """
        if item["strand_code"] in DataProcessor.EXCLUDED_STRAND_CODES:
            return False
        if 'homework' in (item.get("topic") or '').lower():
            return True
        return bool(item["due_label"])

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
        Urgency tier, in the order teachers work a session:

          0  FIC       - turned in, graded, too many mistakes: must be reworked
                         with the student in class. The alert condition.
          1  INC       - came back incomplete
          2  Past due  - owed and the due date has gone by
          3  Due later - due today or in future
        """
        if item["status"] == 'fic':
            return 0
        if item["incomplete"]:
            return 1
        if item["overdue"]:
            return 2
        return 3

    @staticmethod
    def _section_rank(item: Dict[str, Any]) -> int:
        """Classwork before Homework -- the order a session is actually worked."""
        return {'C': 0, 'H': 1}.get(item.get("category"), 2)

    @staticmethod
    def todo(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Outstanding work only: all Classwork first, then all Homework, and
        within each section FIC -> INC -> past due -> due later, then by due
        date so the most urgent leads its group.

        This deliberately replaces progress.js todo(), which had only two
        tiers, ignored the section, and put not-started work ahead of FICs.
        """
        return sorted(
            (i for i in items if i["status"] in ('fic', 'notdone')),
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
            self.service = build('classroom', 'v1', credentials=self.creds, cache_discovery=False)
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

    def load_student(self, course_id: str) -> List[Dict[str, Any]]:
        """
        All assignment items for one student's Classroom course.

        Three calls per student: coursework, topics, and all submissions.
        Status comes from the topic the work sits in, but the SCORE only
        exists on the submission, so graded items need it to show their marks.
        """
        assignments = self.get_coursework(course_id)
        if not assignments:
            return []
        topic_by_id = self.get_topics(course_id)
        try:
            subs_by_cw = self.get_submissions_by_coursework(course_id)
        except Exception as e:
            # Scores are enrichment -- losing them must not lose the items.
            logger.warning(f"Course {course_id}: could not load submissions ({e}); scores omitted.")
            subs_by_cw = {}
        items = [
            DataProcessor.build_item(a, topic_by_id, subs_by_cw.get(a.get('id')))
            for a in assignments
        ]

        kept = [i for i in items if DataProcessor.is_trackable(i)]
        if len(kept) != len(items):
            logger.info(
                f"Course {course_id}: showing {len(kept)} of {len(items)} items "
                f"({len(items) - len(kept)} filtered as reference material / "
                f"no due date / excluded strand)."
            )
        return kept


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


ROOM_LABEL = {"English": "English Room", "Math": "Maths Room", "Weenopi": "Weenopi"}


def _time_key(t: str) -> tuple:
    """Sort '9:00 AM' before '12:00 PM' before '3:00 PM'."""
    try:
        return (0, datetime.strptime(t.strip(), "%I:%M %p").time())
    except ValueError:
        return (1, t)


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

    ctx: Dict[str, Any] = {
        "idToken": id_token, "days": ScheduleService.DAYS,
        "subjects": [], "times": [], "groups": [], "unmatched": [],
        "day": sel_day, "subject": sel_subject, "time": sel_time,
        "room_label": ROOM_LABEL,
    }

    try:
        teacher_email = _verify_and_get_email(id_token)
        schedule_svc = get_schedule_service()

        # Default to today when it's a session day, else the first one.
        if sel_day not in ScheduleService.DAYS:
            today = date.today().strftime('%A')
            sel_day = today if today in ScheduleService.DAYS else ScheduleService.DAYS[0]
        entries = schedule_svc.get_day_schedule(sel_day)

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
        courses = classroom_svc.list_teacher_courses()
        logger.info(f"{sel_day} {sel_subject} {sel_time}: {len(courses)} active courses visible.")

        # The schedule is the source of truth for WHO should be on screen, so
        # the slate is built from it first. Every scheduled student gets a card
        # no matter what their Classroom lookup does -- a teacher must never
        # have to wonder whether the list is complete.
        slate = [(t, n) for t in sorted(by_teacher) for n in by_teacher[t]]

        def load_one(entry):
            teacher, name = entry
            card = {
                "teacher": teacher,
                "name": name,
                "slug": re.sub(r'[^a-z0-9]+', '-', f"{teacher}-{name}".lower()).strip('-'),
                "grade": "", "todo": [], "counts": DataProcessor.counts([]),
                "grids": {s: DataProcessor.grid([]) for s in ("English", "Math")},
                "state": "ok", "load_error": "",
            }
            course = find_course_for_student(courses, name)
            if not course:
                card["state"] = "unmatched"
                return card
            card["grade"] = split_course_name(course.get("name", "")).get("grade", "")
            try:
                items = classroom_svc.load_student(course["id"])
            except Exception as student_err:
                logger.error(
                    f"Failed to load Classroom data for '{name}' "
                    f"(course {course.get('id')}): {student_err}", exc_info=True)
                card["state"] = "error"
                card["load_error"] = f"{type(student_err).__name__}: {student_err}"
                return card

            subject_items = [i for i in items if i["subject"] == sel_subject]
            card["todo"] = DataProcessor.todo(subject_items)
            card["counts"] = DataProcessor.counts(subject_items)
            card["grids"] = {
                s: DataProcessor.grid([i for i in items if i["subject"] == s])
                for s in ("English", "Math")
            }
            return card

        # Students load concurrently -- each is 3 independent API calls, and
        # sequentially a full session took long enough to risk a timeout.
        if slate:
            with ThreadPoolExecutor(max_workers=min(8, len(slate))) as pool:
                cards = list(pool.map(load_one, slate))
        else:
            cards = []

        groups = []
        for teacher in sorted(by_teacher):
            in_group = [c for c in cards if c["teacher"] == teacher]
            if in_group:
                groups.append({"teacher": teacher, "students": in_group})

        summary = {
            "scheduled": len(cards),
            "loaded": sum(1 for c in cards if c["state"] == "ok"),
            "unmatched": sum(1 for c in cards if c["state"] == "unmatched"),
            "errors": sum(1 for c in cards if c["state"] == "error"),
        }
        logger.info(f"{sel_day} {sel_subject} {sel_time}: {summary}")

        ctx.update({
            "groups": groups,
            "summary": summary,
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