---
name: disciplined-development
description: Use this skill whenever the user asks to implement, refactor, debug, review, or modify code in this repository. It enforces worktree usage, patch boundaries, low coupling, minimal diffs, and test discipline.
---

# Disciplined Development Skill

Use this skill for all non-trivial development tasks.

## Core Behavior

Before editing code, always produce a short implementation boundary:

1. Goal
2. Current branch/worktree status
3. Files likely to change
4. Files forbidden to change
5. Expected tests
6. Coupling risks
7. Patch size risk

If the task is too broad, do not implement immediately. Propose a smaller patch.

## Worktree Discipline

Prefer one worktree per task.

Before editing:
- Check current branch.
- Avoid editing main/master.
- Prefer a feature branch named after the task.
- Do not mix unrelated tasks in one worktree.

Example branch names:
- `feat/market-mapping-rules`
- `fix/report-generation`
- `test/transmission-backtest`
- `refactor/signal-boundaries`

## Patch Policy

A patch is acceptable only if it:
- has one clear purpose
- touches a small number of files
- does not mix unrelated changes
- does not rewrite large files without reason
- preserves public APIs unless explicitly approved
- includes relevant tests or explains why tests are not possible

Refuse broad patches that:
- change architecture without a design step
- modify unrelated files
- combine bugfix, feature, refactor, and formatting
- silently change data contracts
- delete tests instead of fixing them

## Low Coupling Rules

Prefer:
- domain logic independent from CLI/UI
- external data loading separated from business rules
- mapping tables separated from execution logic
- pure functions for scoring and filtering
- explicit types or schemas for input/output data

Avoid:
- business logic inside command-line argument parsing
- hardcoded file paths deep inside core logic
- direct network calls inside scoring code
- global state
- circular imports
- giant utility modules
- one function doing loading + scoring + reporting + writing

## Development Flow

Follow this sequence:

1. Inspect relevant files.
2. Summarize current design.
3. State the edit boundary.
4. Make the smallest useful change.
5. Add or update tests.
6. Run focused tests.
7. Run broader tests if cheap.
8. Summarize changed files and risks.

## Stop Conditions

Stop and ask for narrower scope if:
- more than 5-8 files need changes
- the task requires an architecture decision
- public APIs need breaking changes
- tests reveal unrelated failures
- the requested patch would reduce maintainability

## Final Report

Always end with:

- Summary
- Files changed
- Tests run
- Coupling impact
- Risks
- Suggested next step
