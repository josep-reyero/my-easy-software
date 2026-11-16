# Guide: add an app to a project

Make a new app inside a project with `mes new-app`, install it, and reach it from the project's command.

```bash
cd my-project
mes new-app rides            # the app my-project-rides, in apps/my_project_rides/
uv sync                      # installs its program (my-project-rides)
mes apps sync                # the project finds it (apps.lock.json)
uv run my-project rides add "First ride"
uv run mes jobs run my-project-rides.rebuild
```

`mes new-app <name>` takes a short name (`rides`) or the full name (`my-project-rides`). It writes
`apps/<package>/` with `__init__.py`, `app.toml`, `cli.py` (commands `items`, `add`, `rebuild`),
`AGENTS.md` and `dashboards/<short>.yaml`, and changes two lines: it adds the package to the one-line
`apps = [...]` of `project.toml`, and the app's program and folder to `[project.scripts]` and
`packages = [...]` of `pyproject.toml`. When a line is not in that simple form, the file is left as
it is and you get the exact line to add. It never overwrites a file and never writes through a link.
It refuses names that are taken, reserved (`describe`, `guide`, `apps`, `help`) or whose table prefix
would overlap another app's.

What the new app has (see `mes guide apps` for every key of `app.toml`):

- its own files in `ctx.app_data_dir` (`<data folder>/<short>/`), the source of truth;
- its own table `<prefix>items` in the project's database, rebuilt by `rebuild` with `ctx.db()`;
- a manual job `<app>.rebuild`, and a dashboard listed under `[ui] dashboards`.

To write rows that other apps and the project's dashboards read, add the table to `project.toml`
`[tables]` and to the app's `[data] writes` (see `mes guide share-data`). To add a part to the
project's overview dashboard, see `mes guide contribute-panel`.

An app can also come from outside the project: add it as a dependency in `pyproject.toml` (for
example `{ path = "vendor/<app>" }` under `[tool.uv.sources]`), list its package under
`[project] apps`, then `uv sync` and `mes apps sync`. mes never imports its code; it only runs its
program.
