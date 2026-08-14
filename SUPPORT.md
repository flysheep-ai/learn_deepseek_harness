# Getting help with learn-agent-harness

Thanks for using the course! Here are the best ways to get help, in order of
preference.

## 1. Read the docs

- [README.md](README.md) — what the course is, the learning path, and quick start
  (English primary; [中文版](README.cn.md))
- [DESIGN.md](DESIGN.md) — the research findings and design contract behind every
  chapter
- Each chapter's own `README.md` (中文) / `README.en.md` — the lesson text,
  "why it's designed this way", and "try it yourself" exercises

## 2. Ask a question

- **Discussions** — for "how do I understand X?" and general conversation:
  <https://github.com/flysheep-ai/learn_deepseek_harness/discussions>
- **Issues** — for concrete bugs or feature requests (please use the templates):
  <https://github.com/flysheep-ai/learn_deepseek_harness/issues>

## 3. Report a bug

If a chapter's `--demo` fails, please include:

1. The command you ran (e.g. `python3 s18_full_harness/code.py --demo --debug`)
2. Your Python version (`python3 --version`)
3. The full error output (the last ~50 lines are usually enough)

## 4. Report a security issue

Do **not** open a public issue for security reports. See
[SECURITY.md](SECURITY.md).

---

> **Quick sanity check before you ask:** every chapter should run offline with
> no API key:
>
> ```sh
> python3 s01_agent_loop/code.py --demo
> python3 -m unittest discover tests
> ```
>
> If `--demo` works but a real-model run fails, the issue is almost always the
> environment variables (`cp .env.example .env` and fill in the values).
