"""
Checks whether each student was handed the English booklets they are due.

Companion to booklet_tracker.py, deliberately NOT part of it. The two curricula
disagree on nearly everything -- level identifiers, booklets per level, the
weekly count, whether the level test gates progression, and which direction the
grade cut-off runs -- so sharing one code path would mean a rule change in one
room quietly altering the other.

Full rules, and where each came from: english_booklet_spec.md

Shape of the English curriculum:
  * READING: twelve levels A B C D E F G H I 6 7 8, thirty booklets each, ONE a
    week. Booklet 30 rolls into booklet 1 of the next level.
  * WRITING: a second series, grade 1 and below only. One booklet lasts a whole
    reading level (A-E) or 6-8 weeks (F+), so it is NOT a weekly handover -- the
    check is only that one exists, at an allowed level.
  * The level test falls due at booklet 28 and RUNS ACROSS the level boundary,
    tolerated through booklet 2 of the next level. Unlike maths it does not gate
    the move up.
  * No supplementary packets.
"""
import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The reading ladder, in order. Levels 6, 7 and 8 are genuinely digits: the same
# tokens are also maths levels, which is the one real collision between the two
# rooms (see resolve_ambiguous_level).
READING_LEVELS: Tuple[str, ...] = ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I',
                                   '6', '7', '8')
BOOKS_PER_LEVEL = 30
READING_PER_WEEK = 1

# The writing ladder runs further than anyone reaches: writing stops after grade
# 1, by which point a child is on E or F. Accepting the full range costs nothing
# -- a booklet that exists parses, one that does not never appears -- and no
# "next writing booklet" is ever computed, so the per-level counts do not matter.
WRITING_LEVELS: Tuple[str, ...] = tuple('ABCDEFGHIJKLM')
WRITING_MAX_GRADE = 1

# Reading booklets are checked to grade 4. Above that nothing is demanded, but
# booklet activity is still reported -- a child past the curriculum who is
# somehow still on booklets is worth surfacing. That is the REVERSE of maths,
# where grade 6+ goes completely silent.
READING_MAX_GRADE = 4

LEVEL_TEST_DUE_AT_BOOK = 28
# 28, 29, 30, then booklets 1 and 2 of the next level: five booklets in which a
# 90-minute test can be finished across sessions.
TEST_TOLERANCE_INTO_NEXT = 2
LEVEL_TEST_PASS = 80

# A missing test is reported from booklet 28 onwards -- no test at 28 means the
# child was never started on it in time, which is the miss itself rather than
# something that only becomes wrong later.
#
# It stops being reported ten booklets past that point. A level takes a child
# seven or eight months, so anything further back is last year's problem: nobody
# can act on it, and old tests do not survive in Classroom anyway, so absence
# stops being evidence. Measured from booklet 28 of the level the test belongs
# to, which puts the last booklet still worth mentioning at 8 of the next level.
TEST_LOOKBACK_BOOKLETS = 10

# How far back a duplicated booklet is worth reporting. Classroom keeps every
# assignment ever made, so without a bound the check reaches into levels a child
# left years ago -- Spoorthi, working through level G, was flagged for B-25 and
# C-11. Ten weeks is roughly ten booklets at one a week, matching the reach of
# the level-test check above.
DUPLICATE_LOOKBACK_WEEKS = 10

BOOKLET_TOPICS = ('classwork', 'homework')
GRADED_TOPIC_RE = re.compile(r'graded', re.IGNORECASE)
GRADE_RE = re.compile(r'\bGr(?:ade)?\s*(?P<grade>\d{1,2})', re.IGNORECASE)

_LEVEL_ALT = '|'.join(READING_LEVELS)
_WLEVEL_ALT = '|'.join(WRITING_LEVELS)

# --- recognition -----------------------------------------------------------
#
# Going forward every booklet is prefixed, which makes it unambiguous. The
# unprefixed patterns exist only for work issued before the convention and
# cannot be retired, since nothing can be renamed retroactively.

