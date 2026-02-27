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

1. Split the work into small, reviewable logical chunks.
2. Treat on-disk file contents as the single source of truth and read the target file with exact line numbers before each chunk.
3. For each chunk, do exactly one `emacs.submit_apply_patch` call (primary path):
   - exactly one file (`path`)
   - exactly one logical change (one coherent intent)
4. Use `emacs.submit_diff` only as a fallback (for example, user explicitly provides/requests raw unified diff format).
5. Prefer multiple submissions for one file when changes are logically distinct; do not force one monolithic patch per file.
6. If a submission fails, regenerate from current on-disk contents; do not keep editing a previously failed patch/diff blindly.
7. Submit all chunks required to complete the current task before pausing.
8. After all required chunks are submitted, stop and wait for the human to review/apply/save/finalize in Emacs (unless the user explicitly asks you to pause earlier).

## `emacs.submit_apply_patch` (primary write path)

- Prefer this tool for normal code changes.
- Write `patch` exactly in `apply_patch` envelope format:
  - `*** Begin Patch`
  - one file block (`*** Update File: ...` or `*** Add File: ...` or `*** Delete File: ...`)
  - patch body hunks
  - `*** End Patch`
- Keep one call = one file + one logical intent.
- Ensure tool argument `path` matches the file referenced in the patch header.

## Submission description requirements

The `description` must be detailed and review-friendly. Write it like an implementation plan + rationale, so the human can review without context switching.

Use this structure (adapt as needed, but keep it concrete):

- **Goal**: what problem this chunk solves and why now.
- **Approach**: the design/strategy you chose and why it is the best, correct option.
- **Implementation walkthrough**: step-by-step through the code and execution, assume low familiarity with a given language / technology stack
- **Behavior / edge cases**: what this does for tricky inputs or failure modes.

## Submission rules (hard constraints)

- One submission call (`emacs.submit_apply_patch` or fallback `emacs.submit_diff`) == one file and one small logical change.
- The same file may be submitted multiple times when changes are split into logical review hunks.
- `path` must be repo-relative (no absolute paths, no `..`).
- Keep hunks tight and reviewable; split large work into multiple submissions.
- Ensure patch/diff text ends with a newline.
- Do not run extra pre-submit dry-run checks; submit and use server feedback for corrections.


## Submission recovery loop (strict)

1. Classify the exact error from `emacs.submit_apply_patch` (or fallback `emacs.submit_diff`).
2. For apply-patch format errors: re-read on-disk file with exact line numbers, regenerate patch from scratch, and resubmit smaller logical hunks.
3. Malformed diff/hunk errors (fallback `emacs.submit_diff` path): regenerate from scratch with fresh context and smaller hunks.
4. Path errors: fix `path` to repo-relative form and resubmit.
5. Size-limit errors: split description/patch/diff into smaller logical submissions and resubmit.
6. `emacs_unreachable`: ask user to start/restart bridge; retry only when ready.
7. `root_mismatch`: stop and ask user to align roots before continuing.
8. Unknown server errors: retry once with fresh on-disk context; if it fails again, report exact error and next fix.
9. Continue until accepted or blocked by user-required action.

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
- Mutating repo files via local edits/commands instead of MCP submission tools.
- Using `emacs.submit_diff` as the default path when `emacs.submit_apply_patch` is applicable.
