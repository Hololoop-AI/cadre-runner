---
level: container-boundary
status: accepted
date: 2026-11-15
containers: [statusd, pipeline.py]
firmness: provisional until the board moves into Qadre
---
# 0003 · The kanban board is one locked JSON file with two writers

## Context
The driver asked for a plain kanban board for planning and tracking work
visually, which agents may also read and move. It is unrelated to the event
board (runnerlib/blackboard.py) on purpose: the driver ruled that sharing the
word "board" does not make them related. The plan was approved on the review
page "Kanban board, round 2: a plain board for planning work"
(task-shared-board-widget-a-ka-20261113-175556-bf0f41).

## Decision
1. **Store:** `<data_dir>/kanban.json`, one board per page server, in the
   same folder as items.json and projects.json. No new container.
2. **Two writers:** the page server (POST /kanban, the driver) and
   `pipeline.py kanban` (agents). Every write holds an `fcntl.flock` on
   `kanban.lock` across read, change and an atomic `os.replace`; readers take
   no lock. This is the first data_dir store with two writer processes;
   items.json's in-process lock would not be enough here.
3. **Owner:** the file's `board` field names the sidebar group that owns the
   board (`dev` when absent). That group's home page shows the cards in the
   columns between the first and the last (ActiveCards); other groups do not.
   Nothing else in the code names a group.
4. **Live:** the file is part of the fleet's change signature, so open Board
   screens and home pages hear of an agent's change on the existing stream.
5. **Who:** `by` is the agent's task id, or `driver`. On a shared team
   backend every person posts as `driver`.

## Revisit when
- the board moves into Qadre (which component owns it: a Blackboard
  projection, a Cadre panel or a package);
- a second board, or one board per group, is wanted;
- board changes make the fleet's whole-page re-read noisy (give the stream's
  `change` event a kind).
