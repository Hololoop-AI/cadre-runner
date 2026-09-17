# Risk record — tag-notes, run 41, stage `risk-triage`

The reference the risk-hold brief is written from: what the mechanical flag
caught, what the triage stage was given to look at, and the diff available to
the author. This is a reference for the judge, never the graded artifact.

**Flag** — `storage-format-change`: a commit in this run rewrites every record
in the notes store on next save.

**Blast radius (glob hit)** — `notes/store.py` (the only writer),
`notes/cli.py` (callers), `tests/test_store.py`.

**Why it tripped** — `save_all()` now serialises records through
`_record_to_json()`, which emits a `tags` key unconditionally. Every existing
store file is rewritten on the first save after upgrade. There is no migration
script and no backup step; the store is a single JSON file overwritten in place.

**What ships if approved** — merge to `main`, which is what the `notes` release
tag is cut from. Users installing from `main` get the new writer.

**The diff the flag concerns**

```
notes/store.py:118-131
 def save_all(path, records):
-    payload = [dict(r) for r in records]
+    payload = [_record_to_json(r) for r in records]
     tmp = path.with_suffix(".tmp")
     tmp.write_text(json.dumps(payload, indent=2))
     tmp.replace(path)

notes/store.py:96-104
+def _record_to_json(r):
+    out = {"id": r.id, "text": r.text, "tags": sorted(r.tags)}
+    return out
```

**Done-criteria touching this** — "a store written before this change keeps
loading, and `notes list` output for it stays byte-identical".

**What the triage stage checked** — the load path (`load_all`) tolerates a
missing `tags` key; the byte-identical assertion in the slice's validation path
covers reads, not writes; no test exercises save-after-load on a pre-change
store.
