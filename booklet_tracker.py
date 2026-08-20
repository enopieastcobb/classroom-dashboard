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
import logging
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BTM_FIRST, BTM_LAST = 1, 18
CTM_FIRST, CTM_LAST = 19, 30
BTM_PER_WEEK, CTM_PER_WEEK = 2, 1
LEVEL_TEST_PASS = 80

# Active booklet work lives in Classwork and Homework.
#
# Note these are matched against the item's topic AFTER the subject has been
# stripped from it: Classroom's "Maths Classwork" arrives here as subject
# "Math" + topic "Classwork". Matching the full Classroom topic name would
# never hit, which is exactly why no booklets were being found at all.
BOOKLET_TOPICS = ('classwork', 'homework')

# Finished booklets are moved into a Graded topic, so the history has to be
# read from there too. Without it a student who has completed everything looks
# like one who was never given anything, and there is no way to tell which
# booklet they are up to. These never count as outstanding -- their status is
# already "done" -- so they set the position and nothing more.
GRADED_TOPIC_RE = re.compile(r'graded', re.IGNORECASE)


def _carries_booklets(topic: str) -> bool:
    t = _norm_topic(topic)
    return t in BOOKLET_TOPICS or bool(GRADED_TOPIC_RE.search(t))

# A booklet is recognised two ways, tried in this order.
#
# 1. An explicit BTM/CTM label -- "BTM 19-5", "CTM 8-25", "BTM 17-6 HC". This
#    is authoritative and may sit anywhere in the title, so labelling removes
#    all ambiguity: no other series can be mistaken for a booklet.
LABELLED_RE = re.compile(
    r'\b(?P<series>BTM|CTM)\s*L?(?P<level>\d{1,2})\s*-\s*(?P<book>\d{1,2})(?![\d-])',
    re.IGNORECASE)
# 2. A bare number pair, which must LEAD the title. That is what separates a
#    booklet from another series that merely contains a number pair: every
#    unlabelled booklet leads with its number, while "Geometry 1-3" and
#    "Algebra 1 Unit 7 part 8: Algebra 10-2" do not. Reading those as booklets
#    raised alerts for booklets that don't exist in the student's curriculum.
BARE_RE = re.compile(
    r'^\s*L?(?P<level>\d{1,2})\s*-\s*(?P<book>\d{1,2})(?![\d-])', re.IGNORECASE)
FIC_RE = re.compile(r'\bfic\b', re.IGNORECASE)
# "Level 12 Critical Test", "Level 15 BT", "Level E Test"
LEVEL_TEST_RE = re.compile(r'\blevel\s*(?P<level>\d{1,2})\b.*\btest\b', re.IGNORECASE)

# The supplementary packet for a level, which must be finished before the level
# test: the centre's view is that the 18 booklets AND the packet together are
# what make the concept stick. Written variously as "L11 Supplementary Packet.",
# "L16 supplementary packet .", "L13 supplement packet", "L12a Supplementary
# Packet.", "Level 7 Supplementary Packet."
SUPP_RE = re.compile(
    r'\b(?:L|Level)\s*(?P<level>\d{1,2})[a-z]?\b.*\bsupplement', re.IGNORECASE)

# Every child up to the first semester of grade 5 is on the booklet curriculum,
# so one of them showing no booklet work at all is itself worth reporting: it
# means either nothing was ever issued, or the titles aren't in a form the
# check recognises. Silence would look like "all fine".
BOOKLET_EXPECTED_UP_TO_GRADE = 5

# The one pair that may be taken in either order, coming out of level 14.
# See current_position.
SWAPPABLE_LEVELS = frozenset({15, 16})

# Levels 17 and 18 can run as two tracks at once. A child who has passed 15 or
# 16 and is already doing advanced maths at school is put on both to catch up,
# working them side by side rather than one after the other.
#
# This is NOT the 15/16 swap. Those two are taken in either order, one then the
# other; 17 and 18 run TOGETHER and never in reverse -- 18 is never taken first
# and returned from. So this pair stays out of SWAPPABLE_LEVELS: a level-17
# booklet issued after 18 has started means both tracks are live, not that the
# child has dropped back to 17.
#
# That decision rests on how far ahead the child is AT SCHOOL, which is not in
# Classroom. So the pair is DETECTED, never predicted: two tracks are read from
# the booklet history, and a child who should be on two but isn't cannot be
# flagged from here. What the alert catches is a child already running both
# tracks who was handed nothing for one of them.
PARALLEL_LEVELS = (17, 18)

