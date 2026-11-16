# Changelog

All notable changes to Cadre are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The version lives in `VERSION`;
`web/package.json` must match it (a test checks).

## [Unreleased]

## [0.1.0] - 2026-11-16

First versioned release. Everything before it is in git history.

### Added
- Dialogues: ask for something, an agent does it and writes a review page, you
  answer on the page, and the same session continues.
- The fleet page: every run, the kanban board, records, cards and study
  sessions, across one or more machines.
- A CI check that fails the build when a file names a real deployment.
- `pipeline.py --version`.

### Changed
- The fleet page listens on loopback (`127.0.0.1`) unless `status_bind` in the
  config opens it to another address. It used to listen on every network.
- Skills install to `~/.local/share/cadre/ai-dev-skills` by default; an existing
  clone in the old location is kept. `CADRE_SKILLS_REPO` points the installer at
  another skills repo.
