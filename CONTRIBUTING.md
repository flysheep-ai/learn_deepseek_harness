# Contributing to learn-agent-harness

Thanks for your interest in contributing! 🎉

`learn-agent-harness` is a **runnable course on agent harness internals** — not a
production framework. Contributions that make it a *better course* are welcome:
clearer explanations, better examples, fixed bugs, new exercises, better tests,
translations, or corrections.

Please read this guide before opening a pull request. It is short and specific.

---

## Code of Conduct

All participants are expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Be kind, be patient, and remember this is
a teaching resource.

---

## What kind of contribution is a good fit

This project has one hard rule that shapes every design decision:

> **Model decides. Harness enables.**
> The agent's intelligence comes from the model. The harness must not script the
> model's thinking — it builds an operable world of tools, environment, context,
> state, permissions, and feedback.

Two consequences for contributions:

1. **No decision-routing in the harness.** Branches like
   `if task_type == "research"` or `call_coding_agent(...)` are off-limits in the
   harness body. There is a test
   ([`ModelDecidesTest`](tests/test_mechanisms.py)) that scans for these.
2. **Teaching value > feature count.** This is a textbook, not a framework.
   A feature that obscures the lesson it sits next to will not be merged, no
   matter how useful it is in production.

Also see [DESIGN.md](DESIGN.md) — the design contract — and the "two iron laws" in
[README.md](README.md).

### Good fits

- Fixing a bug in a chapter's `code.py` or `harness_llm.py`
- Clarifying a confusing paragraph in any README
- Adding a "Try it yourself" exercise to a chapter
- Adding a deterministic test for a mechanism that is currently untested
- Fixing a broken link, badge, or stale claim
- Translating chapter READMEs into a new language

### Poor fits (please do not open a PR for these)

- New framework features (streaming, concurrency, MCP/ACP/LSP, Web UI, real
  sandbox) — these are explicitly out of scope, see [DESIGN.md](DESIGN.md)
- Restructuring chapters into a shared `src/` package — deliberate duplication
  is part of the teaching method

When in doubt, **open an issue first** and discuss before writing code.

---

## Project layout

```
learn-agent-harness/
├── harness_llm.py          # the ONLY shared module (model access, no harness logic)
├── s01_agent_loop/         # code.py + README.md (中文) + README.en.md
├── s02_tool_use/           #   …
├── …
├── s18_full_harness/       # code.py + READMEs + skills/
├── tests/                  # deterministic unittest suite
├── docs/                   # paper notes (Cordis, in Chinese)
├── DESIGN.md               # research findings + design decisions
└── README.md               # English primary; README.cn.md is the Chinese version
```

Every chapter is a **complete, self-contained, offline-runnable Python file**.
There is no shared `src/` package: each chapter intentionally re-implements the
harness so that diffing two adjacent chapters answers "what did this chapter add".

The single exception is `harness_llm.py` — HTTP transport is not harness
mechanics, so it is shared.

---

## Development setup

Python **3.11+** is required.

```sh
# 1. Clone and enter the project
git clone https://github.com/flysheep-ai/learn_deepseek_harness.git
cd learn_deepseek_harness

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install runtime + development dependencies
make install-dev                    # or: pip install -r requirements.txt -r requirements-dev.txt
```

Only one runtime dependency is used (`httpx`). Everything runs offline with no
API key via each chapter's `--demo` flag.

---

## Running tests

The test suite is deterministic (it uses a scripted fake model) and must pass
before any PR:

```sh
make test                          # python3 -m unittest discover tests
```

Or, without `make`:

```sh
python3 -m unittest discover tests
```

Run a single chapter offline:

```sh
python3 s18_full_harness/code.py --demo --debug
```

---

## Linting

Static checks use [Ruff](https://docs.astral.sh/ruff/):

```sh
make lint                          # ruff check .
make lint-fix                      # ruff check . --fix
```

The rule set is pinned to the classic pyflakes + pycodestyle set
(`select = ["E4", "E7", "E9", "F"]` in [`pyproject.toml`](pyproject.toml)) —
newer Ruff releases keep expanding the default selection, which would churn the
chapter files. Two rules are additionally ignored:

- `E731` (lambda-assignment) — chapters use `X = lambda ...` as a teaching idiom
- `F541` (f-string without placeholders) — some demo prints keep an `f` prefix
  to make a later interpolation edit obvious

Do **not** run an auto-formatter over the chapter `code.py` files. The
`# ── 沿用 sXX（未改动） ──` / `# ── sXX 新增 ──` markers and the layout are
part of the lesson; mass reformatting destroys the ability to diff chapters.

---

## Conventions for code changes

1. **Keep every chapter self-contained.** A mechanism taught in chapter N lives
   in chapter N's `code.py` in full — do not "DRY it up" into `harness_llm.py`.
2. **Mark evolution in the code.** Use the established comment markers:
   `# ── 沿用 sXX（未改动） ──`, `# ── sXX 新增：<概念> ──`,
   `# ── sXX 改写：…… ──`.
3. **Prefer plain `dict` for messages.** Do not introduce a `Message` class; see
   [DESIGN.md §3.2](DESIGN.md).
4. **Keep `--demo` offline.** Any new behavior must be demonstrable with
   `ScriptedProvider` and covered by a deterministic test when it is a mechanism.

---

## Pull request checklist

- [ ] `make test` passes (20 tests, all green)
- [ ] `make lint` passes (`ruff check .`)
- [ ] Every chapter's `python sXX_*/code.py --demo` still runs offline
- [ ] New mechanisms are covered by a deterministic test in `tests/`
- [ ] The change respects **"Model decides. Harness enables."** (no task routing)
- [ ] READMEs/docs are updated when behavior or layout changes
- [ ] No auto-formatter was run over the chapter files

---

## Commit messages

Keep them short and descriptive, in the imperative mood. This repository's
history mixes Chinese and English; either is fine, but be consistent within a
commit.

Good:

```
Fix NameError in s11/s12/s13 interactive /ctx command
docs: clarify the difference between turn and step
test: cover compaction shadow boundary
```

---

## Questions?

Open a [discussion](https://github.com/flysheep-ai/learn_deepseek_harness/discussions)
or an issue. See [SUPPORT.md](SUPPORT.md) for where to ask questions.

Happy hacking — and thanks for making the course better!
