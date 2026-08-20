"""Deterministic scorers: node output vs an expected-claims file.
Structure only, never prose. Returns claims; CLI and trackers render them."""

import json


def score_spec(result: dict, expected_claims: dict) -> list[tuple[str, bool, str]]:
    slices = result.get("slices") or []
    artifact = (result.get("artifact_md") or "").lower()
    questions = result.get("open_questions") or []
    checks = []

    def claim(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    exp = expected_claims
    claim("slice_count<=%d" % exp["slice_count_max"],
          0 < len(slices) <= exp["slice_count_max"],
          f"got {len(slices)}: {[s.get('name') for s in slices]}")
    if exp.get("every_slice_has_validation_path"):
        bad = [s.get("name") for s in slices
               if len((s.get("validation_path") or "").strip()) < exp.get("validation_path_min_chars", 1)]
        claim("every_slice_has_validation_path", not bad, f"missing/thin: {bad}")
    if exp.get("allowed_flows"):
        bad = [(s.get("name"), s.get("flow")) for s in slices if s.get("flow") not in exp["allowed_flows"]]
        claim("flows_within_allowed", not bad, str(bad))
    if exp.get("every_slice_has_files"):
        bad = [s.get("name") for s in slices if not s.get("files")]
        claim("every_slice_has_files", not bad, f"missing: {bad}")
    if exp.get("no_blocking_questions"):
        blocking = [q.get("question") for q in questions if q.get("blocking")]
        claim("no_blocking_questions", not blocking, str(blocking)[:200])
    for term in exp.get("forbidden_in_artifact", []):
        claim(f"artifact_never_mentions:{term}", term.lower() not in artifact)
    return checks


def passed(checks) -> bool:
    return all(ok for _, ok, _ in checks)
