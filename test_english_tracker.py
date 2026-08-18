"""Regression cases for the English booklet parser.

Every title here is REAL, taken from Classroom screenshots. The patterns are
fragile by necessity -- legacy booklets carry no prefix and share their shape
with several non-booklet series in the same room -- so these cases exist to stop
a pattern change quietly breaking recognition.
"""
from english_tracker import (parse_reading, parse_writing, parse_level_test,
                             parse_reading_all, next_reading_booklet)

READING = ['ENG H--1 HC FIC', 'ENG H - 4 HC FIC', 'ENG H - 2 HC FIC',
           'ENG I-23 HC', 'ENG D-13 hc', 'ENG F - 29 HC FIC', 'ENG G - 1 HC',
           'ENG F - 30 HC', 'ENG H - 13 HC', 'ENG F-21 HC', 'F28 HC', 'F27 HC',
           'F26 HC', 'I - 20 HC', 'I - 19 HC', 'H-5 HC', 'G28-HC', 'G 24 HC',
           'G - 24HC', 'REISSUE ENG F-12 HC', 'REDO ENG F-12 HC']

WRITING = ['F1 Essay Writing Aug Sept - 2026', 'ESSAYWriting: F-1 HC',
           'ESSAYWriting: C-1 HC']

TESTS = {'ENG Level G Test': 'G', 'ENG Level F Test': 'F', 'Level G Test': 'G',
         'TEST: ENG Level I': 'I', 'TEST: ENG Level 6': '6'}

# Real titles from the same room that must never read as booklets.
NEITHER = ['Classic series A11-4. Fic Aug11', 'Classic Series C1-4',
           'Classic Series C1-5', 'Classic series A31-1. fic Aug11',
           'Classic Series C11-1', 'New Series A1-2.', 'New Series A1-1.',
           'Essay Writing Aug-Sept 2026', 'DGP Gr2 Week 5 fic Aug18',
           'DGP Gr6 Week 1.', 'PR Gr3 Week 4. fic Aug1 ,6',
           'Roots Gr3 Part 15. Fic inc Aug12', 'Selections gr 4 pt 1',
           'Selection Gr2 Part 3 FIC Jun14 ,27 ,Jul26', 'SAT Vocab 1.',
           'Grammar binder Gr6 Unit 1.', 'Proof Reading Guide', 'DgP Gr6 guide',
           'Word Roots Gr4 part 2.', 'Poetry Fun Gr1 pt 4.', 'Grammar week 3.',
           'Proof Reading Gr 4 pt 3.', 'Reading Selection Gr2 Pt2.',
           'Root Gr3 Part 5.', 'A11 beachcomber notes']

fails = []
for t in READING:
    if not parse_reading(t):
        fails.append(f'reading not recognised: {t!r}')
for t in WRITING:
    if not parse_writing(t):
        fails.append(f'writing not recognised: {t!r}')
    if parse_reading(t):
        fails.append(f'writing ALSO read as reading: {t!r}')
for t, lvl in TESTS.items():
    if parse_level_test(t) != lvl:
        fails.append(f'test level wrong: {t!r} -> {parse_level_test(t)} want {lvl}')
for t in NEITHER:
    if parse_reading(t) or parse_writing(t):
        fails.append(f'false positive: {t!r}')

multi = [f"{b['level']}-{b['book']}" for b in
         parse_reading_all('ENG I - 18, I - 19 ,I - 21 FICs assigned for hw 8/11/26')]
if multi != ['I-18', 'I-19', 'I-21']:
    fails.append(f'multi-booklet extraction: {multi}')

# Ladder: 30 rolls into the next level; I rolls into 6; 8-30 is the end.
for got, want in ((next_reading_booklet('F', 29), ('F', 30)),
                  (next_reading_booklet('F', 30), ('G', 1)),
                  (next_reading_booklet('I', 30), ('6', 1)),
                  (next_reading_booklet('6', 30), ('7', 1)),
                  (next_reading_booklet('8', 30), None)):
    if got != want:
        fails.append(f'ladder: got {got} want {want}')

print(f'{len(READING)+len(WRITING)+len(TESTS)+len(NEITHER)+6} cases')
print('FAIL:\n  ' + '\n  '.join(fails) if fails else 'all pass')
