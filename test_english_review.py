"""Review-logic cases, built from real students in the Classroom screenshots."""
from datetime import date
from english_tracker import review_student

TODAY = date(2026, 8, 22)


def it(title, topic='Homework', subject='English', status='notdone',
       turned_in=False, score=None, posted='2026-08-01', given=''):
    return {'title': title, 'topic': topic, 'subject': subject, 'status': status,
            'turned_in': turned_in, 'score_percent': score, 'posted_key': posted,
            'given': given}


def gr(title, **kw):
    """A finished booklet in the SHARED Graded topic -- no subject named."""
    return it(title, topic='Graded Assignments', subject=None, status='done',
              turned_in=True, score=kw.pop('score', 90), **kw)


def show(name, items, section):
    r = review_student(items, TODAY, section)
    print(f'=== {name}  [{section or "no section"}] ===')
    if not (r['missing'] or r['notes']):
        print('   OK')
    for m in r['missing']:
        print(f'   MISSING: {m}')
    for n in r['notes']:
        print(f'   {"SEVERE" if n in r["severe"] else "note  "}: {n}')
    print()


# Macorina C, Gr 3: on F-30, G-1 is a DRAFT so never arrives, level F test set.
show('Macorina C - F-30 in hand, G-1 only a draft', [
    it('ENG F - 29 HC FIC', topic='Classwork', status='fic'),
    it('ENG Level F Test', topic='Classwork', posted='2026-08-10'),
    it('ENG F - 30 HC'),
    gr('F28 HC'), gr('F27 HC'), gr('F26 HC'),
    it('Classic series A31-1. fic Aug11', topic='Classwork'),
    it('Essay Writing Aug-Sept 2026'),
], 'Gr 3 [2026-2027]')

# Same child a week on: F-30 finished, nothing in hand.
show('Macorina C - F-30 done, nothing in hand', [
    it('ENG Level F Test', topic='Classwork', posted='2026-08-10'),
    gr('F-30 HC'), gr('F28 HC'), gr('F27 HC'),
], 'Gr 3 [2026-2027]')

# Abraam E, Gr 5 -- past the curriculum but still on level I booklets.
show('Abraam E - grade 5, still on booklets', [
    it('ENG I-23 HC'), gr('I - 20 HC'), gr('I - 19 HC'),
    gr('13-24 HC'), gr('19-8 HC'), gr('19-4 HC'),   # maths, must be ignored
], 'Gr 5 [2026-2027]')

# The FIC-rehomework anomaly: three booklets already graded, re-issued as one
# new record. No REDO/REISSUE prefix, so all three are duplicates.
show('Abraam-style FIC re-issue (grade 4) - three duplicates', [
    it('ENG I - 18, I - 19 ,I - 21 FICs assigned for hw 8/11/26'),
    gr('I - 18 HC'), gr('I - 19 HC'), gr('I - 21 HC'), gr('I - 22 HC'),
], 'Gr 4 [2026-2027]')

# The same, done properly with REDO -- must be silent on duplicates.
show('Same three, prefixed REDO - no duplicate flag', [
    it('REDO ENG I - 18 HC'), it('REDO ENG I - 19 HC'),
    gr('I - 18 HC'), gr('I - 19 HC'), gr('I - 22 HC'),
], 'Gr 4 [2026-2027]')

# Test overdue: child is 3 booklets into G with the level F test unfinished.
show('Level F test unfinished, child on G-3', [
    it('ENG G - 3 HC'),
    it('ENG Level F Test', topic='Classwork', status='notdone'),
    gr('F28 HC'), gr('G - 1 HC'), gr('G - 2 HC'),
], 'Gr 3 [2026-2027]')

# Test never given at all, child already a level on.
show('Level F test never given, child on G-4', [
    it('ENG G - 4 HC'), gr('F28 HC'), gr('G - 1 HC'), gr('G - 3 HC'),
], 'Gr 3 [2026-2027]')

# Failed test with neither REDO nor ADVANCE recorded.
show('Level F test failed, no decision recorded', [
    it('ENG G - 1 HC'),
    it('ENG Level F Test', topic='Classwork', status='done', turned_in=True, score=62),
    gr('F-30 HC'),
], 'Gr 3 [2026-2027]')

# Failed test WITH a redo list -- the week's booklet comes from the list.
show('Level F test failed, REDO set drives the week', [
    it('ENG Level F Test', topic='Classwork', status='done', turned_in=True,
       score=62, given='REDO: F-12, F-19, F-24'),
    gr('F-30 HC'),
], 'Gr 3 [2026-2027]')

# Open FIC and nothing else in hand -- hold explains the shortfall.
show('Open FIC, nothing else in hand', [
    it('ENG G - 5 HC FIC', topic='Classwork', status='fic'),
    gr('G - 4 HC'),
], 'Gr 3 [2026-2027]')

# Grade 1: writing booklet expected.
show('Aadhya P - Gr 1, has writing booklet', [
    it('ENG D-13 hc'), it('F1 Essay Writing Aug Sept - 2026', topic='Classwork'),
], 'Gr 1 [2026-2027]')

show('Aadhya P - Gr 1, NO writing booklet', [
    it('ENG D-13 hc'),
], 'Gr 1 [2026-2027]')

# Grade 3 with no writing booklet -- must be silent, writing stops after Gr 1.
show('Gr 3, no writing booklet - must not flag', [
    it('ENG G - 5 HC'), it('Essay Writing Aug-Sept 2026'),
], 'Gr 3 [2026-2027]')

# Ambiguous 6-N in the shared Graded topic, both directions.
show('Gr 4 with English trace - 6-17 reads as English', [
    it('ENG 6-18 HC'), gr('I - 30 HC'), gr('6-17 HC'),
], 'Gr 4 [2026-2027]')

show('Gr 2, no English trace - 6-17 stays maths', [
    it('ENG C-4 HC'), gr('6-17 HC'), gr('C-3 HC'),
], 'Gr 2 [2026-2027]')

# End of the ladder: nothing follows 8-30.
show('End of ladder - 8-30 finished', [
    gr('ENG 8-30 HC'), it('ENG Level 8 Test', topic='Classwork', status='done',
                          turned_in=True, score=95),
], 'Gr 4 [2026-2027]')

# Handed in but unmarked -- the follow-through is the teacher's.
show('Level F test handed in, not yet graded', [
    it('ENG G - 4 HC'),
    it('ENG Level F Test', topic='To Be Graded', status='submitted', turned_in=True),
    gr('F-30 HC'),
], 'Gr 3 [2026-2027]')

# Child never finished it -- different problem, stays severe.
show('Level F test never finished by the child', [
    it('ENG G - 4 HC'),
    it('ENG Level F Test', topic='Classwork', status='notdone'),
    gr('F-30 HC'),
], 'Gr 3 [2026-2027]')