# "ENG I-23 HC", "ENG H - 4 HC FIC", "ENG H--1 HC FIC", "ENG D-13 hc"
READING_RE = re.compile(
    rf'\bENG\s*(?P<level>{_LEVEL_ALT})\s*-+\s*(?P<book>\d{{1,2}})(?!\d)(?!-\d)',
    re.IGNORECASE)

# "ESSAYWriting: F-1 HC". A title under this prefix with no level is prompt work
# for grade 2+, not a booklet, and simply does not match.
WRITING_RE = re.compile(
    rf'\bESSAY\s*WRITING\s*:?\s*(?P<level>{_WLEVEL_ALT})\s*-+\s*'
    rf'(?P<book>\d{{1,2}})(?![\d-])', re.IGNORECASE)

# Legacy reading: "F28 HC", "H-5 HC", "G28-HC", "G 24 HC", "G - 24HC".
#
# HC is REQUIRED here and is what makes the pattern safe: it is always present on
# a booklet, and without it "F1 Essay Writing Aug Sept - 2026" reads as level F
# booklet 1. The level must also lead the title, which is what separates a
# booklet from "Classic series A11-4" and "New Series A1-2".
# The HC lookahead tolerates it being welded to the number ("G - 24HC") and
# preceded by a hyphen ("G28-HC"), but not buried inside a word, so a stray "hc"
# in ordinary prose cannot promote a title to a booklet. A title naming Essay
# Writing is excluded outright: that is the writing series, never a reading one,
# and it is the surer signal of the two.
LEGACY_READING_RE = re.compile(
    rf'^\s*(?P<level>{_LEVEL_ALT})\s*-?\s*(?P<book>\d{{1,2}})(?!\d)(?!-\d)'
    rf'(?!.*\bessay\s*writing\b)'
    rf'(?=.*(?<![A-Za-z])HC\b)', re.IGNORECASE)

# Legacy writing: "F1 Essay Writing Aug Sept - 2026". These carry NO HC, so they
# are recognised by the phrase instead. "Essay Writing Aug-Sept 2026" has no
# leading level and so is correctly not a booklet.
LEGACY_WRITING_RE = re.compile(
    rf'^\s*(?P<level>{_WLEVEL_ALT})\s*-?\s*(?P<book>\d{{1,2}})(?!\d)(?!-\d)'
    rf'(?=.*\bessay\s*writing\b)', re.IGNORECASE)

# "TEST: ENG Level I" going forward; "ENG Level F Test", "Level G Test" behind us.
TEST_RE = re.compile(
    rf'\bTEST\s*:?\s*(?:ENG\s*)?LEVEL\s*(?P<level>{_LEVEL_ALT})\b', re.IGNORECASE)
LEGACY_TEST_RE = re.compile(
    rf'\b(?:ENG\s*)?LEVEL\s*(?P<level>{_LEVEL_ALT})\s*TEST\b', re.IGNORECASE)

FIC_RE = re.compile(r'\bfic\b', re.IGNORECASE)
# A repeat of a booklet is normally an error. These three say it was intended,
# and each marks a different situation for whoever reads the assignment later:
#   REDO     -- failed the level test; the set is reassigned one a week
#   REISSUE   -- the physical book was lost, so a SECOND book was handed over
# Both genuinely produce two records, so both have to be forgiven.
#
# FINISH is deliberately NOT here. A booklet that came back unfinished gets
# another week by editing the row that already exists -- no new book, no new
# record. So a FINISH title appearing twice is a real mistake, and exempting
# it would hide exactly what the check is for.
# Past-tense forms are accepted, since that is how people write.
DELIBERATE_REPEAT_RE = re.compile(
    r'\b(?:REDO(?:NE)?|REISSUE[DS]?)\b', re.IGNORECASE)
