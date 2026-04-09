<!-- bw-standard-version: 3.1.0 -->
# Project Planning Structure

This document describes the buzzwoo standard folder structure for Claude Code projects.

## Directory Layout

```
.claude-bw/
├── context/            # Shared project assets (snippets, docs, references)
├── specs/              # Project specifications (synced from Google/ClickUp Docs)
├── prd/                # Product requirements (PM-owned, WHAT to build)
│   └── {ID-name}/      # e.g., NM-123-user-auth/
├── questions/          # PM handoff questions (from new-cycle workflow)
└── local/              # Gitignored, dev-only
    └── notes/          # Session notes, scratch files

.claude/plans/          # Implementation plan files (Claude Code native)
```

## Folder Reference

| Location | Purpose | Owner | Committed? |
|----------|---------|-------|------------|
| `specs/` | Project specifications (symlinked to central specs repo, or local in legacy mode) | PM | In specs repo (or project repo in legacy mode) |
| `specs/assets/` | Screenshots and images for specifications | PM/Dev | Yes |
| `prd/` | Product requirements - WHAT to build (symlinked to central specs repo, or local in legacy mode) | PM | In specs repo (or project repo in legacy mode) |
| `questions/` | PM handoff questions (symlinked to central specs repo, or local in legacy mode) | Dev → PM | In specs repo (or project repo in legacy mode) |
| `context/` | Shared project assets (symlinked to central specs repo, or local in legacy mode) | Anyone | In specs repo (or project repo in legacy mode) |
| `.claude/plans/*.md` | Implementation plan files (`NM-123-plan.md`) | Dev | Yes |
| `local/notes/` | Session notes, research, scratch files | Anyone | No |

## Plans vs PRD Files

**Plan files** go in `.claude/plans/` (Claude Code native):
```
.claude/plans/
├── NM-123-plan.md          # Implementation steps for task NM-123
├── plan-dark-mode.md       # Implementation steps for dark mode feature
└── ...
```

**PRD files** go in `.claude-bw/prd/{folder}/` (committed, PM-owned):
```
.claude-bw/prd/
├── NM-123-user-authentication/
│   └── requirements.md     # What to build, acceptance criteria
├── feat-dark-mode/
│   └── requirements.md
└── ...
```

## File Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Task plan | `.claude/plans/{TASK-ID}-plan.md` | `.claude/plans/NM-123-plan.md` |
| Feature plan | `.claude/plans/plan-{short-name}.md` | `.claude/plans/plan-dark-mode.md` |
| Bug fix plan | `.claude/plans/fix-{short-name}-plan.md` | `.claude/plans/fix-login-redirect-plan.md` |
| PRD folder (task) | `prd/{TASK-ID}-{short-name}/` | `prd/NM-123-user-auth/` |
| PRD folder (feature) | `prd/feat-{short-name}/` | `prd/feat-dark-mode/` |
| Questions | `questions/{ID}-{name}-questions.md` | `questions/NM-123-auth-questions.md` |

## Key Principles

- **Specs** describe WHAT to build (PM-owned, in central specs repo or synced from external docs)
- **PRD** defines requirements and acceptance criteria (PM-owned, in central specs repo or committed locally)
- **Plans** contain implementation steps with status tracking (committed, shared)
- **Context** holds shared assets anyone on the team can reference (in central specs repo or committed locally)
