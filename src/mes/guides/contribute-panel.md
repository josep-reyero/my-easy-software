# Guide: contribute a panel to a dashboard

Let an app add its own sections to one of the project's dashboards, and how the project can override them.

A project dashboard (`ui/dashboards/<slug>.yaml`) can mark places where apps may add parts, with
anchor items in its layout:

```yaml
layout:
  - - type: stat
      query: total
  - anchor: activity         # apps may place sections right after this
  - anchor: apps
```

An app declares what it adds in its `app.toml`:

```toml
[ui.contributes.overview]
sections = "dashboards/overview-walks.yaml"   # a file in the app's dashboards folder
```

That file holds `sections`, each with an `id`, its `position` (`start`, `end` or `after:<anchor>`),
its own `queries` (their names start with the app's prefix, for example `walks_total`) and the
`layout` rows to place:

```yaml
spec: easy-ui/sections-v1
sections:
  - id: walks
    position: after:activity
    queries:
      walks_total:
        sql: SELECT count(*) AS n FROM activities WHERE source = 'fitlab-walks'
    layout:
      - - type: stat
          title: Walks
          query: walks_total
```

Queries read only the allowlist: the shared tables and views, plus the app tables listed under the
apps' `[ui] tables` (`mes db schema` prints it). Sections are placed in `[project] apps` order; an
unknown anchor puts the section at the end, with a warning.

The project decides in the end: `ui/sections/<app>.<slug>.yaml` replaces an app's sections for that
dashboard (`sections: []` removes them), and `ui/dashboards/<slug>.yaml` replaces an app's whole
dashboard of the same name. `easy-ui compose --project-dir . --json` shows the final order and where
every section came from; `mes dev` previews it. See `mes guide dashboards`.
