# English booklet handover — rules as I understand them

Read this as a readback, not a proposal. Everything here came from you; if a line
is wrong, the code will be wrong in the same way. Nothing is open — every question
has an answer, so this is what I will build unless you correct it.

---

## 1. Scope

| | |
|---|---|
| Room | English |
| Topics read | English Classwork · English Homework · English To Be Graded · Graded Assignments |
| Grades checked | **grade 4 and below** |
| Grade 5+ | nothing demanded — but if booklet activity exists, note it ("still on English booklets") |
| Drafts | not assigned. A child cannot see a draft, so it counts as not handed over |

Grade 5+ is the **reverse** of maths, where grade 6+ goes completely silent.
Abraam E (Gr 5, active level I booklets, being moved off booklets this month) is
the case this exists for.

---

## 2. Two series

### Reading booklets — the weekly handover

| | |
|---|---|
| Ladder | **A B C D E F G H I 6 7 8** — twelve levels |
| Per level | 30 booklets |
| Per week | **1 booklet** |
| Roll over | booklet 30 → booklet 1 of the next level |
| Open FIC | blocks the next booklet — the child fixes it in class first |
| Supplementary packets | none (unlike maths) |

`6`, `7`, `8` are genuinely digits, so `6-17` is level 6 booklet 17.

### Writing booklets — not a weekly handover

| | |
|---|---|
| Who | **grade 1 and below only** — never grade 2+ |
| Levels | ladder runs **A–M**, but in practice nothing past E or F is ever reached, since writing stops after grade 1 |
| Per level | A–E: 1 booklet each · F onwards: 4 each |
| How long each lasts | A–E: a whole English level (30 reading booklets) · F+: 6–8 weeks |
| Level test | none |
| Level allowed | the reading level, or one above it (in D → D or E writing) |

Because a single writing booklet spans months, there is no weekly writing
handover. The check is only: **does a writing booklet exist at all, at an
allowed level?** Nothing is ever named as "this week's writing booklet".

---

## 3. The level test — reading series only

| | |
|---|---|
| Titled | new: `TEST: ENG Level I` · legacy: `Level G Test`, `ENG Level G test` |
| Due | when the child enters **booklet 28** |
| Runs across | 28, 29, 30, and booklets 1–2 of the next level — 5 booklets of tolerance |
| Past that | **prominent flashing flag** |
| Finished | **graded** |
| Pass | **80 or above** |
| Failed | `REDO:` in the test description lists which of the level's 30 booklets to redo. The test stays in Classwork until they are done |
| Override | `ADVANCE` in the description — a deliberate decision to move on |

Redo booklets are **created as new assignments, one a week**, until the list is
cleared. So while a redo list is outstanding the week's booklet comes from that
list rather than from moving forward — the same shape as maths.

The test does **not** gate the move to the next level. Unlike maths, the child
carries on while the test is still running.

---

## 4. Naming and parsing

### Going forward — one canonical form

```
[REDO|REISSUE] <SERIES> <level>-<booklet> [HC] [FIC]
TEST: <SERIES> Level <level>
ESSAYWriting: <level>-<booklet> [HC]      (English writing booklets)
ESSAYWriting: <anything with no level>    (prompt work, grade 2+, not a booklet)
```

| example | reads as |
|---|---|
| `BTM 15-17 HC` | maths basic, level 15 booklet 17 |
| `CTM 12-20 HC` | maths critical |
| `ENG I-23 HC` | English reading |
| `ESSAYWriting: F-1 HC` | English writing |
| `REDO BTM 12-9 HC` | redo — exempt from the duplicate check |
| `REISSUE ENG F-12 HC` | lost book reissued — exempt from the duplicate check |
| `TEST: ENG Level I` | English level test |
| `TEST: BTM Level 18` | maths basic level test |
| `TEST: CTM Level 12` | maths critical level test |

The series belongs in the test title because `Level 6` alone is ambiguous between
rooms — and because maths has separate Basic and Critical tests at the same
level, which the current parser cannot tell apart. A Critical test can therefore
satisfy a Basic level gate today. `TEST: BTM` / `TEST: CTM` fixes that.

### Historical data — heuristics, unavoidably

Nothing can be renamed retroactively, so the rules below still have to carry every
booklet issued before the convention. They apply ONLY where a prefix is absent.

| form | reads as |
|---|---|
| `ENG H - 4 HC FIC` | reading, level H booklet 4 |
| `ENG I-23 HC` | reading, level I booklet 23 |
| `ENG H--1 HC FIC` | reading, level H booklet 1 (double hyphen tolerated) |
| `ENG D-13 hc` | reading, level D booklet 13 (lowercase) |
| `EW F-1 HC` | writing, level F booklet 1 *(proposed prefix)* |
| `F28 HC` | legacy reading, level F booklet 28 |
| `F1 Essay Writing Aug Sept - 2026` | legacy **writing**, level F booklet 1 |
| `Essay Writing Aug-Sept 2026` | not a booklet — prompt work, grade 2+ |

Recognition rules:

- **Level must be one of the twelve.** This alone rules out `Classic Series C1-4`,
  `New Series A1-2`, `DGP Gr2 Week 5`.
