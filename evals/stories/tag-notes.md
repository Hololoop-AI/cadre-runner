# Story: tag notes and filter by tag

**Summary** — A user can attach tags when adding a note and list only the
notes carrying a given tag.

**Why / Outcome** — Notes pile up; the CLI needs a way to slice them. Tags at
add time plus a filtered list is the smallest useful version.

**Acceptance Criteria**
- Given `notes add "buy milk" --tag errand --tag home`, When it runs, Then the
  note is stored carrying exactly those tags.
- Given stored notes with and without the tag, When `notes list --tag errand`
  runs, Then only notes tagged `errand` print, in id order.
- Given a note added with no `--tag`, When any command runs, Then it behaves
  exactly as today — *example:* `notes list` output for untagged notes is
  byte-identical to the current format.

**Constraints**
- Storage stays one JSON file; existing files without tag fields keep loading.

**Non-Goals**
- Renaming or deleting tags; tag autocomplete; multi-tag boolean queries.
