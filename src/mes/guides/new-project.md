# Guide: creating a new project

How to create a project whose app stores data and whose dashboard works right away.

Get `mes` first if `mes --version` does not work (see `mes guide setup`).

Create it: `mes --json new <name>` (2 to 31 lowercase letters, digits and dashes, starting with a
letter; at most 24 if it will run on a server). It creates:

- `project.toml`: what the project is: `[project]`, its app under `[apps.<name>]`, a job
  (`rebuild`), and `[ui]` (the SQLite file and the tables dashboards may read).
- `pyproject.toml`: how uv installs it.
- `src/<module>/cli.py`: the app's command-line tool. `add` (write) saves a note in SQLite,
  `notes` (read) shows them, `rebuild` (write) builds the derived table `daily`.
- `AGENTS.md`: how an agent uses the project (`<app> guide` prints it).
- `ui/dashboards/main.yaml`: the main dashboard; it reads the table `daily`.
- `ui/fixtures/sample.sqlite`: made-up data, so the dashboard shows something at once.
- `.gitignore`: keeps `.venv/` and stray data files out of git.

Then, inside the new folder:

```
uv sync                          # a private Python setup in .venv/
uv run <name> add "First note"   # data goes to ~/.local/share/mes/<name>/, never into the folder
uv run mes jobs run rebuild      # builds the table the dashboard reads
uv run mes dev                   # one link to the dashboard, on this computer only
```

`mes dev` needs easy-ui (my-easy-ui). It shows the sample data until the project has its own data
file, rebuilds the dashboard data when that file changes, and reports mistakes in `project.toml`
as you save. Stop it with Ctrl-C.

`project.toml` in short:

```toml
[project]
name = "notes"            # also the folder on the server and the accounts mes-notes, mes-notes-web
title = "My notes"
version = "0.1.0"

[apps.notes]              # one or more apps; each is a console script in pyproject.toml
cli = "notes"

[jobs.rebuild]            # see: mes guide jobs
run = "notes rebuild"     # the app, then its command
manual = true

[ui]                      # see: mes guide dashboards
data = "notes.sqlite"
tables = ["daily"]
dashboards = ["main"]
refresh_after = ["rebuild"]

[storage]                 # optional: files kept by my-bucket-storage (mybucket)
bucket = "files"
```

Unknown keys are errors, so a typo cannot switch a limit off. Next: `mes guide commands` to add
commands, `mes guide dashboards` for the dashboard, `mes guide deploy` to put it on a server.

Keep the repo free of personal information: no real names, emails, host names, home paths or
tokens. Per-computer settings go in `~/.config/mes/config.toml` under `[projects.<name>]`
(for example `data_dir = "..."` to keep the data elsewhere).
