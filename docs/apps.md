# Apps: proper packages inside a project

Status: design, agreed direction, not built yet. Where this differs from `v1-contract.md`
(which describes apps as only a named command), this file is the newer decision.

The model is close to Django's: a **project** is the thing you deploy and the security boundary;
an **app** is a self-contained package that plugs into a project and brings its own commands,
jobs, tables and dashboards.

## Project and app

| | Project | App |
|---|---|---|
| What it is | one folder with `project.toml`; what you deploy | one Python package with `app.toml` |
| Example | `fitdata` | `fitdata-garmin` |
| Owns | the server accounts, the data folder, the installed-apps list, settings, project-level dashboards | its commands, its jobs and schedules, its tables and files, the dashboards it ships, its guide for agents |
| Isolation | projects cannot read each other | apps in one project trust each other (same account, same data) |

## An app is a package

```
fitdata/                         the project
  project.toml
  pyproject.toml                 one environment for the whole project
  apps/
    fitdata_garmin/              an app that lives in the project
      app.toml
      __init__.py, cli.py, ...
      dashboards/*.yaml
      AGENTS.md
  ui/dashboards/*.yaml           project-level dashboards (may combine several apps)
```

An app can also be a separate, reusable package (its own repo, installed as a dependency) as long
as it ships an `app.toml`. The project decides which apps are active:

```toml
# project.toml
[project]
name = "fitdata"
title = "Fitness"
apps = ["fitdata_garmin"]        # installed apps, by package name; order matters for overrides

[settings.fitdata_garmin]        # optional per-project overrides of what the app declares
jobs.daily.schedule = "*-*-* 06,12,21:00:00"
jobs.backfill.enabled = false
```

```toml
# apps/fitdata_garmin/app.toml
[app]
name = "fitdata-garmin"          # the command name and the prefix for its jobs
title = "Garmin archive"
cli = "fitdata_garmin.cli:app"   # a mes.App

[data]
folder = "garmin"                # its own subfolder of the project's data folder
tables = ["days", "activities", "files"]   # tables it owns in the project's shared database
rebuild = "index"                # the command that rebuilds only these tables from its files

[jobs.activities]
run = "activities-sync --days 2" # a command of THIS app; no app name needed
schedule = "*:0/10"
network = ["connectapi.garmin.com"]
memory = "512M"
timeout = "20m"

[ui]
dashboards = ["overview", "sleep"]   # shipped in the app's dashboards/ folder
```

## How a project is composed

- **Commands.** Every app keeps its own command (`fitdata-garmin sync`). The project also gets one
  command named after the project that reaches every app: `fitdata garmin sync`,
  `fitdata describe` (all apps, all commands, with their kinds). Agents use the project command.
- **Jobs.** `mes` collects the jobs of every installed app and names them `<app>.<job>`
  (`fitdata-garmin.activities`). A job always runs its own app's command. `after =` may name a job
  of another app in the same project. The project can change a schedule or switch a job off in
  `[settings.<app>]` without editing the app.
- **Data.** One data folder and one shared SQLite database per project. Each app keeps its files
  under its own subfolder and declares the tables it owns; its rebuild command may drop and
  recreate only those tables. Any app may read any table. Two apps may not declare the same table.
  Cross-app views belong to the project (`[views]` in `project.toml`, plain SELECTs).
- **Dashboards.** `mes` gathers dashboards from every app plus the project's own `ui/dashboards/`.
  A project dashboard with the same name replaces an app's (like overriding a template), so an
  owner or their agent can reshape an app's dashboard without touching the app. The tables the
  dashboards may read are the union of what the apps' `[ui]` sections allow, narrowed by the
  project if it wants.
- **Network and limits.** Declared per job, in the app. The server plan shows them per app so the
  owner can see what each installed app is allowed to reach.
- **Guides.** `fitdata guide` joins the project's AGENTS.md with each app's.

## What does not change

- The project is still the only security boundary: two accounts per project, the gate, signed
  deploys. Installing an app into a project means trusting it with that project's data.
- Dashboards stay declarative, and the dashboard server still reads only published copies.
- An app with a single `app.toml` and a single project wrapping it stays as easy as today:
  `mes new <project>` creates a project with one app in `apps/`; `mes new-app <name>` adds another.

## Moving the fitness project to this shape

`fitdata` becomes a project with one app, `apps/fitdata_garmin/`, holding today's code. Its files
already live under `garmin/` in the data folder, so nothing moves on disk. The tables `days`,
`activities` and `files` become that app's tables. The commands `fitdata-garmin ...` keep working,
and `fitdata ...` becomes the project command that reaches it.
