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


# --- sections below grade 1 are named, not numbered ------------------------
# "Gr K", "Pre-K" and "Weenopi" (the three- and four-year-olds) all read as an
# unknown grade before this, which silently skipped the youngest children.
from english_tracker import grade_number as eng_grade
from booklet_tracker import grade_number as math_grade

GRADES = {'Weenopi': -2, 'Weenopi [2026-2027]': -2, 'Pre-K': -1, 'Pre K': -1,
          'PreK': -1, 'Gr K': 0, 'Grade K': 0, 'KG': 0, 'Kindergarten': 0,
          'Gr 1[2026-2027]': 1, 'Gr 4 [2026-2027]': 4, 'Gr 7/8 [2026-2027]': 7,
          # "Gr" and "Grade" are the same thing, and a stray full stop or
          # hyphen must not make a child's grade unreadable.
          'Grade 1': 1, 'gr 1': 1, 'Gr1': 1, 'Gr. 1': 1, 'Grade. 1': 1,
          'Gr-1': 1, 'Grade-1': 1, 'Gr  1': 1, '1st Grade': 1, '1st grade': 1,
          'Grade 4 [2026-2027]': 4,
          # ...but a word merely starting with "Gr" is not a grade.
          'Group 1': None, '': None}
bad = [f'{s!r} -> english={eng_grade(s)} maths={math_grade(s)} want {want}'
       for s, want in GRADES.items()
       if eng_grade(s) != want or math_grade(s) != want]
print()
print(f'{len(GRADES)} section forms')
print('all pass' if not bad else 'FAIL:\n  ' + '\n  '.join(bad))

# The writing check must reach every section below grade 1, not just Gr 1.
def _no_writing(section):
    r = review_english([eng('ENG A-4 hc', '2026-08-13')], T, section)
    return any('essay writing' in n for n in r['notes'])

wrong = [s for s in ('Weenopi', 'Pre-K', 'Gr K', 'Gr 1') if not _no_writing(s)]
wrong += [s for s in ('Gr 2', 'Gr 3', 'Gr 4') if _no_writing(s)]
print('writing-grade coverage: ' + ('all pass' if not wrong else f'FAIL {wrong}'))


# --- writing booklets: one per level below F, four from F ------------------
# A grade-1 child can be on reading level D and writing level F at once, and F
# holds four booklets worked in turn. Below F there is one booklet covering all
# thirty reading booklets of the level, so finishing it is not a reason to
# expect another -- which is what would have flagged every Pre-K child the
# moment they handed their writing booklet in.
def w_note(items, section):
    r = review_english(items, T, section)
    return [x for x in r['notes'] if 'writing' in x]


def eng_done(t, p='2026-05-01'):
    return {'title': t, 'topic': 'Graded Assignments', 'subject': 'English',
            'subject_stated': False, 'status': 'done', 'turned_in': True,
            'score_percent': 90, 'posted_key': p}


G1 = 'Gr 1[2026-2027]'
check('writing: F1 in hand -> nothing due',
      w_note([eng('ENG D-13 hc', '2026-05-01'), eng('Essay Writing F1', '2026-05-01')], G1), [])
check('writing: F1 finished -> F2 due',
      w_note([eng('ENG D-13 hc', '2026-05-01'), eng_done('Essay Writing F1')], G1),
      ['essay writing F2 not given yet (F1 is finished)'])
check('writing: F4 finished -> nothing further',
      w_note([eng('ENG D-13 hc', '2026-05-01'), eng_done('Essay Writing F4')], G1), [])
check('writing: Pre-K B1 finished -> covers the level',
      w_note([eng('ENG B-20 hc', '2026-05-01'), eng_done('Essay Writing B1')],
             'Pre-K [2026-2027]'), [])
check('writing: Weenopi with no writing booklet at all',
      w_note([eng('ENG A-4 hc', '2026-05-01')], 'Weenopi [2026-2027]'),
      ['no essay writing booklet assigned'])
print()
print('all pass' if not fails else 'FAILURES:\n  ' + '\n  '.join(fails))
