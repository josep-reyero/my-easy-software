# Guide: adding a command to an app

How to give a project's app a new command that people and agents can use safely.

An app's commands are in `apps/<package>/cli.py`, built on the App SDK:

```python
from mes import App, UsageError

app = App("notes-main")   # the app's name, as in its app.toml [app] name

@app.command("summary", kind="read")
def summary(ctx, days: int = 7):
    """Key numbers for the last days."""      # the help text
    if days < 1:
        raise UsageError("--days must be 1 or more.", hint="For example: --days 7")
    return {"days": days}                      # the return value is the data
```

Rules:
- Every command has a kind: `read` (only looks), `write` (changes data) or `admin` (changes
  setup). Pick the lowest kind that is true. A session with `MES_ALLOW=read` cannot run write or
  admin commands (exit 4); the gate never runs admin commands.
- Arguments come from the function signature: no default = positional, default = `--option`,
  `bool` = `--flag/--no-flag`. `Annotated[int, "help text"]` adds help.
- Return data (dicts, lists of rows, text). Never print it. With `--json` stdout holds only JSON.
- Errors: raise `mes.errors` types (`UsageError`, `NotFound`, `Refused`, `TemporaryFailure`, ...)
  with a message that says what happened and why, and a `hint` that says what to do next.
- Needing a person: return `mes.protocol.needs_input(...)` or `mes.protocol.needs_human(...)`.
  Never prompt on the terminal.
- Files: use `ctx.app_data_dir` (this app's own folder inside the project's data folder) for its
  raw files, and `ctx.state_dir` for caches and cursors. Both are outside the repo. Raw data is
  never changed. Tables go into the project's shared database through `ctx.db()` (shared tables
  with `db.replace_rows(...)`, the app's own tables with `db.conn`; see `mes guide share-data`);
  they must always be rebuildable from the files. On a server the data folder is
  `/var/lib/mes/<project>/data`.
- Keep `@app.schema` and `@app.sources` up to date so agents can discover the data, and update
  `AGENTS.md` (the table of commands).

Every app has built-in `describe`, `guide`, `schema` and `sources`. Check the result with
`uv run <project> <short> --json describe` (or `uv run <app> --json describe`). A command used by a
job must be `read` or `write`
(`mes jobs list` checks it).
