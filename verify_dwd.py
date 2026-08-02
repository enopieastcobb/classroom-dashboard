import os
import time
import json
import google.auth
from google.oauth2 import credentials as oauth2_credentials
from google.auth.transport import requests as transport_requests
from googleapiclient.discovery import build

def test_domain_wide_delegation():
    """
    Verifies DWD by attempting to impersonate a teacher and listing courses.
    Requires environment variables: TEST_TEACHER_EMAIL and SERVICE_ACCOUNT_EMAIL.
    """
    teacher_email = os.environ.get("TEST_TEACHER_EMAIL")
    sa_email = os.environ.get("SERVICE_ACCOUNT_EMAIL")
    
    if not teacher_email or not sa_email:
        print("❌ Error: Please set environment variables:")
        print("   export TEST_TEACHER_EMAIL='teacher@yourdomain.com'")
        print("   export SERVICE_ACCOUNT_EMAIL='your-sa@project.iam.gserviceaccount.com'")
        return

    # This scope must match what you authorized in the Workspace Admin Console
    scopes = ['https://www.googleapis.com/auth/classroom.courses.readonly']
    
    print(f"🔄 Attempting to impersonate {teacher_email} using {sa_email}...")

    try:
        # 1. Get default environment credentials (e.g., from Cloud Shell or Cloud Run)
        creds, _ = google.auth.default()
        auth_request = transport_requests.Request()
        creds.refresh(auth_request)
        
        # 2. Use IAM Credentials API to sign a JWT for DWD
        iam_service = build('iamcredentials', 'v1', credentials=creds)
        payload = {
            "iss": sa_email,
            "sub": teacher_email,
            "scope": " ".join(scopes),
            "aud": "https://oauth2.googleapis.com/token",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600
        }
        name = f"projects/-/serviceAccounts/{sa_email}"
        signed_response = iam_service.projects().serviceAccounts().signJwt(
            name=name, 
            body={"payload": json.dumps(payload)}
        ).execute()
        
        # 3. Exchange signed JWT for an Access Token
        resp = auth_request.session.post("https://oauth2.googleapis.com/token", 
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": signed_response["signedJwt"]})
        resp.raise_for_status()
        
        # 4. Perform a real API call to confirm success
        service = build('classroom', 'v1', credentials=oauth2_credentials.Credentials(resp.json()["access_token"]))
        service.courses().list(pageSize=1).execute()
        print(f"✅ Success! Domain-Wide Delegation is active for {teacher_email}")
    except Exception as e:
        print(f"❌ DWD Verification Failed: {e}")

if __name__ == "__main__":
    test_domain_wide_delegation()