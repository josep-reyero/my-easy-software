---
name: mes-contribute-panel
description: Let an app of a my-easy-software project add its own sections to a project dashboard ([ui.contributes.<slug>] in app.toml, a sections file with a position such as after:<anchor>), or override what apps add (ui/sections/<app>.<slug>.yaml). Use when the user wants an app's chart or numbers to appear on the project's overview, or wants to hide or replace an app's panel.
---

# Contribute a panel to a dashboard

Run `mes guide contribute-panel` and follow it (`mes guide dashboards` for the rest). Preview with
`mes dev`, check the result with `easy-ui compose --project-dir . --json`, and tell the user where
the section now appears.
