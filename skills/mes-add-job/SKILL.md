---
name: mes-add-job
description: Add or change a job in a **my-easy-software** project (the [jobs] of an app's app.toml, with ids like <app>.<job>, and [settings.<app>] in project.toml) that runs on a schedule, after another job, on request or from a signed webhook, with network and memory limits. Use when the user wants something to happen automatically, every day or hour, after a sync, or when an outside service calls in.
---

# Add a job

Run `mes guide jobs` and follow it (for webhook jobs also `mes guide webhooks`). Check with
`mes --json jobs list`, try it with `mes --json jobs run <app>.<job>`, and tell the user in plain words
what the job does, when it runs, and what network it may use.
