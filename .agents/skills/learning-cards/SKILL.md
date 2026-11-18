---
name: learning-cards
description: >-
  Writes, reframes and judges spaced-repetition cards for the driver's deck in
  Cadre (`pipeline.py cards`): a question, a kind, and a reference answer the
  tutor grades against. ALWAYS invoke before proposing, revising or triaging a
  card — from a task page, a study session, a passage thread or a debrief. Do
  not write `cards propose` JSON directly without it.
when_to_use: >-
  Any time a card is about to be proposed ("propose one to three cards"),
  when the driver says a card is weak, vague or trivia, when cleaning up
  proposed cards, or when a review shows a card is badly framed.
allowed-tools:
  - Read
  - Bash
---

# Learning cards

**Thesis:** a card is worth its place only if answering it, months from now
and cold, rebuilds something the driver wants to keep. Everything below
serves that.

Cards are the driver's agentic Anki: anything they want to retain — code they
own, a decision's reasons, a new subject — reviewed on an FSRS schedule, graded
by a tutor against the card's reference. AI-written cards fail often (in a
2026 study about a third were unusable, and models were poor judges of their
own cards), so write deliberately and expect the driver to keep, reframe or
drop each one.

## Writing a card

Ask first: what will the driver want to be able to do with this later — recall
it, explain it, apply it, or find their way in code? That picks the kind and
the question style.

| Kind | Asks the driver to | Good question shapes |
|---|---|---|
| `fact` | recall something precise that is worth not looking up | "What does X mean in Y?", a cloze on the one load-bearing term |
| `concept` | explain a why, a trade-off, a relationship | "Why does X … rather than …?", "What breaks if …?" |
| `procedure` | carry out or choose steps | "How would you …?", "What comes after … and why?" |
| `code` | understand code they own | predict ("what happens when …"), locate ("where would a change to … go"), implement a small piece |

Properties that make a card hold up over many reviews (from Andy Matuschak's
writing on prompts): **focused** (one idea), **precise** (one right answer
shape, not "tell me about X"), **consistent** (the same answer every time),
**tractable** (answerable from memory), **effortful** (it actually makes the
driver retrieve, not recognise). Name the context — "In cadre-runner, …",
"In sourdough baking, …" — because the card is read cold.

Signs of a weak card, and what to do instead:
- A signature, argument list, file name or number that is a 30-second lookup
  and changes no choice → ask why it is shaped that way, or what depends on it.
- "Explain the whole X" → split into the two or three ideas that matter.
- The answer is in the question → move the hint into the reference.
- It only makes sense with today's page open → add the context it needs.

## The reference

Every card carries one; the tutor grades and teaches from it, the driver
never sees it before answering.

```json
{"answer": "two or three sentences: the ideal answer",
 "points": ["what a good answer must hit", "one per idea"],
 "mistakes": [{"wrong": "a likely wrong idea", "fix": "its correction"}]}
```

Points are what grading checks, so make them the substance, not the wording.
Add a mistake only when there is a real, likely one. For code cards, anchor
the reference in the code as it is now (`--anchor <file or commit>`); it can go
stale, and the anchor is how a reviewer notices.

## Commands

The deck is `pipeline.py cards` (in Cadre the full command, with `--config`,
is given in your prompt as `$pipeline` or spelled out).

```bash
# propose — lands as "proposed"; the driver keeps, reframes or drops it
echo '{"cards":[{"front":"In <topic>, …","kind":"concept","reference":{"answer":"…","points":["…"]}}]}' \
  | $pipeline cards propose --source <topic name> --anchor <file, commit, page or task>

# reframe — same id, same schedule and history, new wording/kind/reference
echo '{"front":"…","kind":"code","reference":{"answer":"…","points":["…"]}}' \
  | $pipeline cards revise <card id>
```

`--source` is a topic by name ("cadre-runner", "system design"), never a folder
path. A proposal missing a kind or a reference is refused with the reason.

## Reframing instead of dropping

When the driver calls a card weak, or a review shows it is misframed, the
default is to rescue what is worth keeping: find the idea underneath, pick the
kind and question shape that fits it, rewrite the reference, and `revise` it.
Show the before and after in one line each. Drop only when nothing underneath
is worth remembering — and say why.

## How many

One to three per page or session, chosen for what the driver will most want
in six months. More cards is not more learning; a deck the driver distrusts
gets abandoned.
