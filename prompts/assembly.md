# Stage: Assembly (S5) — all slices built; polish, equip, open the final PR

Every slice of `$story_id` is built and merged into `$feature_branch`. Assemble the story for human review — the final PR must arrive already equipped.

1. You are on a clean `$feature_branch`. Run the full test suite first; it must be green before anything else. If red, STOP: comment the failure on tracking issue #$tracking_issue and end the run.
2. **Cross-slice refactor** — invoke the **refactor** skill over the combined diff vs `origin/$default_branch`. Per-slice refactors already ran inside each build; only cross-slice findings belong here: duplicated helpers across slices, drifted naming, dead scaffolding at the seams. Full suite green after. Commit to `$feature_branch`, push.
3. **Cross-slice code review** — invoke the **code-review** skill (scope-discipline lenses) against the combined diff and each slice's contract. Its verdict and any findings go into the final PR body's review section — do not edit code here.
4. **Review aids** — invoke the **pr-walkthrough** skill against the combined change (head `$feature_branch`, base `$default_branch`). Commit the walkthrough HTML, quiz, and deck.json under `study/$story_slug/` on `$feature_branch`, push.
5. **Final PR** — open `$feature_branch` → `$default_branch`, title "[story] $story_id: $story_title". Body per artifact-voice — orient first (what this story is and why, from the planning artifact), then: slice list with one-liners, links to the walkthrough/quiz files, the code-review verdict, the full-suite result. Merging this PR ships the story; it is the one PR written for human readers.
6. Update `.pipeline/state.json` (`"stage": "final-review"`, final PR number), commit, push. Comment on tracking issue #$tracking_issue: assembly done, link the final PR.

Never merge the final PR — that is the human act that ships the story.
