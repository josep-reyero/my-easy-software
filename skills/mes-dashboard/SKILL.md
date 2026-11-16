---
name: mes-dashboard
description: Add, change or restyle a dashboard of a my-easy-software project (ui/dashboards/*.yaml for easy-ui, the [ui] sections of project.toml and app.toml, sample data in ui/fixtures, the look in ui/theme.yaml and ui/styles/*.css) and preview it with "mes dev". Use when the user wants to see their data as charts, tables or numbers, wants the dashboards to look different (colours, fonts, a theme, "make it look like ..."), or a dashboard shows nothing or an error.
---

# Add or change a dashboard

Run `mes guide dashboards` and follow it. Preview with `uv run mes dev` and give the user the one
link it prints. Only the shared tables and views and the apps' `[ui] tables` reach dashboards.

To change how the dashboards look, follow "Change the look" in that guide: `easy-ui themes` and
`easy-ui theme use NAME` (ui/theme.yaml), stylesheets in `ui/styles/*.css` using the names
`easy-ui style-guide` lists, `id:` on a component to style just that one. Check with
`easy-ui validate` and `easy-ui screenshot <dashboard>`.
