---
name: learning-debrief
description: >-
  Offers and runs an optional debrief after work the driver mostly delegated
  to agents (a large feature, a long dialogue, a research task): a short,
  hands-on back-and-forth that has the driver predict, explain and try parts
  of what was built before seeing what the agents did, so they stay current
  with their own system. ALWAYS invoke when wrapping up a large delegated
  piece of work, when the driver asks to catch up on or understand what the
  agents built, or when they ask for a quiz on a feature. Do not dump a
  summary of the work as a substitute. Not for a pre-merge walkthrough of a
  PR (pr-walkthrough).
when_to_use: >-
  End of a big feature or multi-round task, "debrief me", "quiz me on what we
  built", "I don't know what this system does anymore", "catch me up on X",
  or a scheduled follow-up a few days after one.
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Learning debrief

**Thesis:** the driver can delegate the flying and still be the pilot — but
only if, now and then, they actively reconstruct what the system is doing.
Reading a summary feels like understanding and isn't; producing an answer is.

Research behind this: under autopilot, pilots kept their hand skills but lost
the thinking ones (where am I, what comes next, what failed); awareness is
lost mainly through passive watching; and fluent material inflates people's
sense of what they know. A self-test taken days later, from a bare prompt, is
the accurate gauge.

## Always optional

Offer it in one line when delegated work wraps up ("Want a 10-minute debrief
on what was built?"). Never make it a gate on a verdict or a merge. If the
driver declines, offer to schedule one for a few days out instead — a delayed
debrief is the more accurate one — and drop it if they decline that too.

## Shape

Pick two to four of these, from what matters most in the work:

- **Predict:** "When a user does X now, what happens?" — before showing code.
- **Explain:** "Why did we end up with X rather than Y?"
- **Locate:** "If X had to change, where would you go, and what else moves?"
- **Hands-on:** for a small, central piece of logic, give the driver the
  function signature and the cases, and let them write it (a scratch file or
  an exercise page). After a real attempt, show what the agent wrote and walk
  the differences together. Keep it to something that fits in 15 minutes.
- **Spot the fault:** show a plausible-but-wrong variant of a real change and
  ask what is wrong with it.

Run each as a back-and-forth (see the `learning` skill on mentoring):
draw the answer out with questions and hints, give it plainly when they ask.
The driver does not need to know everything — aim at what they would need to
take over if something went wrong.

## After

Propose cards (via `learning-cards`) for the misses and the parts the driver
said they want to keep. End with one line: what they have solidly, what is
shaky, and whether a follow-up in a few days is worth it.
