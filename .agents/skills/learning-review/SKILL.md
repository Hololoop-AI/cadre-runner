---
name: learning-review
description: >-
  Runs a review of the driver's due cards in Cadre: they answer, the tutor
  grades against each card's reference, draws out what is missing, and
  records a grade that schedules the next review. ALWAYS invoke when cards are
  due in a study session, when the driver asks to review or practise their
  cards, or when grading a STUDY ANSWERS block that includes card answers. Do
  not grade cards by gut feel without it.
when_to_use: >-
  "review my cards", "what's due", the start of a tutor round with due cards,
  grading answers to card questions, the Learn page's review flow.
allowed-tools:
  - Read
  - Bash
---

# Learning review

**Thesis:** a review is the driver rebuilding an answer from memory, and the
tutor's part is to make that rebuild succeed a little more each time — not to
supply the answer.

## The loop, per card

1. **Ask.** Show the card's front, nothing else. Pick the move from its kind
   and history (below); the same card can be asked differently on different
   days, but keep its core question so the schedule stays meaningful.
2. **Let them answer.** A blank or "no idea" is a real answer, graded 1
   (unless the card is the problem; see below).
3. **Grade against the reference.** `cards due --json` returns each card's
   `reference`: check the answer point by point, and name any listed mistake
   it makes. Grade 1–4: 1 wrong or blank, 2 partial with a real gap, 3 right
   with minor imprecision, 4 right and precise. Grade substance, never length
   or wording.
4. **Draw out the gap, contingently.** For each missing point, the lightest
   help that might work: "say more", then a hint, then a pointed question,
   then the point itself. After a miss, give more help; after a success, give less — that
   contingency is what makes tutoring work. Stop when the gap is closed or the
   driver wants the answer; then give it plainly.
5. **Record.** `$pipeline cards review <id> --grade <driver's grade>
   --answer "<their answer>" --model-grade <yours> --reasoning "<the gap>"`.
   Propose your grade; the driver's is the one that schedules. If they
   dispute it, theirs goes in `--grade`, yours stays in `--model-grade`.

## Choosing the move

| Situation | Move |
|---|---|
| Fact card, seen before | plain recall |
| Concept card | ask them to explain it, then probe one point |
| Procedure card, recall is solid | give a small new case and ask them to apply it |
| Code card | predict ("what happens when …"), locate ("where would … go"), or a short implement-it-then-compare |
| Two cards they keep confusing | ask them side by side and have them name the difference |
| Several misses in a row on a topic | a short worked example, then one attempt |
| Little time or low energy | fewer cards, recall only, no probing |

These are defaults. Read the driver: short answers, repeated "skip", or
"just tell me" mean lighten up; engaged, long answers mean you can go deeper.

## When the card is the problem

If a miss comes from the card — ambiguous, trivia, stale against the code —
say so, and reframe it with the `learning-cards` skill (`cards revise`) rather
than grading the driver down for it.

## Output

In chat: one card at a time, the grade with one line naming the gap, then the
next card. On a page: due cards as question + answer box, graded next round.
At the end, one line: how many reviewed, how they went, what is due next.
