---
name: mes-share-data
description: Let apps of one **my-easy-software** project share data - shared tables under [tables] and views under [views] in project.toml, [data] writes in app.toml, writing rows with ctx.db() and replace_rows, and keeping the database in line with "mes db sync" and "mes db check". Use when the user wants one app to use another app's data, a combined table or view, or a dashboard over several apps.
---

# Share data between apps

Run `mes guide share-data` and follow it. Check with `mes --json db schema`, `mes --json apps check`
and `mes --json db check`, and tell the user in plain words which app writes which rows and what
the dashboards may read.
