---
name: learning-course
description: >-
  Runs a self-directed course on a whole subject the driver chooses (system
  design, a language, a domain) across many study sessions: a topic map,
  modules that move from guided explanation to recall to practice problems,
  and cards from each module. ALWAYS invoke when the driver wants to learn a
  subject as a whole rather than clarify one point ("teach me system design",
  "I want to study X properly", "start a course on Y"), and when resuming one.
  Do not answer such a request with a one-off explanation. For a static
  course artifact generated from a codebase, use codebase-course instead.
when_to_use: >-
  Study sessions whose topic is a field or skill, not one passage; resuming a
  course; "what should I study next in X".
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - WebSearch
  - WebFetch
---

# Learning course

**Thesis:** a course is many short sessions of the driver doing the thinking,
in an order that builds — with what they have learned kept alive by review.

## The map

First session: ask what the driver wants to be able to do with the subject
and what they already know, then propose a topic map of 6–12 ideas in
teaching order, each one line. They reorder or cut it. Keep the map in a
course file (`courses/<subject>.md` in the study's working directory unless
the driver keeps one elsewhere): the map, what is done, what was shaky, and
the card topic name used for this course. Read it at the start of every
session — it is how the course has memory across sessions.

## A session

1. **Due cards for this course first** (`learning-review`, with
   `$pipeline cards due --source <course topic> --json`), briefly.
2. **One idea.** Open with a question that gets them thinking about it
   (a prediction, a "how would you…"), then teach it compactly with a
   concrete example, drawing the reasoning out of them as you go.
3. **Practice at the right difficulty.** Recall and explain early; problems
   once the idea holds. For system design: a small scenario to design for,
   then compare with a reference design and discuss trade-offs. For coding
   subjects: a short exercise, then a model solution.
4. **Cards** for what is worth keeping (`learning-cards`).
5. **Update the course file.**

Help fades as competence grows: worked examples for a new area, problems
with little help once the driver is moving well.

## Pace

The driver decides when an idea is done, when to skip ahead, and when to
stop. If an idea is not landing, try a different angle rather than repeating
the same explanation; if they say they have it, believe them and let the
cards check it later.
