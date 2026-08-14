# s08 — Skill Loading

**中文版：[README.md](README.md)**

[s07](../s07_prompt_assembly/) → **s08** → [s09](../s09_subagent/) → … → s18

> Should knowledge be stuffed into the prompt up front, or handed over when the
> model asks for it?
>
> The answer determines whether your agent can grow a tenth capability.

---

## The problem the last chapter left

s07 lets you add sections freely. So the natural move is:

```python
@prompts.section("git_guide", 60)
def _git_guide(ctx): return open("guides/git.md").read()        # 3000 chars
@prompts.section("python_style", 61)
def _py_style(ctx): return open("guides/python.md").read()      # 2500 chars
@prompts.section("debugging", 62)
def _debug(ctx): return open("guides/debugging.md").read()      # 2000 chars
```

Then you notice two things:

**1. Every request pays for them.**
The user asks "what's TIMEOUT in config.py" — nothing to do with git workflows
— and those 7500 chars go out anyway. Ten blocks = 30,000 chars per request.

**2. Worse: attention dilutes.**
It's not just money. The model must find the relevant 500 chars inside 30,000.
The more you stuff in, the more easily irrelevant content pulls it off track.

---

## What this chapter solves

**Progressive Disclosure**:

```
Always in the prompt (cheap)        Loaded on demand (expensive)
┌──────────────────────────┐      ┌──────────────────────────┐
│ Available skills:        │      │ # Systematic debugging     │
│ - git-workflow: git rules│ ───▶ │ 1. Reproduce: run it once…│
│ - python-style: code rules│     │ 2. Read the full error…   │
│ - debugging: how to debug│      │ …                        │
│         196 chars        │      │        494 chars          │
└──────────────────────────┘      └──────────────────────────┘
                                   appears only after skill("debugging")
```

The demo's first part does the math:

```
Stuffing everything in: 1610 chars / request
Catalog only:           196 chars / request  (12%)
```

Real-world skill bodies run thousands of chars; the ratio gets far steeper.

---

## The new core concepts

### 1. Skill: a two-part structure

```python
@dataclass(frozen=True)
class Skill:
    name: str
    description: str      # ← these two live in the prompt permanently
    path: Path

    @property
    def body(self) -> str:            # ← this is loaded on demand
        return _strip_frontmatter(self.path.read_text()).strip()
```

On disk, a directory + a Markdown file with frontmatter:

```
skills/
  debugging/SKILL.md
  git-workflow/SKILL.md
  python-style/SKILL.md
```

```markdown
---
name: debugging
description: Systematic method for failing tests and runtime errors. Read when tests fail or an error is unclear.
---

# Systematic debugging
...
```

**`body` is a property, not a field** — not a style choice. Read the body into
memory at construction and 10 skills become 10 resident copies; "progressive"
keeps only the name.

### 2. description is routing information for the model

```python
if not desc:
    print(f"[skill] skipping {name}: missing description")
    continue
```

A skill without a description is **invisible** to the model — it can't judge
when to use it.

So the description matters. Compare:

```
❌ description: about debugging
✅ description: Systematic method for failing tests and runtime errors. Read when tests fail or an error is unclear.
```

The good one states the **trigger condition**. The model decides whether to
load based on this sentence — it is the routing table of this knowledge.

### 3. SkillRegistry: a sibling of ToolRegistry, different job

```
ToolRegistry    what the model can do   (action space)
SkillRegistry   how the model should do (knowledge)
```

Turning knowledge into tools is a common design mistake: every piece of
knowledge then occupies a tool-schema slot, and the model must "invoke" to
read. **Skills are content; tools are capabilities.**

We have exactly one `skill` tool (the way to fetch); skills themselves are not
tools.

### 4. The body rides the inbox, not the tool result

```python
def run_skill(name: str) -> str:
    ...
    INBOX.put(f"[Loaded skill: {name}]\n\n{body}", source="skill")
    return f"Loaded skill {name} ({len(body)} chars); it will enter the context next step."
```

The tool returns **only a confirmation**; the body enters the context on the
next step. Why the detour?

**Because the semantics differ.** A tool result is a `role:"tool"` message the
model reads as an **observation** ("I ran X and got Y"). Skill content is an
**instruction** — it should carry the weight of something a human said.

`--debug` shows the path clearly:

```
[step 1]  claimed=0
    ← model reply     tool_calls=1 [skill]
    · tool result     skill ok 44B          ← just the confirmation, 44 bytes
[step 2]  claimed=1 (skill)                 ← the body is claimed here
    → model request   messages=8 system=517chars
```

And it **reuses the inbox mechanism built in s06** — no new channel invented.
Skill content, background-job notices (s12), subagent results (s09)… all these
"things the harness wants the model to know" differ in source but share one
entry point.

---

## Minimal architecture diagram

```
  skills/*/SKILL.md
        │ discover()
        ▼
  ┌──────────────┐    catalog()    ┌──────────────────┐
  │SkillRegistry │────────────────▶│ PromptSection    │──▶ system prompt
  │              │  name + desc    │ "skills"（permanent）│      （cheap）
  └──────┬───────┘                 └──────────────────┘
         │
         │ get(name).body            the model calls skill("debugging")
         │        ▲                          │
         │        └──────────────────────────┘
         ▼
      Inbox.put(body, source="skill")
         │
         ▼  claimed by the next step
      user/message（SURFACE）──▶ enters the model's context
```

---

## Run it

```sh
python s08_skill_loading/code.py --demo
python s08_skill_loading/code.py --demo --debug
```