REDO_PREFIX_RE = re.compile(r'\bREDO\b', re.IGNORECASE)
REISSUE_RE = re.compile(r'\bREISSUE\b', re.IGNORECASE)
REDO_LIST_RE = re.compile(r'\bredo\b\s*:?\s*(?P<list>[A-I0-9,\s\-]+)', re.IGNORECASE)
ADVANCE_RE = re.compile(r'\badvance\b\s*:?\s*(?P<reason>.*)', re.IGNORECASE)


def _norm_topic(topic: str) -> str:
    return re.sub(r'\s+', ' ', (topic or '')).strip().lower()


def carries_booklets(topic: str) -> bool:
    """Classwork, Homework, To Be Graded, and the shared Graded topic."""
    t = _norm_topic(topic)
    return t in BOOKLET_TOPICS or bool(GRADED_TOPIC_RE.search(t))


def grade_number(section: str) -> Optional[int]:
    """The grade from a class section like "Gr 4 [2026-2027]"."""
    m = GRADE_RE.search(section or '')
    return int(m.group('grade')) if m else None


def level_index(level: str) -> Optional[int]:
    """Position on the reading ladder, or None if not a reading level."""
    level = (level or '').upper()
    return READING_LEVELS.index(level) if level in READING_LEVELS else None


def next_reading_booklet(level: str, book: int) -> Optional[Tuple[str, int]]:
    """
    The booklet that follows, rolling into the next level after 30.

    Returns None at the very end of the ladder -- there is nothing after 8-30,
    and demanding a booklet that does not exist would be worse than silence.
    """
    if book < BOOKS_PER_LEVEL:
        return level.upper(), book + 1
    i = level_index(level)
    if i is None or i + 1 >= len(READING_LEVELS):
        return None
    return READING_LEVELS[i + 1], 1


def parse_reading(title: str) -> Optional[Dict[str, Any]]:
    """A reading booklet, or None. Prefixed form wins over the legacy one."""
    title = title or ''
    m = READING_RE.search(title)
    labelled = bool(m)
    if not m:
        m = LEGACY_READING_RE.search(title)
    if not m:
        return None
    book = int(m.group('book'))
    if not 1 <= book <= BOOKS_PER_LEVEL:
        return None
    return {'series': 'ENG', 'level': m.group('level').upper(), 'book': book,
            'labelled': labelled}


def parse_reading_all(title: str) -> List[Dict[str, Any]]:
    """
    Every reading booklet named in a title, not just the first.

    One assignment can cover several -- "ENG I - 18, I - 19 ,I - 21 FICs
    assigned for hw" is three booklets in one record. Returning all of them is
    what lets the duplicate check see the repeats.
    """
    title = title or ''
    found, seen = [], set()
    for m in READING_RE.finditer(title):
        book = int(m.group('book'))
        key = (m.group('level').upper(), book)
        if 1 <= book <= BOOKS_PER_LEVEL and key not in seen:
            seen.add(key)
            found.append({'series': 'ENG', 'level': key[0], 'book': book,
                          'labelled': True})
    if found:
        # A prefixed title may name further booklets without repeating "ENG":
        # "ENG I - 18, I - 19 ,I - 21". Pick those up relative to the first.
        tail = title[title.upper().find('ENG'):]
        for m in re.finditer(rf'\b(?P<level>{_LEVEL_ALT})\s*-+\s*'
                             rf'(?P<book>\d{{1,2}})(?![\d-])', tail, re.IGNORECASE):
            book = int(m.group('book'))
            key = (m.group('level').upper(), book)
            if 1 <= book <= BOOKS_PER_LEVEL and key not in seen:
                seen.add(key)
                found.append({'series': 'ENG', 'level': key[0], 'book': book,
                              'labelled': True})
        return found
    one = parse_reading(title)
    return [one] if one else []


def parse_writing(title: str) -> Optional[Dict[str, Any]]:
    """A writing booklet, or None. Prompt work carries no level and so is None."""
    title = title or ''
    m = WRITING_RE.search(title) or LEGACY_WRITING_RE.search(title)
    if not m:
        return None
    return {'series': 'EW', 'level': m.group('level').upper(),
            'book': int(m.group('book')), 'labelled': bool(WRITING_RE.search(title))}


