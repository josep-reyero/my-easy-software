# Guide: adding or changing a dashboard

How to show a project's data in a dashboard, preview it, and keep it working on a server.

Dashboards are YAML files in `ui/dashboards/` read by the dashboard server easy-ui (my-easy-ui):
named, read-only queries plus fixed components. No JavaScript, HTML or CSS. See every component
with `easy-ui components`; check files with `easy-ui validate`.

A project shows its own dashboards (`ui/dashboards/<slug>.yaml`) and the ones its apps ship
(`[ui] dashboards` in an `app.toml`, files in the app's `dashboards/` folder). Apps can also add
sections to a project dashboard (`mes guide contribute-panel`). In `project.toml`:

```toml
[ui]
dashboards = ["overview"]               # the order of the project's own dashboards (default: all)
refresh_after = ["notes-main.rebuild"]  # on a server, rebuild the dashboard data after these jobs (ids)
accent = "#2a7f62"                      # optional colour
# tables = ["daily"]                    # optional: narrow what the dashboards may read
```

The data is the project's shared database. What the dashboards may read (the allowlist) is the
shared tables and views of `project.toml`, then each app's `[ui] tables`; a project `[ui] tables`
list replaces it. `mes db schema` prints it.

Steps:
1. Make an app write the rows the dashboard needs: a shared table (`mes guide share-data`) or one
   of its own tables listed under its `[ui] tables`, usually from a job.
2. Add made-up rows to `ui/fixtures/sample.sqlite` (same tables and views, see `mes db schema --sql`)
   so the preview works without real data.
3. Edit or add `ui/dashboards/<name>.yaml` and list it in `[ui] dashboards`.
4. `uv run mes dev`: one link on this computer; the page updates when you save and lists mistakes.
   `easy-ui compose --project-dir . --json` shows the final dashboards and where each part came from.

Only the allowlist ever reaches a dashboard, and only the dashboards' own
queries run on them. On a server the dashboard server runs as its own account
(`mes-<project>-web`), can read only the published copy in `/var/lib/mes/<project>/ui/`, and is
reached through your private network (`mes guide setup`, Tailscale serve). To show dashboards to
other people, see `mes guide share`.
