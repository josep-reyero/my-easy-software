# Guide: creating a new project

How to create a project whose app stores data and whose dashboard works right away.

Get `mes` first if `mes --version` does not work (see `mes guide setup`).

Create it: `mes --json new <name>` (2 to 31 lowercase letters, digits and dashes, starting with a
letter; at most 24 if it will run on a server). It makes a project with one app, `<name>-main`
(`--app <short>` picks another short name):

- `project.toml`: the project: `[project]` with its `apps`, the shared table `daily` under
  `[tables]`, and `[ui]` (its dashboards, and which job refreshes them on a server).
- `pyproject.toml`: how uv installs it: the project's command (`<name>`) and the app's program.
- `apps/<module>_main/`: the app: `app.toml` (its tables and its job `rebuild`), `cli.py` (`add`
  saves a note, `notes` shows them, `rebuild` writes the notes per day into `daily`) and `AGENTS.md`.
- `AGENTS.md`: how an agent uses the project (`<name> guide` prints it with each app's guide).
- `ui/dashboards/overview.yaml`: the overview dashboard; it reads the view `daily`, and apps may
  add sections to it (`mes guide contribute-panel`).
- `ui/fixtures/sample.sqlite`: made-up data made with the project's own schema, so the dashboard
  shows something at once.
- `.gitignore`: keeps `.venv/`, `apps.lock.json` and stray data files out of git.

Then, inside the new folder:

```
uv sync                                  # a private Python setup in .venv/
uv run mes apps sync                     # finds the apps, writes apps.lock.json
uv run <name> main add "First note"      # data goes to ~/.local/share/mes/<name>/, never into the folder
uv run mes jobs run <name>-main.rebuild  # fills the shared table the dashboard reads
uv run mes dev                           # one link to the dashboard, on this computer only
```

`mes dev` needs easy-ui (**my-easy-ui**). It runs `mes apps sync`, shows the sample data until the
project has its own database, rebuilds the dashboard data when it changes, and reports mistakes in
`project.toml` and `app.toml` as you save. Stop it with Ctrl-C.

`project.toml` in short:

```toml
[project]
name = "notes"            # also the folder on the server and the accounts mes-notes, mes-notes-web
title = "My notes"
version = "0.1.0"
apps = ["notes_main"]     # the apps: Python packages in apps/ or installed (see: mes guide apps)

[tables.daily]            # a shared table (see: mes guide share-data)
key = ["day"]
columns = { day = "text", notes = "integer" }

[ui]                      # see: mes guide dashboards
dashboards = ["overview"]
refresh_after = ["notes-main.rebuild"]

[storage]                 # optional: files kept by my-bucket-storage (mybucket)
bucket = "files"
```

Unknown keys are errors, so a typo cannot switch a limit off. Next: `mes guide add-app` to add an
app, `mes guide commands` to add commands, `mes guide dashboards` for the dashboard,
`mes guide deploy` to put it on a server.
