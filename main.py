from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.templating import Jinja2Templates
from googleapiclient.discovery import build
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

def get_teacher_creds_remote_signing(teacher_email: str, scopes: List[str]):
    """
    Uses the IAM Credentials API to sign a JWT for Domain-Wide Delegation.
    This avoids the need for a local Service Account JSON key.
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
        "sub": teacher_email,
        "aud": "https://oauth2.googleapis.com/token",
        "iat": iat,
        "exp": exp,
        "scope": " ".join(scopes)
    }

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

class DataProcessor:
    """
    Turns Classroom API responses into the item shape the UI renders.
    Ported from enopi-fic-pwa/src/data/progress.js (parseStrand, parseLevel,
    todo, sessions, grid), widened where that file's regexes were written
    against mock titles rather than the real Classroom ones.
    """

    # An assignment is a "FIC" (Fix-It Center) item purely by its title
    # containing the word "fic" -- per enopi-fic-pwa/spike/classroom-spike.mjs.
    # Independent of completion status.
    FIC_RE = re.compile(r'\bfic\b', re.IGNORECASE)

    # Title -> curriculum strand. Order matters: the first match wins, so more
    # specific patterns come before the ones that would also match them.
    STRAND_MAP = [
        (re.compile(r'\broots?\b|root words', re.I), 'Roots', 'Roots'),
        (re.compile(r'\bdgp\b', re.I), 'DGP', 'DGP'),
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
    def _subject_and_category(meta: Dict[str, Any], topic_by_id: Dict[str, str]):
        """
        Both come from the Classroom topic name -- topics are named
        "English Classwork", "English Homework", "Math Classwork", "Math Homework".
        """
        topic = (topic_by_id.get(meta.get('topicId')) or '').lower()
        subject = 'English' if 'english' in topic else ('Math' if 'math' in topic else None)
        category = 'H' if 'homework' in topic else ('C' if 'classwork' in topic else None)
        return subject, category

    @staticmethod
    def build_item(
        meta: Dict[str, Any],
        sub: Dict[str, Any],
        topic_by_id: Dict[str, str],
    ) -> Dict[str, Any]:
        title = meta.get('title') or ''
        subject, category = DataProcessor._subject_and_category(meta, topic_by_id)
        strand = DataProcessor.parse_strand(title)

        state = sub.get('state')
        graded = sub.get('assignedGrade') is not None
        if graded or state == 'RETURNED':
            status = 'done'
        elif state == 'TURNED_IN':
            status = 'submitted'
        else:
            status = 'notdone'

        is_fic_title = bool(DataProcessor.FIC_RE.search(title))
        # An outstanding FIC is its own status (red); a finished one is just
        # done, tagged so the UI can mark it "fic ✓".
        if is_fic_title and status == 'notdone':
            status = 'fic'

        due = DataProcessor._due(meta)
        posted = DataProcessor._posted(meta)
        done = status in ('done', 'submitted')

        return {
            "title": title,
            "subject": subject,
            "category": category,
            "status": status,
            "was_fic": is_fic_title and done,
            "strand_code": strand["code"],
            "strand_label": strand["label"],
            "level": DataProcessor.parse_level(title),
            "posted_key": posted.isoformat() if posted else '',
            "posted_label": posted.strftime('%b %-d') if posted else '',
            "month_label": posted.strftime('%b %Y') if posted else '',
            "due_label": due.strftime('%b %-d') if due else '',
            "overdue": bool(due and not done and due < date.today()),
            "score": sub.get('assignedGrade'),
            "max_points": meta.get('maxPoints'),
        }

    @staticmethod
    def todo(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Outstanding work only: not-started first, then FICs (progress.js todo())."""
        order = {'notdone': 0, 'fic': 1}
        return sorted(
            (i for i in items if i["status"] in order),
            key=lambda i: order[i["status"]],
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
            slot = by_date.setdefault(key, {"cells": {}, "label": i["posted_label"], "month": i["month_label"]})
            slot["cells"][i["strand_code"]] = i

        rows = []
        for key in sorted(by_date, reverse=True):
            slot = by_date[key]
            rows.append({
                "date": slot["label"],
                "month": slot["month"],
                "cells": [slot["cells"].get(s["code"]) for s in strands],
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
        try:
            # Classroom API discovery is cached internally by the library, but we disable file cache for Cloud Shell
            self.service = build('classroom', 'v1', credentials=self.creds, cache_discovery=False)
        except Exception as e:
            logger.error(f"Failed to build Classroom service (likely identity/Gaia issue): {e}")
            raise

    def get_course_details(self, course_id: str):
        return self.service.courses().get(id=course_id).execute()

    def list_teacher_courses(self) -> List[Dict[str, Any]]:
        """Lists all active courses where the user is a teacher."""
        response = self.service.courses().list(teacherId='me', courseStates=['ACTIVE']).execute()
        return response.get('courses', [])

    def get_student_roster(self, course_id: str) -> Dict[str, str]:
        roster_response = self.service.courses().students().list(courseId=course_id).execute()
        students_list = roster_response.get('students', [])
        return {
            s.get('userId'): s.get('profile', {}).get('name', {}).get('fullName', 'Unknown Student')
            for s in students_list
        }

    def get_coursework(self, course_id: str) -> List[Dict[str, Any]]:
        response = self.service.courses().courseWork().list(
            courseId=course_id, courseWorkStates=['PUBLISHED', 'DRAFT']
        ).execute()
        return response.get('courseWork', [])

    def get_topics(self, course_id: str) -> Dict[str, str]:
        response = self.service.courses().topics().list(courseId=course_id).execute()
        return {t['topicId']: t.get('name', '') for t in response.get('topic', [])}

    def get_submissions_batch(
        self,
        course_id: str,
        assignments: List[Dict[str, Any]],
        topic_by_id: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """One item per (assignment, submission) in this student's course."""
        items: List[Dict[str, Any]] = []

        def callback(request_id, response, exception):
            if exception:
                logger.error(f"Error fetching submissions for {request_id}: {exception}")
                return
            meta = next((a for a in assignments if a.get('id') == request_id), {})
            for sub in response.get('studentSubmissions', []):
                items.append(DataProcessor.build_item(meta, sub, topic_by_id))

        batch = self.service.new_batch_http_request(callback=callback)
        for assignment in assignments:
            cw_id = assignment.get('id')
            batch.add(self.service.courses().courseWork().studentSubmissions().list(
                courseId=course_id, courseWorkId=cw_id), request_id=cw_id)
        batch.execute()
        return items

    def load_student(self, course_id: str) -> List[Dict[str, Any]]:
        """All assignment items for one student's Classroom course."""
        assignments = self.get_coursework(course_id)
        if not assignments:
            return []
        topic_by_id = self.get_topics(course_id)
        return self.get_submissions_batch(course_id, assignments, topic_by_id)


def _normalize_name(name: str) -> str:
    name = re.sub(r'\(.*?\)', '', name)  # strip "(online)" etc.
    return re.sub(r'\s+', ' ', name).strip().lower()


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
        schedule_svc = ScheduleService(SCHEDULE_SPREADSHEET_ID, DEFAULT_CREDS)

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

        groups, unmatched = [], []
        for teacher in sorted(by_teacher):
            students = []
            for name in by_teacher[teacher]:
                course = find_course_for_student(courses, name)
                if not course:
                    unmatched.append(name)
                    continue
                items = classroom_svc.load_student(course["id"])
                subject_items = [i for i in items if i["subject"] == sel_subject] or items
                students.append({
                    "name": name,
                    "slug": re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-'),
                    "todo": DataProcessor.todo(subject_items),
                    "grids": {
                        s: DataProcessor.grid([i for i in items if i["subject"] == s])
                        for s in ("English", "Math")
                    },
                })
            if students:
                groups.append({"teacher": teacher, "students": students})

        ctx.update({"groups": groups, "unmatched": unmatched})
        return templates.TemplateResponse(request, "session_dashboard.html", ctx)

    except Exception as e:
        logger.error(f"Failed to load dashboard: {e}", exc_info=True)
        ctx["error"] = "Could not load this session right now. Please try again in a moment."
        return templates.TemplateResponse(request, "session_dashboard.html", ctx)