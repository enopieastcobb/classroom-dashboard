"""A level test is only expected of a student we have seen long enough.

Nathan W joined a fortnight ago, arrived already on E-2, and was flagged for a
level D test that was never ours to set -- his level D work happened somewhere
else. Six weeks of record is the threshold; below it, nothing about a level
boundary is asserted in either room.
"""
from datetime import date
from english_tracker import review_student as review_english
from booklet_tracker import review_student as review_maths

T = date(2026, 8, 20)
fails = []


def check(name, got, want):
    ok = sorted(got) == sorted(want)
    print(f'{"ok  " if ok else "FAIL"}  {name:46} {got or "-"}')
    if not ok:
        fails.append(f'{name}: got {got} want {want}')


def eng(t, posted, topic='Homework'):
    return {'title': t, 'topic': topic, 'subject': 'English',
            'subject_stated': True, 'status': 'notdone', 'turned_in': False,
            'score_percent': None, 'posted_key': posted}


def eng_done(t, p):
    return {'title': t, 'topic': 'Graded Assignments', 'subject': 'English',
            'subject_stated': False, 'status': 'done', 'turned_in': True,
            'score_percent': 90, 'posted_key': p}


def m(t, posted, topic='Homework', status='notdone', turned_in=False):
    return {'title': t, 'topic': topic, 'subject': 'Math', 'status': status,
            'turned_in': turned_in, 'score_percent': 90 if turned_in else None,
            'posted_key': posted}


def m_done(t, p):
    return m(t, p, topic='Graded Assignment', status='done', turned_in=True)


# --- English: Nathan's real shape ------------------------------------------
nathan = [eng('ENG E-2 HC', '2026-08-13'),
          eng('ESSAY WRITING F1 AUG SEPT F1 - 2026', '2026-08-13'),
          eng_done('D-30 HC', '2026-08-06')]
check('English: joined 2 weeks ago, arrived on E-2',
      review_english(nathan, T, 'Gr 1[2026-2027]')['notes'], [])
check('English: same position, long-standing student',
      review_english(nathan + [eng_done('D-10 HC', '2026-02-01')], T,
                     'Gr 1[2026-2027]')['notes'],
      ['level D test missing -- child is already on E-2'])

# --- maths: the boundary must go quiet, mid-level must not -----------------
edge = [m_done('BTM 15-17 HC', '2026-08-13'), m_done('BTM 15-18 HC', '2026-08-13'),
        m('CTM 12-20 HC', '2026-08-13')]
check('maths: new joiner AT a level boundary -> silent',
      review_maths(edge, T, 'Gr 3')['missing'] + review_maths(edge, T, 'Gr 3')['notes'],
      [])
mid = [m_done('BTM 15-8 HC', '2026-08-13'), m_done('BTM 15-9 HC', '2026-08-13'),
       m('CTM 12-20 HC', '2026-08-13')]
check('maths: new joiner mid-level still owed booklets',
      review_maths(mid, T, 'Gr 3')['missing'], ['15-10', '15-11'])
check('maths: long-standing at the same boundary -> packet flagged',
      review_maths(edge + [m_done('BTM 15-2 HC', '2026-03-01')], T, 'Gr 3')['notes'],
      ['level 15 supplementary packet not given yet'])

print()
print('all pass' if not fails else 'FAILURES:\n  ' + '\n  '.join(fails))