# How far back a duplicated booklet is worth reporting. Classroom keeps every
# assignment ever made, so without a bound the check reaches into levels a child
# left years ago. Ten weeks matches the English rule, where one booklet a week
# makes ten booklets and ten weeks the same span.
DUPLICATE_LOOKBACK_WEEKS = 10

# How much history a student needs before a level test or its packet is expected
# of them -- the same reasoning as the English tracker. A child who joined
# recently arrives mid-level, and the boundary they crossed to get there was
# crossed somewhere else.
HISTORY_REQUIRED_WEEKS = 6

# The same booklet issued twice is an error -- except when it was meant. Three
# words say it was, each marking a different situation for whoever reads the
# assignment later:
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

# Once a child is this far into a level's basic thinking run, the level's
# supplementary packet should already have been issued. Catching it here means
# it is raised while there is a whole level left to work through, rather than
# at booklet 18 when the test is already due and the packet becomes a blocker.
SUPP_EXPECTED_FROM_BOOK = 4

# Booklets a grader has sent back to be redone after a failed level test.
# Written on the LEVEL TEST assignment as "REDO: 12-19, 12-20, 12-22" --
# preferred in the description, where there is room and it doesn't crowd the
# assignment name, with the title accepted as a fallback since existing tests
# already carry it there, e.g.
#   "***CT 12- 45% ( Redo 12-19, 12-20,12-22,12-27)"
# Comments are deliberately NOT a supported place: the Classroom API exposes no
# comment endpoint, so anything written there can never be read back.
REDO_RE = re.compile(r'\bredo\b\s*:?\s*(?P<list>[0-9,\s\-]+)', re.IGNORECASE)

# A deliberate decision to move a child up despite a failed test -- written on
# the level test as "ADVANCE: <reason>". Some children repeat a level several
# times; at some point the frustration costs more than the mastery gains, and
# the centre decides to move on. That is a judgement, not a pass, so the record
# keeps it as such: the level clears, and the reason is carried with it rather
# than the score being quietly treated as a pass.
ADVANCE_RE = re.compile(r'\badvance\b\s*:?\s*(?P<reason>.*)', re.IGNORECASE)


def _has_enough_history(dated, today) -> bool:
    """
    Whether we have seen enough of this student to judge their level tests.

    A level test is only missing if the child was here to be given one. Nathan W
    joined a fortnight ago, arrived already on E-2, and was flagged for a level D
    test that was never ours to set -- his level D work happened somewhere else.

    So a test is looked for only once the record stretches back far enough for a
    level boundary to plausibly have been crossed under our roof. Undated work,
    or none at all, counts as not enough: silence is the safer answer when we
    cannot tell.
    """
    if today is None:
        return True
    dates = [d for d in dated if d]
    if not dates:
        return False
    cutoff = (today - timedelta(weeks=HISTORY_REQUIRED_WEEKS)).isoformat()
    return min(dates) <= cutoff


def parse_advance(*sources: str) -> Optional[str]:
    """
    The reason a child was advanced past a failed test, or None.

    Returns the reason text, or a placeholder when the decision is recorded
    without one -- progression is never blocked for want of an explanation,
    but the missing reason is visible.
    """
    for text in sources:
        m = ADVANCE_RE.search(text or '')
        if m:
            return m.group('reason').strip(' .-') or "no reason recorded"
    return None


def parse_redo_list(*sources: str) -> List[Dict[str, int]]:
    """
    Booklets to redo, from the first source that carries a REDO marker.

    Sources are tried in order -- description first, then title -- so a grader
    who has written it properly is never second-guessed by an older title.
    """
    for text in sources:
        m = REDO_RE.search(text or '')
        if not m:
            continue
        found, seen = [], set()
        for level, book in re.findall(r'(\d{1,2})\s*-\s*(\d{1,2})', m.group('list')):
            level, book = int(level), int(book)
            if not (1 <= level <= 24 and BTM_FIRST <= book <= CTM_LAST):
                continue
            if (level, book) in seen:
                continue
            seen.add((level, book))
            found.append({'level': level, 'book': book,
                          'series': 'BTM' if book <= BTM_LAST else 'CTM'})
        if found:
            return found
    return []
