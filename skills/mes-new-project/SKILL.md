---
name: mes-new-project
description: Create a new **my-easy-software** project (a project with one app in apps/, a shared database, jobs and a working dashboard) with "mes new". Use when the user wants to start a new personal app, tracker, logger or dashboard project, or asks how a mes project is laid out (project.toml, AGENTS.md, ui/dashboards).
---

# Create a new project

Run `mes guide new-project` and follow it. Create the project with `mes --json new <name>`,
then do its `next_steps` with the user, ending with `uv run mes dev` so they see the dashboard.
Keep the repo free of personal information.
