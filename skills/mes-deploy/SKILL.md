---
name: mes-deploy
description: Put a my-easy-software project on the user's own Linux server and keep it updated - "mes server plan" (a root script the user runs once with sudo), "mes server check", signed "mes deploy", rollback, and backups with "mes backup". Use when the user wants their project to run all the time, on a server, VPS or home server, or to update, roll back or back it up.
---

# Deploy to a server

Run `mes guide server` (first time) and `mes guide deploy` (every update), and follow them;
`mes guide backup` for backups. mes never uses sudo: show the user the plan script, let them read
it and run it themselves; the plan is made with the system-installed mes, never `uv run mes`.
Two keys: a signing key (best a hardware key) and a separate login key. Start every deploy with
`mes --json deploy <server> --dry-run`, and after the first one run `mes server selftest`.