def parse_level_test(title: str) -> Optional[str]:
    """The reading level a test belongs to, or None."""
    m = TEST_RE.search(title or '') or LEGACY_TEST_RE.search(title or '')
    return m.group('level').upper() if m else None


def parse_redo_list(*sources: str) -> List[Tuple[str, int]]:
    """
    Booklets a grader sent back, from the first source carrying a REDO marker.

    Description first, then title: a grader who has written it properly is never
    second-guessed by an older title.
    """
    for text in sources:
        m = REDO_LIST_RE.search(text or '')
        if not m:
            continue
        found, seen = [], set()
        for lvl, book in re.findall(rf'({_LEVEL_ALT})\s*-\s*(\d{{1,2}})',
                                    m.group('list'), re.IGNORECASE):
            key = (lvl.upper(), int(book))
            if key not in seen:
                seen.add(key)
                found.append(key)
        if found:
            return found
    return []


def parse_advance(*sources: str) -> Optional[str]:
    """Why a child was moved on past a failed test, or None."""
    for text in sources:
        m = ADVANCE_RE.search(text or '')
        if m:
            return m.group('reason').strip(' .-') or "no reason recorded"
    return None


def resolve_ambiguous_level(level: str, grade: Optional[int],
                            english_trace: bool) -> bool:
    """
    Whether an UNPREFIXED booklet at level 6, 7 or 8 belongs to English.

    Those three tokens are levels in both curricula, and the shared Graded topic
    names no subject, so the title alone cannot decide. The level number carries
    an age signal instead, and it points in opposite directions: English 6-8 sit
    at the TOP of a twelve-level ladder, so only a grade 3-4 child reaches them,
    while maths 6-8 sit EARLY in a twenty-four-level ladder and belong to a child
    below grade 3.

    Maths, always. The grade signal is real but not strong enough on its own:
    Shrey Patel was read as being at English 8-30, a position no child in the
    centre holds, because his maths booklets at these levels were pulled across
    on a grade-3 match. A wrong position is worse than a missing one -- it names
    the wrong next booklet and invents a missing level test.

    An ENG prefix settles it, so nothing issued under the naming standard is
    affected. The cost falls only on a child genuinely at English 6-8 whose OLD
    booklets were never labelled: their history reads short. That is the safer
    direction to be wrong in.
    """
    return level not in ('6', '7', '8')


