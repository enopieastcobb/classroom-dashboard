"""
Checks whether each student was handed the booklets they are due this session.

The failure this exists to catch: a teacher gets busy and forgets to hand over
the next booklet. Nobody notices until the child is home, and by then the whole
week is lost -- and a parent who spots it before we do loses confidence in how
the centre is run. So the check runs during the session, while it can still be
put right.

Curriculum shape:
  * levels 1-24; within a level, booklets 1-18 are Basic Thinking Maths (BTM)
    and 19-30 are Critical Thinking Maths (CTM)
  * a week's issue is TWO BTM booklets and ONE CTM booklet
  * an open FIC in a series blocks the NEXT booklet in that series only -- the
    other series carries on
  * finishing a level's BTM run does not roll straight into the next level: a
    level test sits in between and must be passed (>= 80) first
"""
import re
from datetime import date
from typing import Any, Dict, List, Optional

BTM_FIRST, BTM_LAST = 1, 18
CTM_FIRST, CTM_LAST = 19, 30
BTM_PER_WEEK, CTM_PER_WEEK = 2, 1
LEVEL_TEST_PASS = 80

# Only Classwork and Homework carry booklet work.
#
# Note these are matched against the item's topic AFTER the subject has been
# stripped from it: Classroom's "Maths Classwork" arrives here as subject
# "Math" + topic "Classwork". Matching the full Classroom topic name would
# never hit, which is exactly why no booklets were being found at all.
BOOKLET_TOPICS = ('classwork', 'homework')

# "8-25", "8 - 25", "L8-25", "16-17 hc.", "CTM 8-25".
#
# ANCHORED TO THE START of the title, which is what separates a booklet from
# another series that merely contains a number pair. Every real booklet leads
# with its number; "Geometry 1-3" and "Algebra 1 Unit 7 part 8: Algebra 10-2"
# do not, and reading those as booklets 1-3 and 10-2 raised handover alerts
# for booklets that don't exist in that student's curriculum.
BOOKLET_RE = re.compile(
    r'^\s*(?:(?P<series>BTM|CTM)\s*)?L?(?P<level>\d{1,2})\s*-\s*'
    r'(?P<book>\d{1,2})(?![\d-])', re.IGNORECASE)
FIC_RE = re.compile(r'\bfic\b', re.IGNORECASE)
# "Level 12 Critical Test", "Level 15 BT", "Level E Test"
LEVEL_TEST_RE = re.compile(r'\blevel\s*(?P<level>\d{1,2})\b.*\btest\b', re.IGNORECASE)


def _norm_topic(topic: str) -> str:
    return re.sub(r'\s+', ' ', (topic or '')).strip().lower()


def parse_booklet(title: str) -> Optional[Dict[str, Any]]:
    """A booklet reference, or None when the title isn't one."""
    m = BOOKLET_RE.search(title or '')
    if not m:
        return None
    level, book = int(m.group('level')), int(m.group('book'))
    if not (1 <= level <= 24 and BTM_FIRST <= book <= CTM_LAST):
        return None
    stated = (m.group('series') or '').upper()
    return {
        'level': level,
        'book': book,
        'series': stated or ('BTM' if book <= BTM_LAST else 'CTM'),
    }


def next_booklet(level: int, book: int, series: str):
    """The booklet that follows, rolling into the next level at the end."""
    last = BTM_LAST if series == 'BTM' else CTM_LAST
    first = BTM_FIRST if series == 'BTM' else CTM_FIRST
    return (level, book + 1) if book < last else (level + 1, first)


def _label(level: int, book: int) -> str:
    return f"{level}-{book}"


def review_student(items: List[Dict[str, Any]], today: Optional[date] = None) -> Dict[str, Any]:
    """
    Works out what this student should have been handed today, and what they
    actually were.

    `items` are the built assignment items for one student. Each needs: title,
    topic, status, turned_in, posted_key, score_percent.
    """
    today = today or date.today()
    today_key = today.isoformat()

    booklets, level_tests = [], []
    for i in items:
        # Maths only, and only the two topics that carry booklets. Subject is
        # checked explicitly because `items` holds both rooms for the student.
        if i.get('subject') != 'Math':
            continue
        if _norm_topic(i.get('topic')) not in BOOKLET_TOPICS:
            continue
        title = i.get('title') or ''
        b = parse_booklet(title)
        if b:
            booklets.append({
                **b, 'title': title,
                'is_fic': bool(FIC_RE.search(title)),
                # A FIC still owed: not handed back to the grader.
                'open': i.get('status') == 'fic' and not i.get('turned_in'),
                'posted_key': i.get('posted_key') or '',
            })
            continue
        t = LEVEL_TEST_RE.search(title)
        if t:
            level_tests.append({
                'level': int(t.group('level')), 'title': title,
                'score': i.get('score_percent'),
                'posted_key': i.get('posted_key') or '',
            })

    findings = []
    for series, per_week in (('BTM', BTM_PER_WEEK), ('CTM', CTM_PER_WEEK)):
        in_series = [b for b in booklets if b['series'] == series]
        if not in_series:
            continue

        open_fics = [b for b in in_series if b['is_fic'] and b['open']]
        if open_fics:
            findings.append({
                'series': series, 'kind': 'blocked', 'expected': [],
                'detail': ', '.join(b['title'] for b in open_fics),
            })
            continue

        issued_today = [b for b in in_series if b['posted_key'] == today_key]
        shortfall = per_week - len(issued_today)
        if shortfall <= 0:
            continue

        # Walk forward from the furthest booklet the student has reached.
        furthest = max(in_series, key=lambda b: (b['level'], b['book']))
        level, book = furthest['level'], furthest['book']

        expected = []
        for _ in range(shortfall):
            level, book = next_booklet(level, book, series)
            # Crossing into a new level needs the level test passed first.
            if book == (BTM_FIRST if series == 'BTM' else CTM_FIRST) and level > furthest['level']:
                gate = _level_gate(level_tests, furthest['level'])
                if gate:
                    findings.append({'series': series, 'kind': gate['kind'],
                                     'expected': [], 'detail': gate['detail']})
                    expected = []
                    break
            expected.append(_label(level, book))

        if expected:
            findings.append({'series': series, 'kind': 'missing',
                             'expected': expected, 'detail': ''})

    return {
        'findings': findings,
        'missing': [b for f in findings if f['kind'] == 'missing' for b in f['expected']],
    }


def _level_gate(level_tests, finished_level) -> Optional[Dict[str, str]]:
    """Whether the level test for `finished_level` clears the way to the next."""
    taken = [t for t in level_tests if t['level'] == finished_level]
    if not taken:
        return {'kind': 'needs_level_test',
                'detail': f"level {finished_level} test not given yet"}
    best = max((t['score'] for t in taken if t['score'] is not None), default=None)
    if best is None:
        return {'kind': 'level_test_ungraded',
                'detail': f"level {finished_level} test not graded yet"}
    if best < LEVEL_TEST_PASS:
        return {'kind': 'level_test_failed',
                'detail': f"level {finished_level} test scored {best}%, "
                          f"under the {LEVEL_TEST_PASS}% pass mark"}
    return None
