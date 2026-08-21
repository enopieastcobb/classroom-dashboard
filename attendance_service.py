"""
Attendance marking, stored in Firestore.

Chosen for cost: the free tier allows 20,000 writes and 50,000 reads a day, and
the centre marks roughly sixty children. A year of attendance is a few megabytes
against a 1 GiB free allowance, so this stays free rather than merely cheap.
Cloud SQL would have been ten to twenty-five dollars a month whether anyone
opened it or not.

Only PRESENCE is recorded. There is no absent flag: a child who was scheduled
and has no record was not there. That keeps a tap meaning exactly one thing, and
it means the admin walks the rooms tapping only the children in front of them.

The document id is DETERMINISTIC -- "2026-08-20_nathan-w" -- so the same child on
the same day is always the same record. Tapping twice, or two devices marking at
once, cannot produce two rows. That matters more than it sounds: duplicate
records have been the most persistent source of wrong answers in this codebase.
"""
import logging
import re
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

COLLECTION = "attendance"

_client = None
_client_lock = threading.Lock()


def slug(name: str) -> str:
    """A stable id fragment for a student's name."""
    return re.sub(r'[^a-z0-9]+', '-', (name or '').strip().lower()).strip('-')


def doc_id(day_iso: str, student: str) -> str:
    return f"{day_iso}_{slug(student)}"


def _db():
    """
    The Firestore client, built once.

    Imported lazily so the module can be loaded -- and its pure functions
    tested -- on a machine with no credentials and no google-cloud-firestore.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from google.cloud import firestore
                _client = firestore.Client()
    return _client


def mark_present(day_iso: str, student: str, *, session_day: str = '',
                 subject: str = '', time: str = '', teacher: str = '',
                 marked_by: str = '') -> None:
    """
    Record that a student was present. Idempotent: marking twice is one record.

    The session is stored alongside the name because the schedule Sheet is
    edited -- children move days and hours -- so the roster a month from now
    will not be the roster that applied today. Keeping the room, hour and
    teacher on the record means the history still says where the child actually
    was.
    """
    _db().collection(COLLECTION).document(doc_id(day_iso, student)).set({
        'student': student,
        'date': day_iso,
        'day': session_day,
        'subject': subject,
        'time': time,
        'teacher': teacher,
        'marked_by': marked_by,
        'marked_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
    })


def clear(day_iso: str, student: str) -> None:
    """Undo a mark. Needed because a tap on a tablet is easy to make by mistake."""
    _db().collection(COLLECTION).document(doc_id(day_iso, student)).delete()


def present_on(day_iso: str) -> Set[str]:
    """
    Every student marked present on a date, as name slugs.

    The whole day in one query rather than one per session: it is a single
    read of a few dozen small documents, and it lets the page show what has
    already been marked in the other hours as the admin walks through them.
    """
    try:
        docs = (_db().collection(COLLECTION)
                .where('date', '==', day_iso).stream())
        return {slug(d.get('student') or '') for d in docs}
    except Exception as e:
        logger.error("Could not read attendance for %s: %s", day_iso, e)
        raise


def records_on(day_iso: str) -> List[Dict[str, Any]]:
    """The full records for a date, for a register or an export."""
    docs = (_db().collection(COLLECTION)
            .where('date', '==', day_iso).stream())
    return sorted((d.to_dict() for d in docs),
                  key=lambda r: (r.get('time') or '', r.get('student') or ''))


def records_between(from_iso: str, to_iso: str) -> List[Dict[str, Any]]:
    """
    Every record in a date range, inclusive.

    A range query on one field needs no composite index, and the volume is
    small -- sixty children over a term is a few thousand documents, well
    inside the free read allowance.
    """
    docs = (_db().collection(COLLECTION)
            .where('date', '>=', from_iso)
            .where('date', '<=', to_iso).stream())
    return sorted((d.to_dict() for d in docs),
                  key=lambda r: (r.get('date') or '', r.get('time') or '',
                                 r.get('student') or ''))


def summarise(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Per-child totals for a range: how many sessions attended, and which dates.

    Counts ATTENDANCE only, never absence. Absence would need the roster as it
    stood on each past date, and the schedule Sheet is edited continually --
    children move days and hours -- so a count of "missed" computed against
    today's roster would be quietly wrong about the past. Reporting what was
    recorded keeps every number defensible.
    """
    by_student: Dict[str, Dict[str, Any]] = {}
    for r in records:
        name = (r.get('student') or '').strip()
        if not name:
            continue
        row = by_student.setdefault(name, {'student': name, 'dates': [],
                                           'rooms': set()})
        d = r.get('date') or ''
        if d and d not in row['dates']:
            row['dates'].append(d)
        if r.get('subject'):
            row['rooms'].add(r['subject'])
    out = []
    for row in by_student.values():
        row['dates'].sort()
        out.append({'student': row['student'], 'attended': len(row['dates']),
                    'first': row['dates'][0] if row['dates'] else '',
                    'last': row['dates'][-1] if row['dates'] else '',
                    'rooms': ', '.join(sorted(row['rooms'])),
                    'dates': row['dates']})
    return sorted(out, key=lambda r: (-r['attended'], r['student']))