def _position(reading: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    The furthest reading booklet reached.

    Plain highest-wins, unlike maths: the English ladder has no swapped pair, so
    levels are always taken in order.
    """
    return max(reading, key=lambda b: (level_index(b['level']) or 0, b['book']))


def _collect(items: List[Dict[str, Any]], grade: Optional[int]):
    """
    Sort the student's work into reading booklets, writing booklets and tests.

    Two passes, because of one collision. Levels 6, 7 and 8 belong to both
    curricula and the shared Graded topic names no subject, so an unprefixed
    booklet there could be either room. Unambiguous work is gathered first and
    used as the evidence for deciding the rest.
    """
    reading, writing, tests, maybe = [], [], [], []
    for i in items:
        topic = i.get('topic')
        if not carries_booklets(topic):
            continue
        # Only a topic that NAMES English settles the room. In the shared
        # Graded topic the subject is guessed from the title and defaults to
        # English, so trusting it there is what let maths levels 6, 7 and 8
        # through as English booklets.
        subject_known = (i.get('subject') == 'English'
                         and bool(i.get('subject_stated')))
        shared_graded = bool(GRADED_TOPIC_RE.search(_norm_topic(topic)))
        if not subject_known and not shared_graded:
            continue
        title = i.get('title') or ''

        lvl = parse_level_test(title)
        if lvl:
            tests.append({
                'level': lvl, 'title': title,
                'score': i.get('score_percent'),
                'graded': i.get('status') == 'done',
                # The child has handed it in; only the marking is outstanding.
                'submitted': (i.get('status') == 'submitted'
                              or bool(i.get('turned_in'))),
                'posted_key': i.get('posted_key') or '',
                'redo': parse_redo_list(i.get('given') or '', title),
                'advance': parse_advance(i.get('given') or '', title),
            })
            continue

        w = parse_writing(title)
        if w:
            writing.append({**w, 'title': title})
            continue

        for b in parse_reading_all(title):
            rec = {
                **b, 'title': title,
                'is_fic': bool(FIC_RE.search(title)),
                'open': i.get('status') == 'fic' and not i.get('turned_in'),
                'outstanding': (i.get('status') in ('notdone', 'fic')
                                and not i.get('turned_in')),
                'exempt': bool(DELIBERATE_REPEAT_RE.search(title)),
                'posted_key': i.get('posted_key') or '',
            }
            # Undecidable only when unlabelled, at 6/7/8, and with no subject on
            # the topic to settle it.
            if (not b['labelled'] and b['level'] in ('6', '7', '8')
                    and not subject_known):
                maybe.append(rec)
            else:
                reading.append(rec)

    # English 6-8 sit at the top of the ladder, so a child who has genuinely
    # reached them has unambiguous history at level I or beyond.
    trace = any((level_index(b['level']) or 0) >= (level_index('I') or 0)
                for b in reading)
    reading.extend(b for b in maybe
                   if resolve_ambiguous_level(b['level'], grade, trace))
    return reading, writing, tests


def _duplicates(reading: List[Dict[str, Any]], tests: List[Dict[str, Any]],
                today=None) -> List[Dict[str, Any]]:
    """
    Booklets issued more than once without a reason.

    A repeat is normally an error, but redo booklets are deliberately reassigned
    week after week, so anything carrying REDO or REISSUE, or named on its own
    level's redo list, is exempt. Without that exemption every correctly-handled
    failed test would accuse the teacher who did the right thing.

    Only repeats the child is STILL HOLDING count. A booklet issued twice years
    ago and finished both times is not something anyone can act on now -- and
    Classroom keeps every assignment ever made, so judging all of history buried
    the real findings under levels the child left behind. Spoorthi, working
    through level G, was flagged for B-25, B-29 and C-11. What matters is a
    booklet in the child's hands today that they have already done.

    Bounded in time as well: a copy has to have been issued within the last ten
    weeks. The two conditions catch different halves of the same mistake -- one
    that the child is holding it, one that it happened recently enough to act on.
    """
    cutoff = ''
    if today is not None:
        cutoff = (today - timedelta(weeks=DUPLICATE_LOOKBACK_WEEKS)).isoformat()
    redo_set = {(lvl, bk) for t in tests for lvl, bk in t['redo']}
    counts: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for b in reading:
        counts.setdefault((b['level'], b['book']), []).append(b)

    out = []
    for (lvl, book), group in sorted(counts.items()):
        if len(group) < 2:
            continue
        if (lvl, book) in redo_set or any(b['exempt'] for b in group):
            continue
        if not any(b['outstanding'] for b in group):
            continue
        # ISO dates compare correctly as strings. A booklet with no posted date
        # is kept rather than dropped -- absence of a date is not evidence of age.
        if cutoff and not any(not b['posted_key'] or b['posted_key'] >= cutoff
                              for b in group):
            continue
        out.append({'series': 'ENG', 'kind': 'duplicate', 'expected': [],
                    'severe': True,
                    'detail': f"{lvl}-{book} assigned {len(group)} times"})
    return out


def _test_state(reading, tests, pos) -> Optional[Dict[str, Any]]:
    """
    Whether the level test is where it should be.

    It falls due at booklet 28 and may run across the level boundary, tolerated
    through booklet 2 of the next level -- ninety minutes does not fit in one
    session. Past that it is prominent: the child is a whole level on with an
    unfinished test behind them.
    """
    i = level_index(pos['level'])
    if i is None:
        return None

    def finished(level: str) -> Optional[Dict[str, Any]]:
        for t in tests:
            if t['level'] == level and t['graded']:
                return t
        return None

    def exists(level: str) -> bool:
        return any(t['level'] == level for t in tests)

    # The level just finished: its test should have been started at booklet 28
    # and finished by booklet 2 of this one. Booklets 29 and 30 of that level,
    # plus however far into this one the child has come, is how stale the
    # question has become.
    if i > 0 and (2 + pos['book']) <= TEST_LOOKBACK_BOOKLETS:
        prev = READING_LEVELS[i - 1]
        if not exists(prev):
            return {'series': 'ENG', 'kind': 'test_missing', 'expected': [],
                    'severe': True,
                    'detail': f"level {prev} test missing -- child is already "
                              f"on {pos['level']}-{pos['book']}"}
        sat = next((t for t in tests if t['level'] == prev), None)
        if sat and not sat['graded']:
            # Handed in but unmarked is a different ask from a different person:
            # the child has done their part and the follow-through is the
            # teacher's, so it says so rather than reading as a child stuck
            # mid-test.
            if sat['submitted']:
                return {'series': 'ENG', 'kind': 'test_ungraded', 'expected': [],
                        'severe': False,
                        'detail': f"level {prev} test NOT GRADED YET"}
            if pos['book'] > TEST_TOLERANCE_INTO_NEXT:
                return {'series': 'ENG', 'kind': 'test_overdue', 'expected': [],
                        'severe': True,
                        'detail': f"level {prev} test still unfinished -- child "
                                  f"is already on {pos['level']}-{pos['book']}"}

    # The current level: the test should be in place from booklet 28.
    if pos['book'] >= LEVEL_TEST_DUE_AT_BOOK:
        if not exists(pos['level']):
            return {'series': 'ENG', 'kind': 'test_missing', 'expected': [],
                    'severe': True,
                    'detail': f"level {pos['level']} test missing -- child is at "
                              f"booklet {pos['book']}, it should have started at "
                              f"{LEVEL_TEST_DUE_AT_BOOK}"}
        here = next((t for t in tests if t['level'] == pos['level']), None)
        if here and not here['graded'] and here['submitted']:
            return {'series': 'ENG', 'kind': 'test_ungraded', 'expected': [],
                    'severe': False,
                    'detail': f"level {pos['level']} test NOT GRADED YET"}

    # Sat and failed, with no decision recorded either way.
    for t in tests:
        if (t['graded'] and t['score'] is not None
                and t['score'] < LEVEL_TEST_PASS
                and not t['redo'] and not t['advance']):
            return {'series': 'ENG', 'kind': 'test_failed_no_decision',
                    'expected': [], 'severe': False,
                    'detail': f"level {t['level']} test scored {t['score']}% "
                              f"with no REDO or ADVANCE recorded"}
    return None


def _writing_state(reading, writing, grade) -> Optional[Dict[str, Any]]:
    """
    Whether a writing booklet is in hand, for the children who get them.

    Never a weekly check: one writing booklet covers a whole reading level for
    A-E, or six to eight weeks from F. So the only question is whether one
    exists, at a level matching the reading level or the one above it.
    """
    if grade is None or grade > WRITING_MAX_GRADE:
        return None
    if not reading:
        return None
    pos = _position(reading)
    if pos['level'] not in WRITING_LEVELS:
        return None
    if not writing:
        return {'series': 'EW', 'kind': 'writing_missing', 'expected': [],
                'severe': False,
                'detail': "no essay writing booklet assigned"}
    wi = WRITING_LEVELS.index(pos['level'])
    allowed = {WRITING_LEVELS[wi]}
    if wi + 1 < len(WRITING_LEVELS):
        allowed.add(WRITING_LEVELS[wi + 1])
    # The pairing loosens at grade 1: a child who has reached first grade is
    # moved to E or F writing whatever their reading level. Aadhya P is on
    # reading D with writing F-1, which the general rule alone would flag.
    if grade == 1:
        allowed |= {'E', 'F'}
    if not any(w['level'] in allowed for w in writing):
        best = max(writing, key=lambda w: WRITING_LEVELS.index(w['level']))
        return {'series': 'EW', 'kind': 'writing_level', 'expected': [],
                'severe': False,
                'detail': f"essay writing booklet is {best['level']}-"
                          f"{best['book']}, expected "
                          f"{' or '.join(sorted(allowed))} for reading level "
                          f"{pos['level']}"}
    return None


def review_student(items: List[Dict[str, Any]], today=None,
                   section: str = "") -> Dict[str, Any]:
    """
    What this student should have been handed, and what they were.

    Returns findings (everything, for the log), missing (booklets to name in the
    banner), notes (everything else worth a line) and severe (the subset that
    needs to stand out).
    """
    grade = grade_number(section)
    reading, writing, tests = _collect(items, grade)

    findings: List[Dict[str, Any]] = []

    # Past the curriculum but still on booklets. The reverse of maths, where
    # grade 6+ goes silent: here it is the anomaly that is worth saying, because
    # a child this old should have been moved off booklets.
    if grade is not None and grade > READING_MAX_GRADE:
        if reading:
            findings.append({
                'series': 'ENG', 'kind': 'past_curriculum', 'expected': [],
                'severe': False,
                'detail': f"grade {grade} and still on English booklets"})
        return _result(findings)

    findings.extend(_duplicates(reading, tests, today))

    if reading:
        pos = _position(reading)
        state = _test_state(reading, tests, pos)
        if state:
            findings.append(state)

        # The count comes first, as in maths: an open FIC is itself a booklet in
        # hand, so a child can be blocked and still hold the week's booklet.
        # Counted by booklet, not by record -- see the maths tracker. A reissued
        # book has two rows and must still count once.
        holding = {(b['level'], b['book']) for b in reading if b['outstanding']}
        if len(holding) < READING_PER_WEEK:
            open_fics = [b for b in reading if b['is_fic'] and b['open']]
            redo = [(l, b) for t in tests for l, b in t['redo']
                    if not any(r['level'] == l and r['book'] == b
                               and not r['outstanding'] for r in reading)]
            if open_fics:
                findings.append({
                    'series': 'ENG', 'kind': 'blocked', 'expected': [],
                    'severe': False,
                    'detail': "reading on hold until "
                              + ', '.join(b['title'] for b in open_fics)
                              + " is fixed"})
            elif redo:
                lvl, book = redo[0]
                findings.append({'series': 'ENG', 'kind': 'redo', 'severe': False,
                                 'expected': [f"{lvl}-{book}"],
                                 'detail': "from the redo set"})
            else:
                nxt = next_reading_booklet(pos['level'], pos['book'])
                if nxt:
                    findings.append({'series': 'ENG', 'kind': 'missing',
                                     'severe': False, 'detail': '',
                                     'expected': [f"{nxt[0]}-{nxt[1]}"]})
    elif grade is not None:
        findings.append({'series': 'ENG', 'kind': 'no_booklets', 'expected': [],
                         'severe': False,
                         'detail': f"grade {grade} but no English booklet work found"})

    w = _writing_state(reading, writing, grade)
    if w:
        findings.append(w)

    return _result(findings)


def _result(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    seen, deduped = set(), []
    for f in findings:
        key = (f['kind'], f['detail'], tuple(f['expected']))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    return {
        'findings': deduped,
        'missing': [b for f in deduped if f['kind'] in ('missing', 'redo')
                    for b in f['expected']],
        'notes': [f['detail'] for f in deduped
                  if f['detail'] and f['kind'] not in ('missing', 'redo')],
        'severe': [f['detail'] for f in deduped if f.get('severe')],
    }
