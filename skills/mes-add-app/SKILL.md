---
name: mes-add-app
description: Add an app to an existing my-easy-software project with "mes new-app" (apps/<package>/ with app.toml, cli.py, AGENTS.md and a dashboard), or bring in an app installed from elsewhere, then "uv sync" and "mes apps sync". Use when the user wants a second tool, data source or tracker inside the same project, or asks how apps, short names and apps.lock.json work.
---

# Add an app

Run `mes guide add-app` and follow it (`mes guide apps` explains app.toml and the lock). After
`mes new-app <name>`, run `uv sync`, then `mes --json apps sync` and `mes --json apps check`, and
tell the user in plain words how to reach the new app (`<project> <short> <command>`).
