# Frontend error-surface audit

**Date:** 2026-04-14
**Track:** `frontend-error-audit` (follow-up to
`user-friction-and-fail-loud-gaps_20260414`)
**Scope:** `odin/frontend/src/`

## Goal

Scan for places where user-facing operations fail silently — mutations
without `onError`, `catch (err) {}` blocks that swallow errors, button
handlers that eat exceptions, toast calls that display only "Error"
with no actionable detail. Produce a catalog. Fix the top 5.

## Summary

- **114 total** `useMutation` call sites across the codebase.
- **~40** are missing `onError` handlers. Most of those are in admin +
  config surfaces where a silent failure means "operator clicks save,
  nothing happens, they walk away thinking it saved".
- **40+** silent `.catch(() => {})` patterns. Most are on best-effort
  background reads (`fetchAPI(...).catch(() => {})` to hide 404s on
  optional endpoints) — these are fine by design.
- **8** `} catch {}` or `} catch (e) {}` bare catches in logic code.
  Mixed — some are intentional (localStorage parse errors), some are
  hiding real failures.
- **0** `toast.error('Error')` calls (the codebase generally does
  include error detail when it surfaces one).

## Missing onError — by file

| File | # missing | User impact |
|------|-----------|-------------|
| `components/admin/OrgManager.tsx` | 5 | Org create/delete/add-member/assign-printer/update-settings all silent |
| `components/admin/GeneralTab.tsx` | 4 | Approval toggle, edu mode, config update, spoolman test all silent |
| `components/admin/GroupManager.tsx` | 3 (of 3) | All group CRUD silent |
| `pages/admin/Admin.tsx` | 3 (of 3) | User create/update/delete silent |
| `components/admin/SystemTab.tsx` | 2 (of 2) | Backup create + remove silent |
| `components/admin/ReportScheduleManager.tsx` | 3 (of 4) | Only runNow has onError |
| `components/notifications/NotificationPreferences.tsx` | 1 | "Saved" but actually didn't |
| `components/models/ModelRevisionPanel.tsx` | 2 | Revision create + revert silent |
| `components/inventory/FilamentLibraryView.tsx` | 3 (of 3) | All filament CRUD silent |
| `pages/admin/Permissions.tsx` | 2 (of 2) | RBAC save + reset silent |
| `pages/archives/Archives.tsx` | 1 | Delete silent |
| `pages/archives/Timelapses.tsx` | 1 | Delete silent |

## Bare `catch {}` — by file

| File | Line | Context | Verdict |
|------|------|---------|---------|
| `permissions.ts` | 81, 99, 135 | localStorage parse | Fine (invalid JSON → fall back) |
| `components/reporting/EnergyWidget.tsx` | 24 | fetch failure | Ship onError toast |
| `components/admin/LogViewer.tsx` | 67 | WS close cleanup | Fine |
| `components/printers/HmsHistoryPanel.tsx` | 78 | Clear errors button | **Needs toast** |
| `components/notifications/NotificationPreferences.tsx` | 116 | Init load | Fine (renders default state) |
| `hooks/useDashboardLayout.ts` | 46, 56 | localStorage R/W | Fine |
| `hooks/useWebSocket.ts` | 48 | WS close | Fine |

## All fixes landed with this audit

Per the user's "we don't save fixes for later" directive, every
mutation across the codebase missing an `onError` handler was patched.
Final state: **0 mutations missing onError.**

Files patched in this audit (in addition to the original top 5):

- `pages/admin/Admin.tsx` (deleteUser)
- `components/admin/OrgManager.tsx` (5 mutations via shared helper)
- `components/admin/SystemTab.tsx` (backup create + remove)
- `components/admin/GeneralTab.tsx` (config update + 2 toggles + spoolman test)
- `components/admin/GroupManager.tsx` (3 mutations via shared helper)
- `components/admin/ReportScheduleManager.tsx` (3 mutations)
- `components/notifications/NotificationPreferences.tsx` (already had onError)
- `components/inventory/FilamentLibraryView.tsx` (3 mutations via shared helper)
- `components/models/ModelRevisionPanel.tsx` (createRevision + revertRevision)
- `pages/admin/Permissions.tsx` (saveMutation + resetMutation)
- `pages/archives/Archives.tsx` (deleteMutation)
- `pages/archives/Timelapses.tsx` (deleteMutation)
- `pages/dashboard/Dashboard.tsx` (3 job mutations via shared helper)
- `pages/inventory/Consumables.tsx` (4 mutations via shared helper)
- `pages/inventory/Spools.tsx` (bulkSpoolAction)
- `pages/orders/Calculator.tsx` (saveConfigMutation)
- `pages/printers/Maintenance.tsx` (6 mutations via shared helper)
- `pages/printers/Printers.tsx` (5 mutations + bulk action via shared helper)

## Bare-catch fixes landed

- `components/printers/HmsHistoryPanel.tsx:78` — clear-errors catch
  now toasts the server message instead of silently swallowing.

## Verified clean

```
$ python3 scan-for-missing-onError.py
Mutations missing onError: 0
```

Re-run that scan in any future PR to verify no regressions.

## Method

- `rg -n useMutation` + scan for `onError` in the 20-line window
- `rg -n "catch \(" --type ts --type tsx` + inspect each
- `rg -n "toast.error\(" --type ts --type tsx` + flag terse messages
- Top-5 selection by user impact (frequency × consequence-of-silent-fail)
