# docs/

Supplementary reading for `learn-agent-harness`. The course itself is 18
runnable chapters; this directory holds the **theoretical** side of the story —
specifically, the formal paper behind deepseek-harness's most distinctive ideas.

## What's here

| File | What it is | Language |
|---|---|---|
| [`cordis-paper-spatiotemporal-composability.md`](cordis-paper-spatiotemporal-composability.md) | A guided reading of the Cordis paper, mapping every concept back to a chapter (s13/s14/s15) | 中文 |
| [`paper/A Programming Paradigm for Spatiotemporal Composability.pdf`](paper/A%20Programming%20Paradigm%20for%20Spatiotemporal%20Composability.pdf) | The 88-page paper itself (Peking University, DeepSeek-AI) | English |

## Why the Cordis paper matters

Most harnesses are engineering folklore — a pile of "we do it this way because
it works". deepseek-harness is unusual: its core abstractions (plugins, the
context, reversible registration, reactive dependencies) are backed by a
**formal paper**. That paper is what makes "everything is a plugin" mean
something *precise* instead of something fashionable.

The reading notes distill the paper into its two big ideas and draw an explicit
side-by-side with this course:

- **Revertible effects** — registration returns its own inverse; composite
  cleanup is automatic. → the course's `on()/use()/register()` disposers (s14).
- **Reactive coeffects** — dependency changes are classified
  (`activating / deactivating / neutral`) and drive activation. → the course
  only does *load-time* `require`; the notes explain what's missing and why
  that's the minimal correct version to add later.

The notes' **section 九** ("和我们的 learn-deepseek-harness 对照") is the most
useful single page: a table mapping Cordis → this course, including what was
deliberately **not** ported (fiber lifecycles, intercept, the confluence
theorem).

## Suggested reading path

1. Finish chapters **s13 → s14 → s15** (event bus → plugin system → capability
   seams). These three are the course's minimal teaching version of the paper.
2. Read the notes **sections 一–五** (the two ideas and the unified context).
3. Read the notes **section 九** (the Cordis ↔ course mapping).
4. Skim sections 六–七 (the component calculus + metatheory) only if you want
   the formal guarantees — the intuitions are enough for the course.

> The paper is the "why"; the chapters are the "how". Read the chapters first,
> then come back here to see the machinery they are a miniature of.
