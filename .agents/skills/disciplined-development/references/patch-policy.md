# Patch Policy

## Good Patch

A good patch:
- solves one problem
- has a clear reason
- is easy to review
- has tests or validation
- avoids unrelated cleanup

## Bad Patch

A bad patch:
- rewrites large files
- touches unrelated modules
- changes behavior silently
- changes formatting everywhere
- renames things without need
- mixes feature + refactor + bugfix
- weakens tests
- hides failures

## Required Before Editing

Before editing, state:
- why this patch is needed
- why the selected files are in scope
- what is explicitly out of scope
