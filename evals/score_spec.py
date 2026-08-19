#!/usr/bin/env python3
"""Deterministic scorer: spec-writer output vs an expected file.
Usage: score_spec.py <result.json> <expected.json>
Exit 0 all claims hold; 1 otherwise. Prints one line per claim."""
import json
import sys

result = json.load(open(sys.argv[1]))
expected = json.load(open(sys.argv[2]))["claims"]
slices = result.get("slices") or []
artifact = (result.get("artifact_md") or "").lower()
questions = result.get("open_questions") or []

checks = []

def claim(name, ok, detail=""):
    checks.append((name, bool(ok), detail))

claim("slice_count<=%d" % expected["slice_count_max"],
      0 < len(slices) <= expected["slice_count_max"],
      f"got {len(slices)}: {[s.get('name') for s in slices]}")

if expected.get("every_slice_has_validation_path"):
    bad = [s.get("name") for s in slices
           if len((s.get("validation_path") or "").strip()) < expected.get("validation_path_min_chars", 1)]
    claim("every_slice_has_validation_path", not bad, f"missing/thin: {bad}")

if expected.get("allowed_flows"):
    bad = [(s.get("name"), s.get("flow")) for s in slices
           if s.get("flow") not in expected["allowed_flows"]]
    claim("flows_within_allowed", not bad, str(bad))

if expected.get("every_slice_has_files"):
    bad = [s.get("name") for s in slices if not s.get("files")]
    claim("every_slice_has_files", not bad, f"missing: {bad}")

if expected.get("no_blocking_questions"):
    blocking = [q.get("question") for q in questions if q.get("blocking")]
    claim("no_blocking_questions", not blocking, str(blocking)[:200])

for term in expected.get("forbidden_in_artifact", []):
    claim(f"artifact_never_mentions:{term}", term.lower() not in artifact)

failed = [c for c in checks if not c[1]]
for name, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
print(f"\n{len(checks) - len(failed)}/{len(checks)} claims hold")
sys.exit(1 if failed else 0)
