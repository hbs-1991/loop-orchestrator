---
name: writing-plans
description: Use when writing an implementation plan from a spec, before any code is touched
---

# Writing Plans

<!--
Adapted from superpowers:writing-plans for the loop-orchestrator planning sandbox.
Substance is unchanged; what was removed is everything that assumes a human in the
loop or skills that do not exist here: the announce-at-start line, the execution
handoff dialogue, the git-worktree note, and the dated default filename (the
planning run dictates the exact plan path). Keep those omissions when updating
this copy from upstream.
-->

## Overview

Write a comprehensive implementation plan assuming the engineer has zero context for this
codebase and questionable taste. Document everything they need to know: which files to touch
for each task, the code, the tests, the docs they might need to check, how to verify it. Give
them the whole plan as bite-sized tasks. DRY. YAGNI. TDD. Frequent commits.

Assume they are a skilled developer who knows almost nothing about this toolset or problem
domain, and does not know good test design very well.

**The plan is executed by an autonomous agent with no way to ask you anything.** Every gap you
leave becomes a guess it makes on its own.

**Save the plan to the exact path you were given.** Do not invent a filename.

## Scope Check

If the spec covers multiple independent subsystems, say so in the plan's Goal section and
sequence the subsystems so each one produces working, testable software on its own.

## File Structure

Before defining tasks, map out which files will be created or modified and what each one is
responsible for. This is where decomposition decisions get locked in.

- Design units with clear boundaries and well-defined interfaces. Each file should have one
  clear responsibility.
- Prefer smaller, focused files over large ones that do too much — edits are more reliable in
  code that fits in context at once.
- Files that change together should live together. Split by responsibility, not by technical layer.
- In an existing codebase, follow the established patterns. Do not unilaterally restructure; but
  if a file you are modifying has grown unwieldy, including a split in the plan is reasonable.

This structure informs the task decomposition. Each task should produce self-contained changes
that make sense independently.

## Bite-Sized Task Granularity

**Each step is one action (2-5 minutes):**
- "Write the failing test" — step
- "Run it to make sure it fails" — step
- "Implement the minimal code to make the test pass" — step
- "Run the tests and make sure they pass" — step
- "Commit" — step

## Plan Document Header

**Every plan MUST start with this header:**

```markdown
# [Feature Name] Implementation Plan

> **For agentic workers:** implement this plan task by task, in order. Steps use checkbox
> (`- [ ]`) syntax — tick them off in this file as you go. If the `parallel-plan-execution`
> skill is available and the tasks split into file-disjoint streams, use it.

**Goal:** [One sentence describing what this builds]

**Architecture:** [2-3 sentences about the approach]

**Tech Stack:** [Key technologies/libraries]

---
```

## Task Structure

````markdown
### Task N: [Component Name]

**Files:**
- Create: `exact/path/to/file.py`
- Modify: `exact/path/to/existing.py:123-145`
- Test: `tests/exact/path/to/test.py`

- [ ] **Step 1: Write the failing test**

```python
def test_specific_behavior():
    result = function(input)
    assert result == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/path/test.py::test_name -v`
Expected: FAIL with "function not defined"

- [ ] **Step 3: Write minimal implementation**

```python
def function(input):
    return expected
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/path/test.py::test_name -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/path/test.py src/path/file.py
git commit -m "feat: add specific feature"
```
````

Use the project's own test runner, build tool and commit conventions — read them out of the
repository (CLAUDE.md, CI workflows, package manifests) instead of assuming.

## No Placeholders

Every step must contain the actual content an engineer needs. These are **plan failures** —
never write them:

- "TBD", "TODO", "implement later", "fill in details"
- "Add appropriate error handling" / "add validation" / "handle edge cases"
- "Write tests for the above" (without actual test code)
- "Similar to Task N" (repeat the code — the engineer may read tasks out of order)
- Steps that describe what to do without showing how (code blocks required for code steps)
- References to types, functions or methods not defined in any task

## Remember

- Exact file paths always
- Complete code in every step — if a step changes code, show the code
- Exact commands with expected output
- DRY, YAGNI, TDD, frequent commits

## Self-Review

After writing the complete plan, look at the spec with fresh eyes and check the plan against it.
This is a checklist you run yourself.

1. **Spec coverage:** skim each requirement in the spec. Can you point to a task that implements
   it? Add a task for every gap.
2. **Placeholder scan:** search the plan for the red flags in "No Placeholders" above. Fix them.
3. **Type consistency:** do the types, signatures and property names used in later tasks match
   what earlier tasks defined? `clearLayers()` in Task 3 and `clearFullLayers()` in Task 7 is a bug.
4. **Runnability:** every command in the plan must be one that actually works in this repository.

Fix issues inline and move on — no second review pass.