- **Unprefixed reading booklets require `HC`/`hc`.** It is always present on them.
  Without this rule `F1 Essay Writing` would read as a reading booklet.
- **Legacy writing booklets carry no `HC`** — recognised by a leading level token
  plus the phrase "Essay Writing".
- **A title may name several booklets.** `ENG I - 18, I - 19 ,I - 21 FICs
  assigned for hw` yields all three, so repeats are caught.

---

## 5. Duplicate booklets — SEVERE

The same booklet must never appear twice — **except when it is on a `REDO` list**.

That exemption is essential, not a nicety. Redo booklets are deliberately
reassigned as fresh records, one a week, so without it every correctly-handled
failed test would fire a SEVERE flag at the teacher who did the right thing.

The rule, precisely. A repeat is legitimate if **either** signal is present:

1. the title carries a `REDO` prefix — `REDO BTM 12-9 HC`, `REDO ENG F-12 HC`
2. the title carries a `REISSUE` prefix — the physical book was lost and replaced
3. the booklet is named in a `REDO:` list on that level's test

Any of these exempts it, and an exempt booklet may repeat **any number of times** — a
child can be sent through the same booklet across several failed attempts. A
booklet with neither signal appearing more than once is **SEVERE**.

Several signals rather than one because the redo list alone requires joining two records:
the booklet here, the list in the test's description there. A forgotten or
mistyped list, or an archived test, would turn a correctly-issued redo into an
accusation against the teacher who did the right thing. The prefix makes the
booklet self-identifying.

**The `REDO` prefix must not replace the series prefix.** Write
`REDO BTM 12-9 HC`, never `REDO 12-9 HC`: unprefixed booklets are recognised only
when the number pair leads the title, so `REDO` in front of a bare number hides
the booklet from the parser entirely — worse than no prefix at all. With
`BTM`/`CTM`/`ENG` present the label is found anywhere in the title.

When a graded booklet merely needs more work at home, the teacher **moves the
existing assignment** into Homework rather than writing a new record — so that
case produces no repeat at all.

Counted **per series**, not globally. Reading levels A–I and writing levels A–M
overlap, so `ENG F-1` and `EW F-1` are different booklets that happen to share a
level and a number. Comparing across series would flag them as a duplicate.

Abraam's `ENG I - 18, I - 19, I - 21 FICs assigned for hw` is exactly the
anomaly this catches: those three were graded weeks earlier, became FICs, and
were re-issued as a new record instead of being moved. Sending FICs home is
itself against normal practice.

---

## 6. What the banner says

Ordinary lines, as maths does today:

- booklets not handed over, named
- reading series on hold behind an open FIC — **only when the child is short**
- grade 5+ still on booklets

Prominent / flashing:

- **level test not started, or not finished past the 5-booklet tolerance**
- **the same booklet assigned twice**

---

## 7. Questions that were open, and how they closed

**A. Do redo booklets get reassigned?** Yes — as new records, weekly. So the
duplicate check exempts anything carrying a `REDO` prefix or named on that level's
redo list (section 5). Check that the exemption is drawn where you would draw it:
it keys on the booklet's own level, so a repeat of `F-12` is exempt when `F-12` is
on the level F redo list, but a repeat of `G-12` is still SEVERE.

**B. Which room owns a legacy `6-N` / `7-N` / `8-N HC`?** See section 7b — maths by
default, English when the grade and the student's own history both point there.

---

## 7b. The one title that belongs to both rooms

`6`, `7` and `8` are levels in **both** curricula, and the `Graded Assignments`
topic names no subject — so a legacy unprefixed `6-17 HC` sitting there is
undecidable from the title alone.

Verified against Abraam's real data that nothing else collides: `I - 19 HC` never
reads as maths (letters aren't maths levels), and `13-24 HC` / `19-8 HC` never
read as English (13 and 19 aren't English levels). Only these three levels.

**Default: maths.** Overridden to English only when both of these hold:

1. the student is in **grade 3 or 4**, and
2. their English history independently reaches that far — a prefixed `ENG`
   booklet or an English-topic booklet at level I or beyond

The grade test is the useful part, and it comes from the curriculum's shape:
English 6–8 sit at the **top** of a twelve-level ladder, so only a grade 3–4
child reaches them. Maths 6–8 sit **early** in a twenty-four-level ladder, so
they belong to a child below grade 3. The same digits mean opposite ages.

Requiring both conditions keeps it conservative. The residual failure mode: a
grade 3–4 child whose *only* English booklet history is these ambiguous items has
no independent trace, so they are read as maths and their English position stays
unknown. Worth checking against one real advanced reader before trusting it.

Prefixes remove the ambiguity entirely for everything issued from now on.

---

## 8. Where English differs from maths

| | maths | English |
|---|---|---|
| per week | 2 BTM + 1 CTM | 1 |
| series | two, parallel | two, but writing is not weekly |
| level identifiers | 1–24 | A–I, 6, 7, 8 |
| per level | 18 + 12 | 30 |
| level test | hard gate before the next level | runs *across* the boundary |
| test due at | after booklet 18 + packet | booklet 28 |
| supplementary packet | yes | no |
| grade cut-off | silent above 5 | checked to 4, **flagged** above |
| duplicate check | not implemented | SEVERE |