GRADE_RE = re.compile(r'\bGr(?:ade)?\s*(?P<grade>\d{1,2})', re.IGNORECASE)

# Sections below grade 1 are named, not numbered: "Gr K", "Pre-K", and
# "Weenopi" for the three- and four-year-olds. GRADE_RE only reads digits, so
# every one of them came back as an unknown grade -- which silently skipped the
# youngest children, the ones the checks matter most for.
#
# Mapped to integers below 1 so every existing comparison keeps working.
GRADE_K, GRADE_PREK, GRADE_WEENOPI = 0, -1, -2
WEENOPI_RE = re.compile(r'\bwee\s*-?\s*nopi\b', re.IGNORECASE)
PREK_RE = re.compile(r'\bpre\s*-?\s*k\b', re.IGNORECASE)
# Deliberately narrow: a bare "K" appears in too many other places, so this
# wants "Gr K", "Grade K", "KG" or "Kindergarten".
K_RE = re.compile(r'\bGr(?:ade)?\s*K\b|\bKG\b|\bkinder\w*', re.IGNORECASE)



def grade_number(section: str) -> Optional[int]:
    """
    The grade from a class section, or None when it cannot be read.

    Handles the named sections below grade 1 as well as "Gr 4 [2026-2027]".
    Weenopi is checked before Pre-K, and Pre-K before K, since each contains
    the next one's letter.
    """
    section = section or ''
    if WEENOPI_RE.search(section):
        return GRADE_WEENOPI
    if PREK_RE.search(section):
        return GRADE_PREK
    if K_RE.search(section):
        return GRADE_K
    m = GRADE_RE.search(section)
    return int(m.group('grade')) if m else None


def _norm_topic(topic: str) -> str:
    return re.sub(r'\s+', ' ', (topic or '')).strip().lower()


def parse_booklet(title: str) -> Optional[Dict[str, Any]]:
    """A booklet reference, or None when the title isn't one."""
    title = title or ''
    m = LABELLED_RE.search(title) or BARE_RE.search(title)
    if not m:
        return None
    level, book = int(m.group('level')), int(m.group('book'))
    if not (1 <= level <= 24 and BTM_FIRST <= book <= CTM_LAST):
        return None
    stated = (m.groupdict().get('series') or '').upper()
    # A written label wins over the number range. If a grader writes
    # "BTM 17-25" the label is what they meant, and the disagreement is
    # reported so the title can be corrected rather than silently reinterpreted.
    inferred = 'BTM' if book <= BTM_LAST else 'CTM'
    if stated and stated != inferred:
        logger.warning(
            "Booklet %r is labelled %s but booklet %d falls in the %s range; "
            "using the label.", title, stated, book, inferred)
    return {'level': level, 'book': book, 'series': stated or inferred,
            'labelled': bool(stated)}


