---
name: emacs-mcp
description: Interact with the codebase and propose code changes via emacs MCP tools
---

# emacs-mcp (Human-in-the-loop editing)

## What changes in this workflow

- Everything is normal agent work: read code, reason, plan, run checks/tests when useful.
- The only difference: when you need to modify files, you do it via MCP tool calls, not by editing the repo directly.
- A human reviews in Emacs, applies changes to disk, saves buffers, and finalizes per file.

## Session start (must do first)

1. Call `emacs.ping`.
2. If `emacs_unreachable`: ask the user to start the Emacs bridge (`M-x emacs-mcp-start`) and select the repo root directory.
3. If `root_mismatch`: stop and ask the user to align roots:
   - restart Emacs bridge with the correct root, and/or
   - restart Codex from the same repo root directory.
4. Only proceed when ping returns `{"ok": true, "status": "ready", ...}`.

## Making changes (how to write)

When you are ready to modify code:

1. Split the work into small, reviewable chunks.
2. For each chunk, do exactly one `emacs.submit_diff` call:
   - exactly one file (`path`)
   - exactly one logical change (one coherent intent)
3. After submitting, stop and wait for the human to review/apply/save/finalize in Emacs.

## `emacs.submit_diff` description requirements

The `description` must be detailed and review-friendly. Write it like an implementation plan + rationale, so the human can review without context switching.

Use this structure (adapt as needed, but keep it concrete):

- **Goal**: what problem this chunk solves and why now.
- **Approach**: the design/strategy you chose and why it is the best, correct option.
- **Implementation walkthrough**: step-by-step through the code and execution, assume low familiarity with a given language / technology stack
- **Behavior / edge cases**: what this does for tricky inputs or failure modes.

## Diff rules (hard constraints)

- One `emacs.submit_diff` call == one file and one small diff.
- `path` must be repo-relative (no absolute paths, no `..`).
- Keep hunks tight and reviewable; split large work into multiple submissions.
- Ensure the diff text ends with a newline.

## `emacs.get_selection` (discussion-only context)

Use `emacs.get_selection` only when it helps discussion:

- when the user highlights code and asks for explanation
- when you need to quote/anchor a discussion to an exact region

Do not use it as the patch base (patch base is saved on-disk files).

## Feedback (only when asked)

Do not read feedback unless the user explicitly asks you to (e.g. “pull feedback”, “check feedback”, “what did I finalize?”).

When asked:

1. Call `emacs.feedback_list`.
2. Call `emacs.feedback_get` for the relevant ids (note: it consumes on read).
3. Analyze `applied_diff` + `user_message` and update your understanding so future diffs stay consistent.

## Avoid / invariants

- Proceeding past `root_mismatch` (results become invalid).
- Mutating repo files via local edits/commands instead of `emacs.submit_diff`.
