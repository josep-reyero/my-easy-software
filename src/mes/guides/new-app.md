# Guide: creating a new app

How to create an app and give it commands that people and agents can use.

Get `mes` first if `mes --version` does not work: `uv tool install --editable <the my-easy-software
folder>` (see `mes guide setup`).

Create it: `mes --json new <name>` (name: 2 to 31 lowercase letters, digits and dashes, starting
with a letter). It creates `app.toml`, a CLI with one read and one write command, `AGENTS.md`, one
dashboard and `pyproject.toml` (plus a `.gitignore`). Then `uv sync` inside the new folder, and
use `uv run <name> ...` and `uv run mes ...` there. Creating the app next to the my-easy-software
folder keeps the path to it short, so no home-folder path ends up in the app's files.

The CLI is built on the App SDK:

```python
from mes import App

app = App("myapp")

@app.command("summary", kind="read", help="Key numbers for the last days.")
def summary(ctx, days: int = 7):
    return {"days": days}          # the return value is the data

if __name__ == "__main__":
    app.main()
```

Rules for commands:
- Every command has a kind: `read` (only looks), `write` (changes data) or `admin` (changes
  setup). Pick the lowest kind that is true. Sessions with `MES_ALLOW=read` cannot run write or
  admin commands (exit 4).
- Arguments come from the function signature: no default = positional, default = `--option`,
  `bool` = `--flag/--no-flag`.
- Return data (dicts, lists of rows, text). Never print it yourself. With `--json` stdout holds
  only the JSON data; without it mes prints a readable table or text.
- For errors raise `mes.errors` types (`NotFound`, `Refused`, `UsageError`, ...) with a message
  that says what happened and why, and a `hint` that says what to do next, in plain words.
- If the command needs a person, return `mes.protocol.needs_input(...)` or
  `mes.protocol.needs_human(...)`. Never prompt on the terminal.
- Use `ctx.data_dir` and `ctx.state_dir` for files. They live outside the repo, on this computer.
  Raw data is never modified; derived tables must always be rebuildable from it.
- Register `@app.schema` and `@app.sources` so agents can discover the data.

Every app has built-in `describe` (commands, kinds and arguments) and `guide` (its `AGENTS.md`).
Start any session with `<app> --json describe`.

Keep the repo free of personal information: no real names, emails, host names, home paths or
tokens. Per-machine settings go in `~/.config/mes/config.toml` under `[apps.<name>]`.
