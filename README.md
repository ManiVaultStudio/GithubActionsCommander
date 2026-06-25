# GitHub Actions Qt Commander v3

A PySide6 desktop app for dispatching GitHub Actions workflows across many repositories.

## New in v3

- Resizable and movable table columns.
- Checkbox filter: only show repositories with the global branch/pattern.
- Checkbox filter: only show repositories with the selected workflow.
- Global branch supports wildcard patterns such as:

```text
feature/core_revamp_archiving/revamp_archiving_*
```

It first uses shell-style wildcard matching and then falls back to regex matching.
When multiple branches match, the lexicographically last branch is used.

- Validation now marks queued/stuck rows as finished when the validation worker completes.
- Save/load repository bundle JSON files for later reuse.

## Setup

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Set a GitHub token:

```powershell
$env:GITHUB_TOKEN="ghp_xxx"
```

Copy and edit config:

```powershell
copy config.example.yaml config.yaml
```

Run:

```powershell
python app.py
```

## Typical workflow

1. Click **Refresh**.
2. Enter the global branch or wildcard pattern.
3. Click **Validate visible**.
4. Enable either:
   - **Only repos with global branch/pattern**
   - **Only repos with workflow**
5. Click **Select visible**.
6. Click **Save bundle** if this repo set should be reused.
7. Click **Run selected**.

## Bundle format

Bundles are JSON files:

```json
{
  "organization": "ManiVaultStudio",
  "workflow": "build.yml",
  "global_branch": "feature/core_revamp_archiving/revamp_archiving_*",
  "selected_repositories": [
    {
      "full_name": "ManiVaultStudio/core",
      "branch_override": ""
    }
  ]
}
```


## v4 fix

Branch names containing `/` are now URL-encoded for GitHub branch checks and compare calls. This matters for branches such as:

```text
feature/core_revamp_archiving/revamp_archiving
```

The branch/pattern filter still depends on validation results. After changing the global branch/pattern, click **Validate visible**. If the strict branch/workflow filters hide all rows before validation, **Validate visible** now validates all repositories instead of doing nothing.


## v5 stability fix

Validation of large repository sets is now more robust:

- every worker creates and closes its own `httpx.Client` inside the worker thread;
- the main GUI thread no longer shares one HTTP client with background workers;
- validation reports progress with a progress bar;
- validation adds a small pause every 20 repositories to reduce GUI event pressure and GitHub secondary rate-limit risk.

This should fix crashes during validation of large organization-wide selections.


## v6 CI status redesign

This version separates validation state from CI state.

- `Status` means the application state, for example `Ready`, `Validating`, `Dispatching`.
- `Latest CI` means the latest GitHub Actions result for the selected workflow and branch, for example `success`, `failure`, `queued`, or `in_progress`.

`Poll once` and continuous polling now rediscover the latest workflow run for each repository every cycle. This means:

- after reopening the app, `Validate visible` + `Poll once` will find an existing failed/successful CI run;
- if someone re-runs CI in GitHub while the app is open, the app switches to the newer run automatically;
- polling can be used as a dashboard, not only for runs launched by this app.


## v7 thread-affinity fix

This version removes GUI-updating lambdas connected directly to worker-thread signals and replaces them with `@Slot` methods on the main window. This avoids Qt's:

```text
QObject: Cannot create children for a parent that is in a different thread
```

The crash was caused by starting follow-up polling from the dispatch worker's thread instead of the GUI thread.


## v8 sorting

- Click any column header to sort ascending/descending.
- Columns are still resizable and movable.
- Sorting is smart for:
  - `Latest CI`: failures are sorted before running/queued/success/unknown;
  - `Status`: error/invalid states sort before ready/idle;
  - `Run ID`: numeric sorting;
  - branch/workflow check columns.
- Sort column and direction are saved with `QSettings`.
- `Auto-sort during updates` controls whether polling/validation updates immediately reapply the active sort.
