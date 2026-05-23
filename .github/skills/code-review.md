---
name: code-review
description: Pre-PR code review for branch changes. Use when user wants to review changes before opening a PR, asks to check their branch for issues, or mentions code review. Analyzes diff against base branch for code quality, bugs, security vulnerabilities, logical fallacies, and style/convention violations. Provides detailed analysis with file:line references and improvement suggestions.
---

# Code Review Skill

Review code changes on a local branch before opening a PR. Uses multiple specialized agents with confidence-based scoring to catch real bugs while minimizing false positives.

**Philosophy**: It's better to flag a potential issue that turns out to be fine than to miss a real bug. Err on the side of caution for error handling, edge cases, and failure modes.

## When to Use

Activate when user:

- Asks to review changes before opening a PR
- Wants a code review of their current branch
- Says things like "review my changes", "check my code", "ready for PR"

## Review Process

Follow these steps precisely:

### Step 1: Eligibility Check (Haiku Agent)

Use a Haiku agent to check if the branch is eligible for review:

- Is the branch the same as the base branch (nothing to review)?
- Are there any uncommitted changes that should be committed first?
- Are there any changes at all?

If not eligible, explain why and stop.

### Step 2: Gather CLAUDE.md Files (Haiku Agent)

Use a Haiku agent to find relevant CLAUDE.md files:

- The root CLAUDE.md file (if one exists)
- Any CLAUDE.md files in directories whose files were modified

These files contain project-specific guidelines that should inform the review.

### Step 3: Summarize Changes (Haiku Agent)

Use a Haiku agent to:

1. Run `git diff origin/main...HEAD` (or appropriate base branch)
2. Run `git log --oneline $(git merge-base HEAD origin/main)..HEAD`
3. Return a summary of what these changes do

### Step 4: Launch 7 Parallel Review Agents (Sonnet)

Launch 7 parallel Sonnet agents to independently review the changes. Each returns a list of issues with reasons:

**Agent #1: CLAUDE.md Compliance Audit**

- Audit changes to ensure they comply with CLAUDE.md guidelines
- Note: CLAUDE.md is guidance for writing code, so not all instructions apply during review

**Agent #2: Multi-Depth Bug Scan**

Read the full content of every changed file (not just the diff). Scan for bugs at multiple depths:
- **Surface level**: Typos, wrong variable names, missing returns, copy-paste errors
- **Logic level**: Trace through conditionals and loops with concrete example inputs
- **Failure level**: For each operation, ask "what if this fails?" - null returns, exceptions, promise rejections
- **Edge case level**: Empty inputs, boundary values, malformed data, single-item collections
- **Cross-function level**: When a changed function calls another function in the same file or in a directly changed file, read that callee's implementation to verify: (a) the return value / error contract is handled by the caller; (b) async functions that return `false`/`null` on failure are not silently ignored; (c) any state the callee sets internally is not assumed to have propagated by the time the caller continues.

**Invisible communication channels**: Look for cases where one piece of code signals a result and the receiving side ignores it. Ask: does this function return a value that conveys success/failure? Is it checked? Does this async operation set state that a caller then reads immediately (before awaiting)? Are there event emitters with no listeners, or promises created but not awaited?

**Incomplete coverage of a known input space**: When code handles a set of variants (regex patterns, switch/if-else chains, union type branches, event type handlers, HTTP status ranges), ask: what is the *complete* set of possible inputs at runtime, and does this code handle all of them? Common gaps: a text-processing regex that handles single-quoted strings but not double-quoted; a status-code handler that covers 4xx but not 5xx; a type union handler that handles two of three variants.

**Guard correctness for the actual type space**: When you see a conditional guard, ask whether it correctly excludes every case the author intends to exclude, given how the language actually works. Common wrong assumptions: `typeof x === 'object'` also passes for `null` and arrays; `!value` rejects `0` and `""` as well as `null`/`undefined`; `x == null` catches both `null` and `undefined` (which can be intentional or a bug depending on context).

**Refactoring regressions**: When code is structurally reorganized (component split, function extracted, call site changed), verify that specific non-default values — props with explicit values, config options set to non-defaults, thresholds, timeouts, limits — that were present in the old version still appear in the new version, or their absence is clearly intentional. The structural change is easy to verify correct; the dropped detail is easy to miss.

**Agent #3: Historical Context Analysis**

- Read git blame and history of modified code
- Identify bugs in light of historical context
- Check what patterns existed before and if changes break them

**Agent #4: Related Code Review**

- Read related files that might be affected by changes
- Check for patterns in sibling files that should be followed
- Look for inconsistencies with existing code

**Agent #5: Code Comment Compliance**

- Read code comments in modified files
- Ensure changes comply with guidance in existing comments
- Check TODOs, FIXMEs, and documentation comments

**Agent #6: Failure Mode and Execution Scope Analysis**

- For async operations: Is rejection/error handled? Could this cause unhandled promise rejection?
- For external calls/APIs: What happens on timeout, network failure, or unexpected response?
- For parsing code: What happens with malformed, truncated, or unexpected input?
- For resource acquisition: Are resources properly released on error paths?
- Ask: "If I were trying to break this code, what input would I use?"

