import pytest
from main import DataProcessor

def test_process_submission_batch_item_success():
    # Mock API response for submissions
    response = {
        'studentSubmissions': [
            {'userId': 's1', 'state': 'TURNED_IN', 'assignedGrade': 85},
            {'userId': 's2', 'state': 'GRADED', 'assignedGrade': 100},
            {'userId': 's3', 'state': 'CREATED', 'assignedGrade': None}
        ]
    }
    
    # Mock coursework metadata
    meta = {
        'title': 'Unit Test Assignment',
        'maxPoints': 100
    }
    
    # Mock roster mapping
    roster = {
        's1': 'Alice',
        's2': 'Bob',
        's3': 'Charlie'
    }

    # Execute processing logic
    records = DataProcessor.process_submission_batch_item(response, meta, roster)

    # Assertions
    assert len(records) == 3
    
    # Check specific fields for Alice
    alice = next(r for r in records if r['Student'] == 'Alice')
    assert alice['Completed'] == 1
    assert alice['Grade %'] == 85.0
    assert alice['Status'] == 'TURNED_IN'

    # Check Charlie (incomplete)
    charlie = next(r for r in records if r['Student'] == 'Charlie')
    assert charlie['Completed'] == 0
    assert charlie['Score'] is None

def test_process_submission_batch_item_missing_student():
    response = {'studentSubmissions': [{'userId': 'unknown', 'state': 'TURNED_IN', 'assignedGrade': 10}]}
    meta = {'title': 'Quiz', 'maxPoints': 20}
    roster = {} # Empty roster

    records = DataProcessor.process_submission_batch_item(response, meta, roster)

    assert records[0]['Student'] == 'External Student'
    assert records[0]['Grade %'] == 50.0