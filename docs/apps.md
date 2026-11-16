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

[tables.activities]              # a shared table: any installed app may add rows to it
columns = { id = "text", date = "text", type = "text", distance_m = "real", duration_s = "real" }
key = ["source", "id"]           # "source" (the app that wrote the row) is added automatically

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
writes = ["days", "activities"]  # shared project tables it contributes rows to
tables = ["garmin_files"]        # tables only this app owns
rebuild = "index"                # rebuilds its own rows and tables from its files, nothing else

[jobs.activities]
run = "activities-sync --days 2" # a command of THIS app; no app name needed
schedule = "*:0/10"
network = ["connectapi.garmin.com"]
memory = "512M"
timeout = "20m"

[ui]
dashboards = ["garmin-training"]     # whole dashboards shipped in the app's dashboards/ folder

[ui.contributes.overview]            # panels it adds to the project's shared "overview" dashboard
sections = "dashboards/overview-garmin.yaml"
```

## How a project is composed

- **Commands.** Every app keeps its own command (`fitdata-garmin sync`). The project also gets one
  command named after the project that reaches every app: `fitdata garmin sync`,
  `fitdata describe` (all apps, all commands, with their kinds). Agents use the project command.
- **Jobs.** `mes` collects the jobs of every installed app and names them `<app>.<job>`
  (`fitdata-garmin.activities`). A job always runs its own app's command. `after =` may name a job
  of another app in the same project. The project can change a schedule or switch a job off in
  `[settings.<app>]` without editing the app.
- **Data: apps contribute to the same data.** One data folder and one shared SQLite database per
  project, and every app can read all of it. There are two kinds of table:
  - *Shared tables* are declared by the project (`[tables.<name>]` in `project.toml`, with their
    columns) and any installed app may write rows into them. Every row carries the app that wrote
    it (`source`), and an app's rebuild replaces only its own rows. This is how a Garmin app and a
    Strava app both feed one `activities` table, and one `days` table gets sleep from one app and
    weight from another (apps fill different columns of the same day; the project says which app
    wins when two fill the same column).
  - *App tables* belong to one app (declared in its `app.toml`) for data nobody else produces.
  Each app keeps its raw files under its own subfolder. Cross-app views are plain SELECTs in
  `[views]` in `project.toml`.
- **Dashboards: apps contribute to the same dashboards.** A dashboard is a list of sections.
  The project owns the dashboards (`ui/dashboards/`), and an app can do three things:
  - ship whole dashboards of its own;
  - *contribute sections to a project dashboard* by naming it (`[ui.contributes.overview]` in
    `app.toml`, pointing at a file of rows and components), so installing an app adds its panels to
    the shared "Overview" without anyone editing that dashboard;
  - be overridden: a project file with the same name replaces an app's dashboard or contributed
    section, so the owner or their agent always has the last word.
  Components read shared tables like any other, so one chart can show data from several apps
  without knowing which app produced which row. The tables dashboards may read are the union of
  what the project and the apps allow, narrowed by the project if it wants.
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
already live under `garmin/` in the data folder, so nothing moves on disk. `days` and `activities`
become shared project tables that the Garmin app fills (ready for a second source), and `files`
becomes the app's own table. The commands `fitdata-garmin ...` keep working,
and `fitdata ...` becomes the project command that reaches it.
