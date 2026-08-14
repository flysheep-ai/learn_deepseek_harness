<!--
Thanks for your contribution! Please fill this in and delete the HTML comments.

This is an educational project — see CONTRIBUTING.md and DESIGN.md before
submitting. In particular, the harness must never script the model's thinking
("Model decides. Harness enables."), and every chapter stays self-contained.
-->

## Summary

<!-- What does this PR change and why? Link any related issue: Fixes #123 -->

## Type of change

- [ ] Bug fix
- [ ] New chapter / section / exercise
- [ ] Documentation improvement
- [ ] Test addition or fix
- [ ] Tooling / CI / repo housekeeping

## Checklist

- [ ] `make test` passes (`python3 -m unittest discover tests`)
- [ ] `make lint` passes (`ruff check .`)
- [ ] Every chapter's `python sXX_*/code.py --demo` still runs offline
- [ ] New mechanisms are covered by a deterministic test in `tests/`
- [ ] The change respects **"Model decides. Harness enables."**
- [ ] No auto-formatter was run over the chapter `code.py` files
- [ ] Documentation is updated where behavior/layout changed

## How to verify

<!-- Steps for a reviewer to reproduce the change, e.g. a command and expected output. -->