**Execution scope mismatch**: Ask "where does this code actually run?" for anything that looks like it might execute at a broader or earlier scope than intended:
- Module-level code (outside functions/classes): does it instantiate workers, open connections, register globals, or make network requests? These fire for every importer unconditionally.
- Code in component render bodies that should be inside `useMemo`/`useCallback` (recomputed every render).
- Subscriptions or timers set up outside `useEffect` (no cleanup, run during SSR, etc.).
- Constructors or class field initializers that perform I/O or expensive work that should be deferred to an explicit `init()` call.
- Code that assumes it runs once but is placed where it can run multiple times (e.g. inside a loop or a callback that fires repeatedly).

**Agent #7: Test Coverage Gaps**

- For new code paths: Is there a test that exercises this specific path?
- For conditional branches: Are both/all branches tested?
- For error handling code: Are error paths tested, not just happy paths?
- For edge cases in the code: Is there a corresponding test case?
- For parsing/regex: Are all input format variations tested?

### Step 5: Confidence Scoring (Parallel Haiku Agents)

For each issue found in Step 4, launch a parallel Haiku agent to score confidence (0-100):

**Scoring Rubric (give to agents verbatim):**

- **0**: Not confident at all. False positive that doesn't stand up to light scrutiny, or pre-existing issue.
- **25**: Somewhat confident. Might be real, but may be false positive. Couldn't verify it's real. Stylistic issues not explicitly in CLAUDE.md.
- **50**: Moderately confident. Verified as real issue, but might be nitpick or rare in practice. Not very important relative to rest of changes.
- **75**: Highly confident. Double-checked and verified as likely real issue that will be hit in practice. Current approach is insufficient. Very important or directly mentioned in CLAUDE.md.
- **100**: Absolutely certain. Double-checked and confirmed as definitely real issue that will happen frequently. Evidence directly confirms this.

For CLAUDE.md issues, the scoring agent must double-check that the CLAUDE.md actually calls out that issue specifically.

### Step 6: Filter by Confidence

Remove any issues with score < 70. If no issues meet this threshold, report that no significant issues were found.

### Step 7: Generate Report

Output the review in this format:

---

## Code Review

**Branch:** `{branch}` -> `{base}`
**Files Changed:** {count} files (+{added}/-{removed} lines)

### Issues Found

{For each issue with score >= 70:}

**{N}. {Brief description}** (Score: {score})

- **Location:** `{file}:{line_start}-{line_end}`
- **Reason:** {CLAUDE.md says "..." / Bug due to ... / Historical context shows ...}
- **Details:** {Explanation of the issue}

```{language}
{Relevant code snippet}
```

**Suggested Fix:**

```{language}
{Improved code}
```

---

Or if no issues found:

---

## Code Review

**Branch:** `{branch}` -> `{base}`
**Files Changed:** {count} files

No significant issues found. Checked for:

- CLAUDE.md compliance
- Logic bugs and edge cases
- Failure modes (error handling, async rejections)
- Test coverage gaps
- Historical context violations
- Code comment compliance

---

## False Positives to Ignore

These should NOT be flagged (give to review agents):

- Pre-existing issues not introduced in this branch
- Something that looks like a bug but isn't
- Issues linters/typecheckers/compilers catch (formatting, types, imports)
- General code quality issues unless explicitly in CLAUDE.md
- Issues called out in CLAUDE.md but silenced with lint ignore comments
- Intentional functionality changes related to the broader change
- Real issues on lines not modified in this branch
- Pure style nitpicks unrelated to correctness

## Issues to Flag Even If They Seem Minor

These SHOULD be flagged even if they might seem like nitpicks:

- Missing error handling for async operations (unhandled promise rejections can crash Node.js)
- Missing edge case handling in parsing/validation code
- Incorrect capture group usage in regex
- Test coverage gaps for new code paths
- Potential null/undefined access without guards
- Resource leaks on error paths

## Notes

- Do not check build signal or attempt to build/typecheck - assume CI handles this
- Use git commands to examine changes, not GitHub API
- Make a todo list first
- Cite and link each issue (if referring to CLAUDE.md, reference it)
- Focus on actionable issues that genuinely improve code quality
- For complex logic (regex, parsers, state machines): DO perform deep semantic analysis
- When in doubt about error handling or edge cases, flag it - false positives are acceptable
- Reference: [Google's Code Review Guidelines](https://google.github.io/eng-practices/review/reviewer/looking-for.html)

## Git Commands Reference

```bash
# Get current branch
git branch --show-current

# Get base branch
git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main"

# Get diff against base
git diff origin/main...HEAD

# Get changed files
git diff --name-only origin/main...HEAD

# Get diff stats
git diff --stat origin/main...HEAD

# Get commit log for branch
git log --oneline $(git merge-base HEAD origin/main)..HEAD

# Get git blame for a file
git blame {file}

# Get history for a file
git log --oneline -10 -- {file}
```

## Invocation Examples

This skill activates for:

- "Review my changes"
- "Code review before PR"
- "Check my branch for issues"
- "Review this before I open a PR"
- "Pre-PR review"
- "/pre-pr-code-review"