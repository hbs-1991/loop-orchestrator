---
name: parallel-plan-execution
description: Use when asked to implement or execute a written multi-task plan in parallel, fan out plan tasks across subagents, run a workflow over a plan, or speed up plan execution with concurrent agents in a shared git repo.
---

# Parallel Plan Execution

## Overview

Run a written implementation plan by fanning its tasks across subagents with the `Workflow` tool — but **parallelism in a shared repo is bounded by file-disjointness and git serialization, not by task count.**

Core principle: partition the plan into **streams that touch disjoint file sets**; run those streams concurrently; serialize everything that shares a file — including git itself. Then verify independently before you commit.

A plan with 9 tasks is rarely 9-way parallel. It is usually 2–3 independent streams (often one per module/slice), each internally sequential, plus a serial cleanup + docs + gate at the end.

## When to use

- The user says "implement this plan in parallel," "run a workflow over the plan," "fan out subagents," "go fast with concurrent agents."
- You have a concrete written plan (tasks + per-task file lists) and the `Workflow` tool is available.

**When NOT to use:** a single-stream plan where every task touches the same files (just run it sequentially — `superpowers:executing-plans`); a plan with no file lists (do the file-overlap analysis first, or it can't be partitioned safely); trivial 1–2 task work.

## Procedure

1. **Map tasks → files.** Read the plan. For each task, list the files it creates/edits/deletes.
2. **Build the dependency graph.** Note code dependencies (task B imports task A's new symbol) and delete-after-replace ordering (a "retire X" task must follow the tasks that build X's replacement).
3. **Partition into streams.** Two tasks go in the **same** stream if they share any file OR have a code dependency. Streams whose file sets are fully disjoint can run in parallel. Within a stream, one agent runs its tasks **in order** (shared files demand a single writer).
4. **Pull out barrier tasks.** Anything touching more than one stream's files (integration, "retire/delete the old flow," cross-cutting refactor) runs **serially after** the parallel phase.
5. **Choose an isolation strategy** (see table below).
6. **Run the parallel phase**, then the serial barrier tasks (deletion/integration), then a **final serial phase**: full quality gate + full affected test suite — this is the first time everything exists together, so cross-stream issues surface here. Fix them.
7. **Verify independently, then commit.** Do not trust agent self-reports — re-run the gate and tests yourself. Then commit in logical groups (see Common Mistakes about commit granularity).

## Isolation strategy

| Strategy | Use when | How it works |
|---|---|---|
| **Shared tree, no git in agents** (DEFAULT) | Parallel streams touch disjoint files and you'll commit afterward | Agents edit the shared working tree directly; they run only **focused** checks on their own files; they run **no git**. You (orchestrator/main loop) run the full gate and create all commits after the workflow returns. Simplest; no worktree cost. |
| **Worktree-per-stream + integrate agent** | Each stream must keep its own commit history, or streams can't be made file-disjoint | Pass `opts.isolation:'worktree'` per stream; each commits on its own branch. A **serial integrate agent** merges the branches (the JS orchestrator cannot run git itself). Heavier (~200–500ms + disk per worktree) and conflicts if streams overlap. |

Default to the shared-tree path. Reach for worktrees only when independent per-stream commits are a hard requirement.

## Agent prompt recipe

Every stream/phase agent prompt must contain:
- **Authoritative source:** the path to the plan + spec file, and exactly which task numbers to implement ("implement Tasks 6 and 7 of `<plan path>`"). Don't re-paste large code; tell them to follow the plan's blocks verbatim and adjust drifted line numbers by content.
- **Exact file scope:** "touch ONLY these files / this module. Do NOT touch `<other streams' files>`, composition, or READMEs." Disjoint scope is what makes parallelism safe.
- **Project HARD rules** relevant to the work (layer contracts, error types, logging).
- **Focused verification only** (shared-tree path): run `pytest <your test files>` + `ruff check`/`ruff format` + `mypy` **scoped to your module**. Explicitly forbid: any `git` command, repo-wide `ruff format .`, `lint-imports`, `mypy --strict src/` — the orchestrator runs those once at the end.
- **A structured return** (status pass/fail, files changed, the verification output observed).
- **Pinned model** (`opts.model`): implementer agents → `claude-opus-5` (Opus 5); the post-implementation Advisor review agent → `claude-fable-5` (Fable 5). See "Model selection" below.

## Model selection (REQUIRED for the Workflow path)

When fanning out via the `Workflow` tool, pin models explicitly with `opts.model` — do NOT let agents inherit the session model:

- **Implementer agents → Opus 5** (`opts.model: 'claude-opus-5'`). Every parallel stream/phase agent that writes or edits code uses Opus 5. It is fast and strong enough for scoped, plan-driven implementation, and keeps the fan-out cheap.
- **Advisor agent → Fable 5** (`opts.model: 'claude-fable-5'`). After implementation (per stream, or once over the combined result before the final gate), run a dedicated **Advisor** review agent on Fable 5 to adversarially check the implemented code against the plan + project HARD rules and report findings. Its job is review, not writing — it returns a structured verdict; the orchestrator (or a follow-up implementer agent) applies fixes.

Put the Advisor step **after** the parallel implement phase (a barrier), so it sees the code all streams produced. The final serial quality gate + full test suite still runs after the Advisor's findings are addressed.

## Workflow skeleton (shared-tree default)

```javascript
export const meta = { name: '...', description: '...', phases: [
  { title: 'Implement' }, { title: 'Advisor' }, { title: 'Retire' }, { title: 'Docs & Gate' },
]}

const IMPLEMENTER = 'claude-opus-5'     // implementer agents
const ADVISOR = 'claude-fable-5'        // post-implementation review

phase('Implement')
const [a, b] = await parallel([           // streams with DISJOINT file sets
  () => agent(streamAPrompt, { phase: 'Implement', schema: RESULT, model: IMPLEMENTER, agentType: '...' }),
  () => agent(streamBPrompt, { phase: 'Implement', schema: RESULT, model: IMPLEMENTER, agentType: '...' }),
])

phase('Advisor')                           // barrier: Fable 5 reviews the combined result
const review = await agent(advisorPrompt, { phase: 'Advisor', schema: FINDINGS, model: ADVISOR })
// address review.findings (orchestrator or a follow-up Opus 5 implementer agent) before the gate

phase('Retire')                            // barrier: deletes/integration touching both
const r = await agent(retirePrompt, { phase: 'Retire', schema: RESULT, model: IMPLEMENTER })

phase('Docs & Gate')                       // docs + FULL gate + FULL test suite, fix red
const g = await agent(docsAndGatePrompt, { phase: 'Docs & Gate', schema: GATE, model: IMPLEMENTER })
return { a, b, review, r, g }
```

Use `parallel()` only for the fan-out where you need all streams before the barrier. Use `pipeline()` when items flow through stages without a synchronizing barrier. Keep git out; commit yourself after it returns.

## Common mistakes

- **Fanning out tasks that share a file** → concurrent edits clobber each other. Group them into one sequential stream (one writer per file).
- **Letting parallel agents `git commit` in a shared tree** → `.git/index.lock` races and corrupt commits. Serialize git: either no-git-in-agents (default) or worktree-per-stream.
- **Running repo-wide `ruff format .` / a whole-repo codemod inside a parallel agent (shared tree)** → it rewrites another agent's in-flight files, causing stale-read edit failures. Focused checks per stream; full gate only in the final serial phase.
- **Deleting/retiring old code in the parallel phase** → the replacement may not exist yet in a sibling stream. Run "retire X" as a barrier task after the streams.
- **Trusting agent self-reports** → an agent may report "all green" optimistically or have run a narrower check than claimed. Re-run the full gate + tests yourself before committing.
- **Expecting an exact per-task commit history** → parallel execution can't preserve a strict sequential commit graph. Commit in logical groups (e.g. per module) and tell the user that's why.
- **Vague agent scope** → without "ONLY these files," agents wander into shared files and reintroduce contention.

## Quick reference

| Plan shape | Orchestration |
|---|---|
| N modules, tasks cluster by module, modules disjoint | One parallel stream per module; barrier for cross-module cleanup; serial docs+gate |
| One module, tasks chain on shared files | Single sequential stream — don't use this skill; use `superpowers:executing-plans` |
| Independent tasks + a final "retire old flow" | Parallel the independents; retire as a serial barrier after them |
| Streams must each ship independent commits | Worktree-per-stream + serial integrate agent |

**REQUIRED BACKGROUND:** `superpowers:executing-plans` (single-stream plan execution) and `superpowers:dispatching-parallel-agents` (when independent work is genuinely parallel).
