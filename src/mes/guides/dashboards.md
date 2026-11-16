# Guide: adding or changing a dashboard

How to show a project's data in a dashboard, preview it, and keep it working on a server.

Dashboards are YAML files in `ui/dashboards/` read by the dashboard server easy-ui (my-easy-ui):
named, read-only queries plus fixed components. No JavaScript, HTML or CSS. See every component
with `easy-ui components`; check files with `easy-ui validate`.

What a dashboard may read is set in `project.toml`:

```toml
[ui]
data = "notes.sqlite"          # the app's SQLite file, in its data folder
tables = ["daily"]             # only these tables are copied for the dashboards
dashboards = ["main"]          # the files ui/dashboards/<name>.yaml (default: all)
refresh_after = ["rebuild"]    # on a server, rebuild the dashboard data after these jobs succeed
accent = "teal"                # optional colour
```

Steps:
1. Make the app write a derived table with the columns the dashboard needs (a `write` command,
   usually run by a job). Add the table to `[ui] tables`.
2. Add made-up rows for it to `ui/fixtures/sample.sqlite` so the preview works without real data.
3. Edit or add `ui/dashboards/<name>.yaml` and list it in `[ui] dashboards`.
4. `uv run mes dev`: one link on this computer; the page updates when you save and lists mistakes.

Only tables listed under `[ui] tables` ever reach a dashboard, and only the dashboards' own
queries run on them. On a server the dashboard server runs as its own account
(`mes-<project>-web`), can read only the published copy in `/var/lib/mes/<project>/ui/` and its
own sign-in folder `/var/lib/mes/<project>/auth/` (which the project's own code cannot reach), has
no network of its own, and is reached through your private network (`mes guide setup`, Tailscale serve). To show dashboards to
other people, see `mes guide share`.
