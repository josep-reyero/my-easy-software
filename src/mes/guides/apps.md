# Guide: apps in a project

What an app is, how a project lists its apps, and how mes finds them (apps.lock.json).

A **project** is what you deploy and the only security boundary: one account, one data folder,
one shared database, one set of keys. An **app** is a Python package with an `app.toml` next to its
`__init__.py`. A project lists its apps in `project.toml`:

```toml
[project]
name = "fitlab"
title = "Fit lab"
apps = ["fitlab_walks", "fitlab_rides"]   # Python package names, in order
```

An app lives either in the project (`apps/<package>/`, the usual case) or is installed into the
project's environment by `uv sync` (a dependency in `pyproject.toml`, for example from
`vendor/<app>/` or a published package). Its `app.toml`:

```toml
[app]
name = "fitlab-walks"          # the app's name; its short name is "walks" (the project's name removed)
title = "Walks"
cli = "fitlab_walks.cli:app"   # the mes.App in its code; or command = "<program>" for any program

[data]
folder = "walks"               # its own folder inside the project's data folder (default: the short name)
writes = ["activities"]        # shared tables of project.toml it adds rows to (see: mes guide share-data)
tables = ["walks_log"]         # its own tables; they start with its prefix "walks_"
rebuild = "rebuild"            # the command that rebuilds what it wrote, from its own files

[jobs.rebuild]                 # its jobs; their ids are "fitlab-walks.rebuild" (see: mes guide jobs)
run = "rebuild"
manual = true

[ui]
dashboards = ["walks"]         # dashboards/walks.yaml in the app's folder
tables = ["walks_log"]         # its own tables the dashboards may read
```

Names: the **short name** is the app name without `<project>-` (`walks`). The **table prefix** is the
short name with `-` changed to `_`, plus `_` (`walks_`). Two apps of a project may not share a name,
a short name, a folder, or prefixes where one starts with the other.

## The list of installed apps

`mes apps sync` finds every app (in `apps/`, or installed, by asking the project's own Python where
the package is, without importing it), asks each app's program `--json describe`, checks every job
against the commands, and writes `apps.lock.json` in the project folder. Everything else (jobs, the
gate, the dashboards, the project command) reads that file. It holds paths of this computer, so it
is never committed (`.gitignore`) and a deploy makes it again on the server.

When it is missing or out of date (for example after editing `project.toml`) and an app is
installed outside `apps/`, you are told: "project.toml changed after the list of apps
(apps.lock.json) was written ..." Run `mes apps sync`. When every app is in `apps/`, an out-of-date
or missing lock is simply not needed: the apps are read from `apps/` directly.

When the project's shared database already exists, `mes apps sync` also brings it in line with
`project.toml` (as `mes db sync` does), so the shared views always show the rows of exactly the
listed apps.

## Removing an app

Take its package out of `[project] apps`, and remove what still names it in `project.toml`: a
`prefer` entry, its `[settings.<app>]` and its job ids in `[ui] refresh_after` (one message lists
every place). Then run `mes apps sync`: its jobs, its dashboards and sections, and its rows in the
shared views are gone. The rows stay in the database file until `mes db check --prune` removes
them, so adding the app back shows them again.

```bash
mes apps list      # the apps, where each one is, its jobs
mes apps sync      # find them and write apps.lock.json (after uv sync, or after changing a toml file)
mes apps check     # a checklist: tables, prefixes, programs, commands, the database, dashboards
```

## The project command

`pyproject.toml` gives the project one command, `<project> = "mes.project:main"`, plus each app's
own program. `<project> <short> <command>` runs a command of that app (`fitlab walks add ...`);
`<project> describe`, `<project> guide` and `<project> apps` describe the whole project. With one
app, `<project> <command>` works too (with a note). See `mes guide add-app` to add one.

## From the old one-file form

`project.toml` with `[apps.<name>]`, `[jobs.<name>]` or `[ui] data = "..."` is refused with a plain
message. Move each app into `apps/<package>/` with an `app.toml`, move its jobs into that file
(drop the app's name from `run`), list the packages under `[project] apps`, and declare shared
tables under `[tables]`. Then run `uv sync` and `mes apps sync`.
