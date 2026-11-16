# Guide: share data between apps

Shared tables in project.toml, how apps write their rows with ctx.db(), and how views merge them.

Every project has one SQLite database, `<project>.sqlite`, in its data folder. Tables come in two kinds:

- **shared tables**, declared in `project.toml` under `[tables]`: any app that lists one in its
  `[data] writes` adds rows to it. Every row carries `source`, the name of the app that wrote it.
- **app tables**, the app's own: their names start with its prefix (`walks_log` for the app
  `fitlab-walks`), it lists them in `[data] tables`, and only it writes them.

```toml
[tables.activities]
key = ["id"]                                # 1 to 4 columns of type text or integer
columns = { id = "text", date = "text", type = "text", distance_m = "real" }

[tables.days]
key = ["date"]
prefer = ["fitlab-rides"]                   # rows of several apps with the same key are merged
columns = { date = "text", walk_km = "real", ride_km = "real", weather = "text" }

[views.weekly]                              # read-only SQL over the shared tables (SELECT or WITH)
sql = "SELECT strftime('%Y-%W', date) AS week, count(*) AS n FROM activities GROUP BY 1"
```

Column types: `text`, `integer`, `real`, `boolean` (0/1), `json` (text holding JSON). mes stores a
shared table as `<name>__rows` and makes the view `<name>` that everyone reads: every app's rows
side by side, or, with `prefer`, one row per key where each column comes from the first app (in
`prefer` order, then `[project] apps` order) that has a value for it. Dashboards and agents read
views, never `__rows`.

## Writing rows

```python
@app.command("rebuild", kind="write")
def rebuild(ctx):
    rows = [{"id": "walk-1", "date": "2026-01-05", "type": "walking", "distance_m": 4200}]
    with ctx.db() as db:                     # one transaction: readers see all of it or none of it
        db.replace_rows("activities", rows)  # this app's rows only; other apps' rows are untouched
    return {"activities": len(rows)}
```

`replace_rows(table, rows)` swaps all of this app's rows; `upsert_rows` adds or replaces by key;
`delete_rows(table, {"date": "..."})` removes some. `source` is filled in for you. A row with an
unknown column, no key value, a value of the wrong kind or a duplicate key is refused ("The rows
for the shared table days were not written: ... Nothing was changed."). `db.conn` is the plain
connection for the app's own tables. Keep the app's own files as the source of truth and make the
tables from them, so `mes db rebuild` can always rebuild everything.

## Keeping the database in line with project.toml

```bash
mes db schema            # tables, views and the dashboards' allowlist (--sql: the exact script)
mes db sync              # creates what is missing, adds new columns, recreates changed views
mes db sync --reset days # empties and recreates a shared table whose key or column type changed
mes db rebuild           # runs every app's rebuild command (or --app <name>)
mes db check             # differences, objects nobody declares, rows of removed apps (--prune)
```

`mes jobs run` and the server's job units run `mes db sync` before a job that writes data. A change
that would lose data (a removed column, a new type or key) is refused until you `--reset` it.
`mes apps check` checks that every app writes only its declared tables.
