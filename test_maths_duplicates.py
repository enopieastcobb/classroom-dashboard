"""Maths duplicate check -- one record is never a duplicate.

Niyam Shah's card showed BTM 11-8 FIC, BTM 11-9 FIC and CTM 8-28, all flagged
as assigned twice. The intended workflow is to mark the EXISTING graded
assignment as a FIC and move it to Classwork, which leaves one record. These
cases pin that a single record stays silent, so a future flag means the data
really does hold two.
"""
from datetime import date
from booklet_tracker import review_student

T = date(2026, 8, 22)


def it(t, topic='Classwork', subject='Math', status='fic', turned_in=False,
       score=None, posted='2026-08-12'):
    return {'title': t, 'topic': topic, 'subject': subject, 'status': status,
            'turned_in': turned_in, 'score_percent': score, 'posted_key': posted}


def graded(t, p):
    return it(t, topic='Graded Assignment', subject=None, status='done',
              turned_in=True, score=90, posted=p)


def check(name, items, expect):
    got = review_student(items, T, 'Gr 1 [2026-2027]')['severe']
    ok = sorted(got) == sorted(expect)
    print(f'{"ok  " if ok else "FAIL"}  {name:52} {got or "-"}')
    return ok


niyam = [it('BTM 11-8 FIC'), it('BTM 11-9 FIC'),
         it('CTM 8-28 online copy fic Aug17'),
         it('L11 supplementary packet.', status='notdone')]

results = [
    # The intended workflow: one record, moved rather than recreated.
    check('one record per booklet (moved, not recreated)', niyam, []),
    # The mistake: the graded copy left behind AND a new FIC record created.
    check('graded copy left behind + new FIC record',
          niyam + [graded('BTM 11-8 HC', '2026-08-01')],
          ['11-8 assigned 2 times']),
    # Deliberate repeats stay silent.
    check('REDO prefix exempts',
          niyam + [graded('REDO BTM 11-8 HC', '2026-08-01')], []),
    check('REISSUE prefix exempts',
          niyam + [graded('REISSUE BTM 11-8 HC', '2026-08-01')], []),
    check('REISSUED past tense also exempts',
          niyam + [graded('REISSUED BTM 11-8 HC', '2026-08-01')], []),
    # FINISH goes on the row that already exists -- no new book, no new
    # record -- so two FINISH rows for one booklet is a real mistake and
    # must NOT be forgiven the way REDO and REISSUE are.
    check('FINISH does NOT exempt: it should be one row',
          niyam + [graded('FINISH BTM 11-8 HC', '2026-08-01')],
          ['11-8 assigned 2 times']),
    # The ten-week bound is about the PAIR being stale, not the older copy.
    # A booklet in the child's hands today that they finished in January is
    # still a live mistake, so it flags.
    check('old first copy, current copy in hand -> flags',
          niyam + [graded('BTM 11-8 HC', '2026-01-05')],
          ['11-8 assigned 2 times']),
    # Both copies beyond the window: outside what anyone can act on.
    check('both copies older than ten weeks -> silent',
          [it('BTM 11-8 FIC', posted='2026-01-12'),
           graded('BTM 11-8 HC', '2026-01-05'),
           it('L11 supplementary packet.', status='notdone')], []),
]
print()
print('all pass' if all(results) else 'FAILURES ABOVE')
