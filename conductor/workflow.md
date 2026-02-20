# Conductor Evaluate-Loop Workflow

## Overview

The Conductor system uses a 5-step Evaluate-Loop to ensure quality implementation.

```
PLAN → EVALUATE PLAN → EXECUTE → EVALUATE EXECUTION → (FIX if needed)
  1          2            3              4                    5
```

## Steps

### Step 1: PLAN (`/loop-planner`)
- Reads spec.md in the track directory
- Loads project context
- Produces `plan.md` with phased tasks, acceptance criteria, and dependencies

### Step 2: EVALUATE PLAN (`/loop-plan-evaluator`)
- Verifies plan before any code is written
- Checks scope alignment, overlap, DAG validity, task clarity
- Outputs **PASS** (proceed to Step 3) or **FAIL** (back to Step 1)

### Step 3: EXECUTE (`/loop-executor`)
- Implements tasks from `plan.md` sequentially
- Updates `plan.md` task markers after each task
- Commits at checkpoints
- Uses TDD where applicable

### Step 4: EVALUATE EXECUTION (`/loop-execution-evaluator`)
- Dispatches specialized evaluator based on track type:
  - `ui` / `design-system` / `screens` → eval-ui-ux
  - `feature` / `refactor` / `infrastructure` → eval-code-quality
  - `integration` / `auth` / `payments` / `api` → eval-integration
  - `business-logic` / `generator` / `core-feature` → eval-business-logic
- Outputs **PASS** (track complete) or **FAIL** (proceed to Step 5)

### Step 5: FIX (`/loop-fixer`)
- Triggered only on FAIL verdict from Step 4
- Creates fix tasks in `plan.md`
- Executes fixes
- Triggers re-evaluation (loops back to Step 4)

## Track Directory Structure

```
conductor/tracks/{track-id}/
├── spec.md       # Requirements and acceptance criteria
├── plan.md       # Execution plan with task checklist
└── metadata.json # Track state, type, loop step
```

## Quick Reference

| Command | Description |
|---------|-------------|
| `/go <goal>` | Auto-create and start a track |
| `/conductor:new-track` | Create a new track interactively |
| `/conductor:status` | Show current track status |
| `/conductor:implement` | Run the full loop on current track |
| `/loop-planner` | Step 1: Create execution plan |
| `/loop-plan-evaluator` | Step 2: Evaluate the plan |
| `/loop-executor` | Step 3: Implement the plan |
| `/loop-execution-evaluator` | Step 4: Evaluate the implementation |
| `/loop-fixer` | Step 5: Fix evaluation failures |
