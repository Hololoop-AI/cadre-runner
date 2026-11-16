# Releasing

Cadre uses [semantic versioning](https://semver.org/). The version is in
`VERSION` at the repo root; `pipeline.py --version` prints it.

1. Move the `[Unreleased]` notes in `CHANGELOG.md` under a new heading,
   `## [X.Y.Z] - YYYY-MM-DD`.
2. Put `X.Y.Z` in `VERSION` and in `web/package.json` (`tests/test_version.py`
   fails if they disagree, or if the changelog has no entry).
3. Merge that through a PR, then tag the merge commit on `main`:
   `git tag -a vX.Y.Z -m "Cadre X.Y.Z" && git push origin vX.Y.Z`.

While the version is `0.x`, any release may change behaviour; each such change
is listed under **Changed** in the changelog.
