import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
import json
import os

# Import the app instance from main.py
from main import app

client = TestClient(app)

@pytest.fixture
def mock_env(monkeypatch):
    """Set up environment variables required by the app."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project-id")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("SERVICE_ACCOUNT_EMAIL", "fake-sa@test-project-id.iam.gserviceaccount.com")

def test_classroom_dashboard_full_flow(mock_env, mocker):
    """
    Integration test for the /dashboard endpoint.
    Mocks Remote Signing, OIDC verification, and Classroom API calls to simulate production flow.
    """
    
    # 1. Mock the Remote Signing function to bypass network calls
    mocker.patch("main.get_teacher_creds_remote_signing", return_value=MagicMock())

    # 2. Mock OIDC token verification
    mock_verify = mocker.patch("main.google_id_token.verify_oauth2_token")
    mock_verify.return_value = {"email": "teacher@yourdomain.com"}

    # 3. Mock Google Classroom API Service Build
    mock_build = mocker.patch("main.build")
    mock_service = MagicMock()
    mock_build.return_value = mock_service

    # Mock Course Details
    mock_service.courses().get().execute.return_value = {
        "name": "Integration Test Course",
        "id": "12345"
    }

    # Mock Student Roster
    mock_service.courses().students().list().execute.return_value = {
        "students": [
            {"userId": "user1", "profile": {"name": {"fullName": "John Doe"}}}
        ]
    }

    # Mock Coursework List
    mock_service.courses().courseWork().list().execute.return_value = {
        "courseWork": [
            {"id": "cw1", "title": "Homework 1", "maxPoints": 100}
        ]
    }

    # Mock the submissions batch processing logic to simulate data return
    processed_records = [{
        "Student": "John Doe", "Assignment": "Homework 1", "Completed": 1,
        "Status": "GRADED", "Score": 85, "Max Points": 100, "Grade %": 85.0
    }]
    mocker.patch("main.ClassroomService.get_submissions_batch", return_value=processed_records)

    # 4. Perform the request to the /dashboard endpoint
    response = client.post("/dashboard", data={
        "courseId": "12345",
        "credential": "mock-oidc-token-string"
    })

    # 5. Verify the results
    assert response.status_code == 200
    assert "Integration Test Course" in response.text
    assert "John Doe" in response.text