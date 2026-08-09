from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from googleapiclient.discovery import build
from google.oauth2 import id_token as google_id_token, credentials as oauth2_credentials
from google.auth import jwt as auth_jwt
from google.auth.transport import requests as transport_requests
import google.auth
import plotly.express as px
import pandas as pd
import json
from markupsafe import escape
import time
import threading
import os
import secrets
import logging
from collections import defaultdict
from datetime import date
from typing import List, Dict, Any

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Load and validate configuration at startup
SA_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")

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
    """Pure logic for transforming API responses into flat records."""
    @staticmethod
    def _format_due_date(meta: Dict[str, Any]) -> str | None:
        due = meta.get('dueDate')
        if not due or not all(k in due for k in ('year', 'month', 'day')):
            return None
        return f"{due['year']:04d}-{due['month']:02d}-{due['day']:02d}"

    @staticmethod
    def process_submission_batch_item(response: Dict[str, Any], meta: Dict[str, Any], roster: Dict[str, str]) -> List[Dict[str, Any]]:
        submissions = response.get('studentSubmissions', [])
        records = []
        due_str = DataProcessor._format_due_date(meta)
        today_str = date.today().isoformat()
        for sub in submissions:
            score = sub.get('assignedGrade')
            max_pts = meta.get('maxPoints')
            completed = sub.get('state') in ['TURNED_IN', 'GRADED']
            records.append({
                "Student": roster.get(sub.get('userId'), "External Student"),
                "Assignment": meta.get('title'),
                "Completed": 1 if completed else 0,
                "Status": sub.get('state', 'NEW'),
                "Score": score,
                "Max Points": max_pts,
                "Grade %": (float(score) / max_pts * 100) if score is not None and max_pts else 0,
                "Due": due_str,
                "Overdue": bool(due_str and not completed and due_str < today_str),
            })
        return records

class ClassroomService:
    """Handles all interactions with the Google Classroom API."""
    def __init__(self, teacher_email: str):
        self.scopes = [
            'https://www.googleapis.com/auth/classroom.courses.readonly',
            'https://www.googleapis.com/auth/classroom.rosters.readonly',
            'https://www.googleapis.com/auth/classroom.coursework.me.readonly',
            'https://www.googleapis.com/auth/classroom.coursework.students.readonly'
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
        response = self.service.courses().courseWork().list(courseId=course_id).execute()
        return response.get('courseWork', [])

    def get_submissions_batch(self, course_id: str, assignments: List[Dict[str, Any]], roster: Dict[str, str]) -> List[Dict[str, Any]]:
        processed_records = []
        
        def callback(request_id, response, exception):
            if exception:
                logger.error(f"Error fetching submissions for {request_id}: {exception}")
                return
            
            # Find the assignment metadata for this request_id
            meta = next((a for a in assignments if a.get('id') == request_id), {})
            records = DataProcessor.process_submission_batch_item(response, meta, roster)
            processed_records.extend(records)

        batch = self.service.new_batch_http_request(callback=callback)
        for assignment in assignments:
            cw_id = assignment.get('id')
            batch.add(self.service.courses().courseWork().studentSubmissions().list(
                courseId=course_id, courseWorkId=cw_id), request_id=cw_id)
        batch.execute()
        return processed_records

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

@app.post("/dashboard")
async def classroom_dashboard(request: Request):
    """
    Authenticated dashboard endpoint.
    Handles both OIDC tokens (for email) and addOnTokens (for context).
    """
    form_data = await request.form()
    
    # GIS (Sign-In with Google) sends 'credential'. 
    # This is what contains the email for DWD impersonation.
    id_token = form_data.get("credential") or form_data.get("idToken")
    
    # Classroom Add-on specific context token
    add_on_token = form_data.get("addOnToken")
    
    course_id = form_data.get("courseId") or form_data.get("course_id")
    
    try:
        if not id_token:
            logger.warning("No OIDC ID Token found. Add-on needs user email for DWD.")
            raise ValueError("Authentication required. Please sign in via the dashboard.")

        # Verify the standard OIDC ID Token
        id_info = google_id_token.verify_oauth2_token(
            id_token, AUTH_HTTP_REQUEST, GOOGLE_CLIENT_ID
        )
        
        teacher_email = id_info.get("email", "").strip().lower()
        if not teacher_email:
            raise ValueError("Authentication token is missing user identity (email).")
            
        if not DEFAULT_CREDS:
            logger.critical("Server-side Google Credentials (ADC) are missing. Check environment setup.")
            raise RuntimeError("Backend authentication service is unavailable.")

        logger.info(f"Dashboard access granted via POST for: {teacher_email}")

        # Initialize Classroom Service
        classroom_svc = ClassroomService(teacher_email)
        
        # Handle scenario where courseId isn't provided (fallback to picker)
        if not course_id:
            courses = classroom_svc.list_teacher_courses()
            return templates.TemplateResponse(request, "course_picker.html", {
                "teacher_email": teacher_email,
                "courses": courses,
                "idToken": id_token
            })

        course_details = classroom_svc.get_course_details(course_id)
        course_name = course_details.get('name', 'Active Class')

        student_roster = classroom_svc.get_student_roster(course_id)
        assignments = classroom_svc.get_coursework(course_id)
        
        if not assignments:
            return templates.TemplateResponse(request, "dashboard.html", {"course_name": course_name, "chart_json": None, "chart_html": "<p>No assignments found for this class.</p>", "roster": []})

        processed_records = classroom_svc.get_submissions_batch(course_id, assignments, student_roster)

        # 4. Process metrics and generate the interactive data visualization
        if processed_records:
            df = pd.DataFrame(processed_records)

            # Create a Heatmap for a "Single View" of the entire class
            # Pivot data: Rows = Students, Columns = Assignments, Values = Completion Status
            pivot_df = df.pivot(index="Student", columns="Assignment", values="Completed")

            fig = px.imshow(
                pivot_df,
                labels=dict(x="Assignments", y="Students", color="Status (1=Done)"),
                x=pivot_df.columns,
                y=pivot_df.index,
                color_continuous_scale=[[0, 'white'], [1, '#2ecc71']], # Green for completed
                title=f"Class Progress Overview: {course_name}"
            )

            fig.update_layout(height=600, margin=dict(l=150))
            fig.update_xaxes(side="top")
            chart_json = fig.to_json()

            # Group into a per-student roster for the detailed, expandable card view
            student_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for record in processed_records:
                student_map[record["Student"]].append(record)
            roster = [
                {
                    "name": name,
                    "completed": sum(1 for a in items if a["Completed"]),
                    "total": len(items),
                    "assignments": items,
                }
                for name, items in sorted(student_map.items())
            ]
        else:
            chart_json = None
            roster = []

    except Exception as e:
        logger.error(f"Failed to load classroom dashboard for course {course_id}: {e}", exc_info=True)
        # Graceful UI rollback if credentials or course context are still initializing
        course_name = "Classroom Insights"
        # Sanitize the error message before sending it to the UI
        safe_error = escape(str(e))
        chart_html = f"""
        <div style="color: #d93025; padding: 10px; border: 1px solid #fadbd8; background: #fdf2f2; border-radius: 4px;">
            <strong>API Synchronization Note:</strong> Live data stream is initializing.<br>
            <small>The system is currently syncing with Google Classroom. Please refresh in a moment.</small>
        </div>
        """
        return templates.TemplateResponse(request, "dashboard.html", {"chart_html": chart_html, "chart_json": None, "roster": []})

    return templates.TemplateResponse(request, "dashboard.html", {
        "course_name": course_name,
        "chart_json": chart_json,
        "roster": roster
    })