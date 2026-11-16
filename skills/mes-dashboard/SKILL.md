---
name: mes-dashboard
description: Add or change a dashboard of a my-easy-software project (ui/dashboards/*.yaml for easy-ui, the [ui] section of project.toml, sample data in ui/fixtures) and preview it with "mes dev". Use when the user wants to see their data as charts, tables or numbers, or a dashboard shows nothing or an error.
---

# Add or change a dashboard

Run `mes guide dashboards` and follow it. Preview with `uv run mes dev` and give the user the one
link it prints. Only tables listed under `[ui] tables` reach dashboards.
