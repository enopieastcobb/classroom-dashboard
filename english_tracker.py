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
# A repeat of a booklet is normally an error; these two say it was intended.
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

    Maths by default. English only when the grade agrees AND the student's
    English history independently reaches that far -- requiring both keeps a
    grade-3 child who is simply behind in maths from being misread.
    """
    if level not in ('6', '7', '8'):
        return True
    return english_trace and grade is not None and 3 <= grade <= 4