def current_position(in_series: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    The booklet the student is working from -- normally the furthest they have
    reached, since levels are taken in order.

    ONE transition is allowed out of order. Coming out of level 14, a younger
    child is often given level 16 (order of operations) before level 15 (long
    division), because long division frustrates them and stalling there costs
    more than the sequence is worth. For that pair only, recency decides which
    of the two they are on: ranking by number would read such a child as being
    on 16 when they have since moved back to 15, and ask for booklets they
    already finished. Everywhere else the highest number is the position --
    the curriculum does not jump around.

    Within the chosen level the furthest booklet wins, so a back-filled older
    booklet -- a redo of 9-2 issued to a student already on 9-7 -- is not read
    as a step backwards.
    """
    top = max(b['level'] for b in in_series)
    recent = max(in_series, key=lambda b: (b['posted_key'], b['level']))['level']
    level = recent if {recent, top} <= SWAPPABLE_LEVELS else top
    return max((b for b in in_series if b['level'] == level),
               key=lambda b: b['book'])


def _running_parallel(btm: List[Dict[str, Any]]) -> bool:
    """
    Whether the student is running levels 17 and 18 as two tracks at once.

    Read from the history rather than assumed: level 17 still unfinished while
    18 has already started is something sequential progression cannot produce,
    since 18 only opens once 17 is complete and its test passed. A child who
    finished 17 the ordinary way and moved on to 18 therefore does not trip it.

    BOTH tracks must still be unfinished. Reaching booklet 18 of either one ends
    the run: the child moves to that level's test and on to the next level, and
    whatever is left in the other track is closed rather than carried. Avyuth K
    had finished 18-18 with level 17 stopped at 14 months earlier, and topping
    his week up from the dormant track asked for 17-15 and 17-16 -- booklets
    from a level he had left behind.
    """
    lo, hi = PARALLEL_LEVELS
    levels = {b['level'] for b in btm}
    if not {lo, hi} <= levels:
        return False
    return all(max(b['book'] for b in btm if b['level'] == lv) < BTM_LAST
               for lv in (lo, hi))


def next_booklet(level: int, book: int, series: str, levels_done=()):
    """
    The booklet that follows, rolling into the next level at the end.

    Only the swappable pair is skipped on the way: a child who took 16 before
    15 goes from 15-18 to 17-1, not back through 16. No other level is ever
    skipped -- a gap anywhere else is a mistake to surface, not to route around.
    """
    last = BTM_LAST if series == 'BTM' else CTM_LAST
    first = BTM_FIRST if series == 'BTM' else CTM_FIRST
    if book < last:
        return level, book + 1
    level += 1
    while level in levels_done and level in SWAPPABLE_LEVELS:
        level += 1
    return level, first


def _label(level: int, book: int) -> str:
    return f"{level}-{book}"


def review_student(items: List[Dict[str, Any]], today: Optional[date] = None,
                   section: str = "") -> Dict[str, Any]:
    """
    Works out what this student should have been handed today, and what they
    actually were.

    `items` are the built assignment items for one student. Each needs: title,
    topic, status, turned_in, posted_key, score_percent.
    """
    today = today or date.today()
    today_key = today.isoformat()

    # The booklet curriculum runs to the first semester of grade 5. Above that
    # a child is on Algebra, Geometry and the like, so booklet expectations do
    # not apply and asking for "the next booklet" is meaningless -- it was
    # naming booklets for grade 7 and 8 students who are long past them.
    # An unknown grade still gets checked: missing a young child's booklet is
    # the costlier mistake.
    grade = grade_number(section)
    if grade is not None and grade > BOOKLET_EXPECTED_UP_TO_GRADE:
        return {'findings': [], 'missing': [], 'notes': []}

    booklets, level_tests, supplements = [], [], []
    for i in items:
        if not _carries_booklets(i.get('topic')):
            continue
        # `items` holds both rooms, so English work has to be kept out. The
        # subject check is skipped for Graded topics: those name no subject, so
        # everything in them is inferred from the title and a booklet or packet
        # reads as English. The patterns below are specific enough to stand on
        # their own there.
        if (i.get('subject') != 'Math'
                and not GRADED_TOPIC_RE.search(_norm_topic(i.get('topic')))):
            continue
        title = i.get('title') or ''
        b = parse_booklet(title)
        if b:
            booklets.append({
                **b, 'title': title,
                'is_fic': bool(FIC_RE.search(title)),
                # A FIC still owed: not handed back to the grader.
                'open': i.get('status') == 'fic' and not i.get('turned_in'),
                # The student still has this one in hand -- it counts towards
                # what they are currently holding, whenever it was issued.
                'outstanding': (i.get('status') in ('notdone', 'fic')
                                and not i.get('turned_in')),
                'posted_key': i.get('posted_key') or '',
                'exempt': bool(DELIBERATE_REPEAT_RE.search(title)),
            })
            continue
        t = LEVEL_TEST_RE.search(title)
        if t:
            level_tests.append({
                'level': int(t.group('level')), 'title': title,
                'score': i.get('score_percent'),
                'posted_key': i.get('posted_key') or '',
                # Description first, title as fallback.
                'redo': parse_redo_list(i.get('given') or '', title),
                'advance': parse_advance(i.get('given') or '', title),
            })
            continue
        s = SUPP_RE.search(title)
        if s:
            supplements.append({
                'level': int(s.group('level')), 'title': title,
                'outstanding': (i.get('status') in ('notdone', 'fic')
                                and not i.get('turned_in')),
            })

    findings = []
    findings.extend(_duplicates(booklets, level_tests, today))
    enough_history = _has_enough_history(
        [b['posted_key'] for b in booklets]
        + [t['posted_key'] for t in level_tests], today)

    # A child young enough to be on the booklet curriculum with no booklet work
    # at all is a finding in itself, not a quiet pass. (Grade was resolved
    # above, where anyone past the curriculum returned early.)
    if not booklets and grade is not None and grade <= BOOKLET_EXPECTED_UP_TO_GRADE:
        findings.append({
            'series': '', 'kind': 'no_booklets', 'expected': [],
            'detail': f"grade {grade} should be on the booklet curriculum, "
                      f"but no booklet work was found",
        })

    # Advancing past a failed test is a judgement the centre made, so it is
    # recorded as such rather than disappearing into a silent pass. Not an
    # alert -- nothing needs fixing -- but it belongs in the log and the record.
    for t in level_tests:
        if t.get('advance'):
            findings.append({
                'series': '', 'kind': 'advanced', 'expected': [],
                'detail': f"advanced past level {t['level']} by decision: {t['advance']}",
            })

    # The level's supplementary packet, checked as soon as the child is a few
    # booklets into the level rather than only when the test falls due. A
    # packet forgotten when a new level starts otherwise goes unnoticed for
    # weeks and then blocks the test.
    btm_seen = [b for b in booklets if b['series'] == 'BTM']
    # Only for a student we have seen long enough. A recent joiner arrives
    # mid-level, and the packet for the level they were already in was issued
    # wherever they came from.
    if btm_seen and enough_history:
        # Two tracks at once means two levels needing a packet, so each active
        # level is checked rather than just the one current position.
        if _running_parallel(btm_seen):
            active = [max((b for b in btm_seen if b['level'] == lv),
                          key=lambda b: b['book']) for lv in PARALLEL_LEVELS]
        else:
            active = [current_position(btm_seen)]
        for current in active:
            if current['book'] >= SUPP_EXPECTED_FROM_BOOK and not any(
                    s['level'] == current['level'] for s in supplements):
                findings.append({
                    'series': 'BTM', 'kind': 'supplement_missing', 'expected': [],
                    'detail': f"level {current['level']} supplementary packet "
                              f"not given yet",
                })

    for series, per_week in (('BTM', BTM_PER_WEEK), ('CTM', CTM_PER_WEEK)):
        in_series = [b for b in booklets if b['series'] == series]
        if not in_series:
            continue

        # What matters is how many booklets the student is CURRENTLY HOLDING,
        # not how many were issued today. A booklet given earlier in the week
        # and still being worked on is this week's issue -- counting only
        # today's handovers demanded the next booklets from students who
        # already had the right ones in hand.
        #
        # This comes FIRST, ahead of the open-FIC check: an open FIC is itself
        # a booklet in hand, so a student can be blocked and still be holding
        # the full week. Nirman had 20-10 FIC and 20-11 -- two BTM, the week
        # satisfied -- and reporting a hold there said nothing a teacher could
        # act on. Nothing owed, nothing to say.
        # Counted by BOOKLET, not by record. A reissued book has two rows -- the
        # lost original and its replacement -- and if the original was never
        # turned in both are outstanding. Counting rows would credit the child
        # with two booklets for one book and quietly skip a real handover.
        holding = {(b['level'], b['book']) for b in in_series if b['outstanding']}
        shortfall = per_week - len(holding)
        if shortfall <= 0:
            continue

        # Short AND blocked: the hold is why nothing more is coming, so it is
        # worth saying. Working the FIC through with the child is the action
        # that unblocks the rest.
        open_fics = [b for b in in_series if b['is_fic'] and b['open']]
        if open_fics:
            findings.append({
                'series': series, 'kind': 'blocked', 'expected': [],
                'detail': f"{series} on hold until "
                          + ', '.join(b['title'] for b in open_fics)
                          + " is fixed",
            })
            continue

        # Two tracks at once (see PARALLEL_LEVELS). The quota is unchanged --
        # two basic thinking booklets a week, wherever they come from -- so a
        # child already holding two is owed nothing regardless of how the two
        # split across the levels. All that changes is WHICH booklets are named
        # next: the track with nothing in hand is filled first, since that is
        # the one that has stopped moving.
        if series == 'BTM' and _running_parallel(in_series):
            cursor, held = {}, {}
            for lv in PARALLEL_LEVELS:
                track = [b for b in in_series if b['level'] == lv]
                cursor[lv] = max(b['book'] for b in track)
                held[lv] = any(b['outstanding'] for b in track)
            order = sorted(PARALLEL_LEVELS, key=lambda lv: held[lv])
            expected = []
            # Two passes: one from each track, then top up from whichever can
            # still advance if a track has run out of booklets.
            for _ in range(2):
                for lv in order:
                    if len(expected) >= shortfall:
                        break
                    if cursor[lv] < BTM_LAST:
                        cursor[lv] += 1
                        expected.append(_label(lv, cursor[lv]))
            if expected:
                findings.append({'series': series, 'kind': 'missing',
                                 'expected': expected, 'detail': ''})
            continue

        # Walk forward from where the student is actually working -- the most
        # recent booklet, not the highest numbered. Levels are taken out of
        # order for younger children (see current_position), so the highest
        # number can be a level they finished and have since moved on from.
        pos = current_position(in_series)
        level, book = pos['level'], pos['book']
        levels_done = {b['level'] for b in in_series if b['level'] != pos['level']}

        # A failed level test can send specific booklets back to be redone.
        # Those take precedence over moving on: they are what should be
        # assigned, in the order the grader listed them, until they are done.
        redo = _pending_redo(level_tests, pos['level'], series, in_series)
        if redo:
            findings.append({
                'series': series, 'kind': 'redo',
                'expected': [_label(r['level'], r['book']) for r in redo[:shortfall]],
                'detail': f"redo set after the level {pos['level']} test",
            })
            continue

        expected = []
        for _ in range(shortfall):
            level, book = next_booklet(level, book, series, levels_done)
            # Crossing into a new level needs the level test passed first.
            # The supplementary packet and level test gate the BASIC THINKING
            # run: eighteen booklets plus the packet are what the level test
            # examines. Critical thinking rolls into the next level on its own.
            if series == 'BTM' and book == BTM_FIRST and level > pos['level']:
                if not enough_history:
                    # The boundary cannot be judged, so say nothing about it --
                    # neither demanding a test nor pushing the child into the
                    # next level on a record that starts mid-way through this
                    # one. Silence beats a confident guess in either direction.
                    expected = []
                    break
                gate = _level_gate(level_tests, pos["level"], supplements)
                if gate:
                    findings.append({'series': series, 'kind': gate['kind'],
                                     'expected': [], 'detail': gate['detail']})
                    expected = []
                    break
            expected.append(_label(level, book))

        if expected:
            findings.append({'series': series, 'kind': 'missing',
                             'expected': expected, 'detail': ''})

    # The early packet check and the level-boundary gate can reach the same
    # conclusion; say it once.
    # Keyed on the MESSAGE, not the kind: the early check and the boundary gate
    # reach the same conclusion under different kinds and would otherwise print
    # the same sentence twice.
    seen_details, deduped = set(), []
    for f in findings:
        if f['detail'] and f['detail'] in seen_details:
            continue
        if f['detail']:
            seen_details.add(f['detail'])
        deduped.append(f)

    return {
        'findings': deduped,
        'missing': [b for f in deduped if f['kind'] in ('missing', 'redo')
                    for b in f['expected']],
        # Things a teacher has to act on that aren't a booklet handover, phrased
        # ready for the banner.
        # 'supplement_outstanding' is deliberately absent: the packet WAS
        # handed over, the student simply hasn't finished it. Nothing was
        # forgotten, so it does not belong in a banner about forgotten
        # handovers -- and the card already shows it as an overdue item. It
        # stays in `findings` for the log, where it still explains why the
        # next level hasn't opened.
        'notes': [f['detail'] for f in deduped
                  if f['kind'] in ('supplement_missing', 'needs_supplement',
                                   'blocked', 'needs_level_test',
                                   # A failed test with no redo set listed is a
                                   # dead end: nothing will be assigned until a
                                   # grader says what to redo.
                                   'level_test_failed', 'level_test_ungraded',
                                   'duplicate')],
        # Marked out in the banner: a mistake rather than a booklet simply not
        # handed over yet.
        'severe': [f['detail'] for f in deduped if f.get('severe')],
    }


def _duplicates(booklets, level_tests, today=None):
    """
    Booklets issued more than once without a reason.

    Three ways a repeat is legitimate: a REDO prefix, a REISSUE prefix, or the
    booklet being named on its own level's redo list. Redo sets are reassigned
    one a week until cleared, so without those exemptions every correctly
    handled failed test would accuse the teacher who did the right thing.

    Bounded twice over. The child must still be HOLDING a copy -- a booklet
    issued twice years ago and finished both times is not something anyone can
    act on now. And a copy must have been issued within the last ten weeks, so
    the check cannot reach back into levels the child has long left.
    """
    cutoff = ''
    if today is not None:
        cutoff = (today - timedelta(weeks=DUPLICATE_LOOKBACK_WEEKS)).isoformat()
    redo_set = {(t['level'], r['level'], r['book'])
                for t in level_tests for r in t.get('redo') or []}
    redo_flat = {(lvl, bk) for _, lvl, bk in redo_set}

    counts = {}
    for b in booklets:
        counts.setdefault((b['series'], b['level'], b['book']), []).append(b)

    out = []
    for (series, lvl, book), group in sorted(counts.items()):
        if len(group) < 2:
            continue
        if (lvl, book) in redo_flat or any(b.get('exempt') for b in group):
            continue
        if not any(b['outstanding'] for b in group):
            continue
        # ISO dates compare correctly as strings. A booklet with no posted date
        # is kept -- absence of a date is not evidence of age.
        if cutoff and not any(not b['posted_key'] or b['posted_key'] >= cutoff
                              for b in group):
            continue
        out.append({'series': series, 'kind': 'duplicate', 'expected': [],
                    'severe': True,
                    'detail': f"{lvl}-{book} assigned {len(group)} times"})
    return out


def _pending_redo(level_tests, level, series, in_series) -> List[Dict[str, int]]:
    """
    Booklets still owed from a redo set, in the order the grader wrote them.

    Only applies while the level test is actually failed: once a passing test
    appears the redo set is spent and normal progression resumes. A booklet the
    student is already holding is treated as under way and not asked for again.
    """
    # A decision to move on ends the redo set, whatever was listed.
    if any(t['level'] == level and t.get('advance') for t in level_tests):
        return []
    failed = [t for t in level_tests
              if t['level'] == level and t.get('redo')
              and t['score'] is not None and t['score'] < LEVEL_TEST_PASS]
    if not failed:
        return []
    if any(t['level'] == level and t['score'] is not None and t['score'] >= LEVEL_TEST_PASS
           for t in level_tests):
        return []          # a later attempt passed; the redo set is done with

    holding = {(b['level'], b['book']) for b in in_series if b['outstanding']}
    latest = max(failed, key=lambda t: t['posted_key'])
    return [r for r in latest['redo']
            if r['series'] == series and (r['level'], r['book']) not in holding]


def _level_gate(level_tests, finished_level, supplements=()) -> Optional[Dict[str, str]]:
    """
    Whether the way is clear from `finished_level` into the next.

    Two gates in order: the level's supplementary packet has to be done, and
    only then does the level test apply. The centre's view is that the 18
    booklets and the packet together are what make the concept stick, so the
    test isn't due until the packet is finished.
    """
    packet = [s for s in supplements if s['level'] == finished_level]
    if not packet:
        return {'kind': 'needs_supplement',
                'detail': f"level {finished_level} supplementary packet not given yet"}
    if any(s['outstanding'] for s in packet):
        return {'kind': 'supplement_outstanding',
                'detail': f"level {finished_level} supplementary packet still outstanding"}

    taken = [t for t in level_tests if t['level'] == finished_level]
    if not taken:
        return {'kind': 'needs_level_test',
                'detail': f"level {finished_level} supplementary packet done — "
                          f"level {finished_level} test is the next thing due"}
    best = max((t['score'] for t in taken if t['score'] is not None), default=None)
    if best is None:
        return {'kind': 'level_test_ungraded',
                'detail': f"level {finished_level} test not graded yet"}
    if best < LEVEL_TEST_PASS:
        # A recorded decision to move on outranks the score.
        advanced = next((t['advance'] for t in taken if t.get('advance')), None)
        if advanced:
            return None
        # A failing score with no decision recorded is itself a job not done:
        # until a grader writes either list, nothing can be assigned and the
        # child simply stalls.
        listed = any(t.get('redo') for t in taken)
        return {'kind': 'level_test_failed',
                'detail': f"level {finished_level} test scored {best}%, under the "
                          f"{LEVEL_TEST_PASS}% pass mark"
                          + ("" if listed else
                             " — needs a REDO: list or an ADVANCE: decision in the "
                             "test description; nothing can be assigned until then")}
    return None
