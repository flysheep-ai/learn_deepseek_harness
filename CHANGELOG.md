# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Community & contribution infrastructure: `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, `SUPPORT.md`, issue and pull-request
  templates, and a GitHub Actions CI workflow (test matrix + lint).
- Development tooling: `pyproject.toml` (project metadata + Ruff config),
  `Makefile`, `.editorconfig`, `.gitattributes`, `.pre-commit-config.yaml`, and
  `requirements-dev.txt`.

### Fixed

- Restored the `print_messages_compact` helper referenced by the interactive
  `/ctx` command in `s11_task_system`, `s12_background_jobs`, and
  `s13_event_bus` (previously raised `NameError`).
- Removed unused imports surfaced by Ruff in `s03_tool_registry`,
  `s05_session_event_log`, `s15_capability_seams`, and `tests/test_mechanisms.py`.

### Changed

- Corrected the test-count badge (21 → 20) in `README.md`, `README.cn.md`, and
  `DESIGN.md` to match the actual `unittest` result.

## [0.1.0] - 2026-08-14

### Added

- Initial release: 18 self-contained, offline-runnable chapters covering the
  agent loop through a full pluggable harness (session event log, tool pipeline,
  permission, subagents, plugin system, capability seams, goal loop).
- `harness_llm.py` — the single shared model-access layer with OpenAI-compatible,
  Anthropic, and offline `ScriptedProvider` backends.
- Deterministic `unittest` suite covering every chapter's demo plus the key
  mechanisms.
- Bilingual documentation (English primary, Chinese) and per-chapter bilingual
  READMEs.
- `DESIGN.md` (research findings + design decisions) and the Cordis paper reading
  notes under `docs/`.