The demo contrasts two questions:

**Question A (no skill needed)**: "what's TIMEOUT in config.py?"
→ the model just `read`s it. `loaded skills: (none)`. **Not a char wasted.**

**Question B (skill needed)**: "tests are failing, debug it our team's way"
→ the model sees `debugging` in the catalog and **decides on its own** to call
`skill("debugging")`, then follows step 1 of the skill ("reproduce first").

Note the words "decides on its own". Nowhere in the harness is there a line
that says "if the user mentions tests, load the debugging skill".

---

## Why it's designed this way

### Why the catalog must **always** be present

Progressive disclosure has two halves; drop either and it dies:

- The catalog must **always** be there — otherwise the model never thinks to ask
- The body must be **on demand** — otherwise the catalog means nothing

The hint line in the section is also required:

```
Below is on-demand knowledge. **Only titles are listed** — use the skill tool to load the full text.
```

Without it, the model takes the catalog for the whole content and guesses the
details from a one-line description.

### Why loaded_skills doesn't reset per turn

```python
rt.files_read = []          # resets per turn
# loaded_skills does NOT reset
```

Because once injected, the skill body stays in the context **permanently** —
it's a `user/message` the log never erases. Resetting would only invite the
model to reload the same content.

`files_read` is different: it's just a "don't re-read this turn" hint, so a
per-turn reset makes sense.

**A state's reset period should match that state's actual lifetime** — never
one-size-fits-all.

### Why duplicate loads are blocked

```python
if name in RT.loaded_skills:
    return f"Skill {name} is already loaded; its content is above. Don't reload."
```

Models do re-request the same skill (especially in long sessions where they
forget they read it). Unblocked, the same 500 chars appear in the context three
times.

Note the block message is **actionable**: "the content is above" tells the
model to look back instead of retrying under another name.

### Is this the harness thinking for the model?

No — and the boundary is worth reading closely:

```python
# ✅ harness: put the catalog out, provide the skill tool
@prompts.section("skills", 45)
def _skills(ctx): return "Available skills:\n- debugging: …"

# ❌ harness must NOT:
if "测试" in user_input:
    load_skill("debugging")
```

The harness provides **capabilities and information**; "when to use which
knowledge" is the model's judgment.

Write that `if` and you've replaced the model's judgment with keyword matching
— "this test case won't pass" fails to match.

---

## What changed vs. the previous chapter

| | s07 | s08 |
|---|---|---|
| Where knowledge lives | all stuffed into prompt sections | **catalog permanent, body on demand** |
| Knowledge cost per request | full (1610 chars) | catalog (196 chars, 12%) |
| Who decides what to load | — | **the model** |
| New objects | — | `Skill` / `SkillRegistry` |
| New tool | 6 | **7** (+`skill`) |
| New event | — | `skill/load`（log-only） |
| Path of the body into context | — | inbox injection → `user/message` |

---

## What real systems do on top

- **Multiple providers**: real harnesses' `ctx.skills` can mount local
  directories, bundled packages, remote registries; reads merge the layers and
  "nearest layer wins" on name collision. We have one local directory.
- **Per-agent skill sets**: different agents see different catalogs (a
  subagent typically needs one or two). s09 does something similar, but there
  it restricts tools.
- **Executable resources**: real SKILL.md often bundles scripts, templates, and
  reference files; loading a skill mounts a whole directory into the workspace.
  We load only the body.
- **Invalidation & caching**: catalog changes notify consumers to refetch. We
  re-`catalog()` every step — simple, and the dict is in memory so it's cheap.
- **The same idea elsewhere**: file content can be progressively disclosed too
  — `read(limit=50)` gives the first 50 lines, the model asks for more if it
  matters. MCP tool directories and codebase symbol indexes are the same
  pattern.

---

## Try it yourself

1. **Write your own skill**
   ```sh
   mkdir -p s08_skill_loading/skills/sql-review
   ```
   Write the frontmatter, re-run the demo, watch it appear in the catalog.
   **You changed zero lines of code.**

2. **Delete a description**
   Watch the skill get skipped with a warning. Ask yourself: why is a
   skill-without-description worse than a skill that doesn't exist?
   (Because it's on disk, and you believe it works.)

3. **Compare the token cost of both approaches**
   Turn all three skills into permanent sections (`@prompts.section` returning
   `body`), run the "what's TIMEOUT" question, compare `system=NNNchars` in
   `--debug`.

4. **Route the body through the tool result**
   Change `run_skill` to `return body`, drop the inbox injection. It works —
   but check `--debug`: it's now a `role:"tool"` message. Would the model obey
   "I executed a tool and got this text" the same as "someone told me this
   rule"?

5. **Watch the routing effect**
   Change `debugging`'s description to something vague ("debugging related"),
   re-run question B with a real model. It probably won't load the skill.

---

## Next chapter

Now a new problem. Suppose the user says:

> "Which files in this codebase handle authentication?"

The model will `grep`, `glob`, and `read` a dozen files. Those search results —
60,000 tokens of raw file content in real projects — **all pour into the main
context and stay there forever**.

The final answer the model needs is one sentence: "auth lives in
auth/middleware.py and api/deps.py."

But that 60,000-token pile follows you through the whole session:

- every later step re-sends it (**money**)
- every later step the model searches inside it (**attention**)
- the context overflows soon (**s10's problem**)

Can "searching" happen in **another context**, with only the conclusion brought
back?

And if so — should that other agent have the same powers as the main one?

→ [s09 — Subagent](../s09_subagent/)
