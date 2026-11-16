"""project.toml and app.toml: parsing, the plain migration errors, composition, and the lock."""

import hashlib
import json
from pathlib import Path

import pytest

import projects as helpers
from mes import manifest
from mes.errors import ExitCode, ManifestError, NotFound

PROJECT = """
[project]
name = "myproj"
title = "My project"
version = "0.2.0"
apps = ["myproj_main", "myproj_tools"]

[tables.activities]
key = ["id"]
columns = { id = "text", date = "text", km = "real" }

[tables.days]
key = ["date"]
prefer = ["myproj-tools"]
[tables.days.columns]
date = "text"
steps = "integer"
note = "text"

[views.weekly]
sql = "SELECT date, count(*) AS n FROM activities GROUP BY 1"

[settings.myproj-tools]
jobs.tidy.timeout = "5m"
jobs.tidy.add_network = ["extra.example.com"]

[ui]
accent = "#2A7F62"
dashboards = ["overview"]
refresh_after = ["myproj-main.build"]
"""

MAIN = """
[app]
name = "myproj-main"
title = "Main"
cli = "myproj_main.cli:app"

[data]
writes = ["activities", "days"]
tables = ["main_notes"]
rebuild = "build"

[jobs.sync]
run = "sync --days 3 --label 'two words'"
schedule = "*-*-* 06:00:00"
network = ["api.example.com"]

[jobs.build]
run = "build"
after = "sync"
memory = "512M"
timeout = "2h"
writes = ["data"]

[jobs.export]
run = "export"
manual = true

[ui]
tables = ["main_notes"]
"""

TOOLS = """
[app]
name = "myproj-tools"
command = "proj-tools"

[data]
folder = "toolbox"
writes = ["days"]

[jobs.tidy]
run = "tidy --all"
after = "myproj-main.build"
network = ["api.example.com"]
"""


@pytest.fixture
def proj(tmp_path):
    return helpers.load(tmp_path / "myproj", PROJECT, {"myproj_main": MAIN, "myproj_tools": TOOLS})


def _err(text: str) -> ManifestError:
    with pytest.raises(ManifestError) as e:
        manifest.loads(text)
    return e.value


def _app_err(text: str, package: str | None = None) -> ManifestError:
    with pytest.raises(ManifestError) as e:
        manifest.loads_app(text, package=package)
    return e.value


def _compose_err(tmp_path: Path, project: str, apps: dict[str, str]) -> ManifestError:
    with pytest.raises(ManifestError) as e:
        helpers.load(tmp_path / "p", project, apps)
    return e.value


# ---------------------------------------------------------------- project.toml on its own


def test_parse_project_file():
    pf = manifest.loads(PROJECT)
    assert (pf.name, pf.title, pf.version, pf.packages) == ("myproj", "My project", "0.2.0",
                                                           ("myproj_main", "myproj_tools"))
    acts = pf.tables["activities"]
    assert acts.rows_table == "activities__rows" and acts.index_name == "activities__rows_key"
    assert [(c.name, c.type) for c in acts.columns] == [("id", "text"), ("date", "text"), ("km", "real")]
    assert acts.key == ("id",) and acts.prefer == ()
    assert pf.tables["days"].prefer == ("myproj-tools",)
    assert pf.views["weekly"].sql.startswith("SELECT")
    assert pf.settings["myproj-tools"]["tidy"].timeout == "5m"
    assert pf.ui == manifest.UI(accent="#2a7f62", dashboards=("overview",), tables=None,
                                refresh_after=("myproj-main.build",))
    assert pf.sha256 == hashlib.sha256(PROJECT.encode()).hexdigest()


def test_defaults_title_and_version():
    pf = manifest.loads('[project]\nname = "ab"\napps = ["ab_main"]\n')
    assert pf.title == "ab" and pf.version == "0.0.0" and pf.tables == {} and pf.ui is None


# The plain-language migration errors (contract section 1.6).


def test_old_apps_section_is_refused_with_a_migration_message():
    err = _err('[project]\nname = "fitdata"\n[apps.fitdata]\ncli = "fitdata"\n')
    assert err.message == ("project.toml still has the old [apps.fitdata] section. Apps are now Python packages, each "
                           "with its own app.toml, and the project lists them under [project] apps. The project cannot "
                           "be used until this is changed.")
    assert err.hint == ('Move the app into apps/fitdata/ with an app.toml (see: mes guide apps), write apps = '
                        '["fitdata"] under [project], and remove [apps.fitdata].')
    assert err.exit_code == 1


def test_old_jobs_section_is_refused_with_a_migration_message():
    err = _err('[project]\nname = "x1"\napps = ["x1_a"]\n[jobs.sync]\nrun = "x1 sync"\nmanual = true\n')
    assert err.message == ("project.toml still has [jobs.sync]. Jobs now belong to the app that runs them, in its "
                           "app.toml, so they are no longer read here.")
    assert err.hint == ("Move the job to [jobs.sync] in the app's app.toml and drop the app's name from its run line. "
                        "To change a schedule from the project, use [settings.<app>].")


def test_old_ui_data_is_refused():
    err = _err('[project]\nname = "fit"\napps = ["fit_a"]\n[ui]\ndata = "fit.sqlite"\n')
    assert err.message == ("[ui] data is no longer used: every project has one shared database, fit.sqlite, in its "
                           "data folder.")
    assert err.hint == "Remove the data line from [ui]."


def test_missing_apps_list():
    err = _err('[project]\nname = "fit"\n')
    assert err.message == ("project.toml does not list its apps, so nothing knows which commands, jobs and dashboards "
                           "the project has.")
    assert err.hint == 'Add apps = ["<package>"] under [project].'


def test_unknown_keys_name_the_key_and_the_allowed_keys():
    err = _err('extra = 1\n[project]\nname = "myproj"\napps = ["a_b"]\n')
    assert '"extra"' in err.message and "project, tables, views, settings, ui, storage" in err.hint
    err = _err('[project]\nname = "myproj"\napps = ["a_b"]\nowner = "x"\n')
    assert '"owner"' in err.message and "name, title, version, apps" in err.hint
    err = _err('[project]\nname = "myproj"\napps = ["a_b"]\n[ui]\ntabels = ["days"]\n')
    assert 'Did you mean "tables"?' in err.hint
    assert err.code == "manifest_invalid"


@pytest.mark.parametrize("name", ["A", "a", "1app", "my_app", "x" * 32, "my app", "mes", "python", "uv", "bash"])
def test_bad_project_names(name):
    _err(f'[project]\nname = "{name}"\napps = ["a_b"]\n')


@pytest.mark.parametrize("apps, needle", [
    ('"x"', "must be a list"),
    ('["Bad"]', "Python package names"),
    ('["a-b"]', "Python package names"),
    ('["a_b", "a_b"]', "more than once"),
    ("[]", "lists no app"),
    ('["mes"]', "framework"),
    ("[" + ", ".join(f'"p{i}x"' for i in range(17)) + "]", "more than once (or more than 16"),
])
def test_bad_apps_list(apps, needle):
    assert needle in _err(f'[project]\nname = "proj"\napps = {apps}\n').message


@pytest.mark.parametrize("table, needle", [
    ('[tables.Bad]\nkey = ["id"]\ncolumns = { id = "text" }', "table name"),
    ('[tables.a__b]\nkey = ["id"]\ncolumns = { id = "text" }', "table name"),
    ('[tables.sqlite_x]\nkey = ["id"]\ncolumns = { id = "text" }', "table name"),
    ('[tables.mes_x]\nkey = ["id"]\ncolumns = { id = "text" }', "table name"),
    ('[tables.t]\nkey = ["id"]', "no columns"),
    ('[tables.t]\ncolumns = { id = "text" }', 'no "key"'),
    ('[tables.t]\nkey = ["id"]\ncolumns = { id = "uuid" }', "does not know"),
    ('[tables.t]\nkey = ["id"]\ncolumns = { source = "text", id = "text" }', '"source"'),
    ('[tables.t]\nkey = ["id"]\ncolumns = { Id = "text" }', "column name"),
    ('[tables.t]\nkey = ["x"]\ncolumns = { id = "text" }', "not one of the table's columns"),
    ('[tables.t]\nkey = ["v"]\ncolumns = { v = "real" }', "text or integer"),
    ('[tables.t]\nkey = ["v"]\ncolumns = { v = "json" }', "text or integer"),
    ('[tables.t]\nkey = []\ncolumns = { id = "text" }', "1 to 4"),
    ('[tables.t]\nkey = ["a","b","c","d","e"]\ncolumns = { a="text",b="text",c="text",d="text",e="text" }', "1 to 4"),
    ('[tables.t]\nkey = ["id"]\ncolumns = { id = "text" }\nprefer = []', "at least one"),
    ('[tables.t]\nkey = ["id"]\ncolumns = { id = "text" }\nmerge = true', '"merge"'),
])
def test_bad_shared_tables(table, needle):
    err = _err(f'[project]\nname = "proj"\napps = ["a_b"]\n{table}\n')
    assert needle in err.message


@pytest.mark.parametrize("sql, needle", [
    ("SELECT 1; DROP TABLE x", '";"'),
    ("SELECT 1;", '";"'),
    ("DELETE FROM x", "SELECT or WITH"),
    ("PRAGMA writable_schema = 1", "SELECT or WITH"),
    ("SELECT 1 -- hidden", "comment"),
    ("SELECT /* x */ 1", "comment"),
    ("SELECT '\x01'", "control characters"),
    ("SELECT " + "1," * 6000 + "1", "longer than"),
])
def test_bad_views(sql, needle):
    err = _err(f'[project]\nname = "proj"\napps = ["a_b"]\n[views.v]\nsql = {json.dumps(sql)}\n')
    assert needle in err.message


def test_a_view_and_a_table_cannot_share_a_name():
    err = _err('[project]\nname = "proj"\napps = ["a_b"]\n[tables.t]\nkey = ["id"]\ncolumns = { id = "text" }\n'
               '[views.t]\nsql = "SELECT 1"\n')
    assert "both a shared table and a view" in err.message


@pytest.mark.parametrize("ui, needle", [
    ('accent = "blue"', "not a colour"),
    ('dashboards = ["Overview"]', "not one of dashboard names"),
    ('refresh_after = ["sync"]', "job ids"),
    ('refresh_after = "x.y"', "must be a list"),
    ('tables = ["days; DROP"]', "not one of table names"),
])
def test_bad_ui(ui, needle):
    err = _err(f'[project]\nname = "proj"\napps = ["a_b"]\n[ui]\n{ui}\n')
    assert needle in err.message + " " + (err.hint or "") and "[ui]" in err.message


@pytest.mark.parametrize("settings, needle", [
    ('[settings.my_app]\njobs.x.timeout = "5m"', "package name"),
    ('[settings.app-x]\njobs.x.colour = "red"', '"colour"'),
    ('[settings.app-x]\nschedule = "daily"', '"schedule"'),
    ('[settings.app-x]\njobs.x.enabled = "no"', "true or false"),
    ('[settings.app-x]\njobs.x.timeout = "forever"', "timeout"),
    ('[settings.app-x]\njobs.x.add_network = ["localhost"]', "local network"),
    ('[settings.app-x]\njobs.x.schedule = "sometimes"', "not a time mes can use"),
])
def test_bad_settings(settings, needle):
    err = _err(f'[project]\nname = "proj"\napps = ["a_b"]\n{settings}\n')
    assert needle in err.message + " " + (err.hint or "")


def test_storage_bucket_and_errors():
    pf = manifest.loads('[project]\nname = "myapp"\napps = ["a_b"]\n[storage]\nbucket = "archive"\n')
    assert pf.storage == manifest.Storage("archive")
    assert "bucket" in _err('[project]\nname = "myapp"\napps = ["a_b"]\n[storage]\nbucket = "Archive"\n').message
    assert "[storage] must be a section" in _err('storage = "x"\n[project]\nname = "myapp"\napps = ["a_b"]\n').message


def test_invalid_toml():
    err = _err('[project\nname = "x"')
    assert "typing mistake on line 1 (a bracket is not closed)" in err.message


def test_trailing_newlines_are_refused():
    for text in ('[project]\nname = "myapp\\n"\napps = ["a_b"]\n',
                 '[project]\nname = "myapp"\ntitle = "two\\nlines"\napps = ["a_b"]\n',
                 '[project]\nname = "myapp"\napps = ["a_b\\n"]\n'):
        _err(text)
    assert not manifest.PROJECT_NAME_RE.match("myapp\n")


# ---------------------------------------------------------------- app.toml on its own


def test_parse_app_file():
    af = manifest.loads_app(MAIN, package="myproj_main")
    assert (af.name, af.title, af.cli, af.command, af.program) == ("myproj-main", "Main", "myproj_main.cli:app",
                                                                   None, "myproj-main")
    assert af.data == manifest.AppData(folder=None, writes=("activities", "days"), tables=("main_notes",),
                                       rebuild="build")
    sync = af.jobs["sync"]
    assert sync.argv == ("sync", "--days", "3", "--label", "two words") and sync.trigger == "schedule"
    assert af.jobs["build"].after == "sync" and af.jobs["build"].writes == ("data",)
    tools = manifest.loads_app(TOOLS)
    assert tools.program == "proj-tools" and tools.data.folder == "toolbox"


@pytest.mark.parametrize("text, needle", [
    ('[app]\nname = "a-b"\n', 'exactly one of "cli"'),
    ('[app]\nname = "a-b"\ncli = "x.cli:app"\ncommand = "a-b"\n', 'exactly one of "cli"'),
    ('[app]\nname = "a-b"\ncli = "x.cli"\n', "module:object"),
    ('[app]\nname = "a-b"\ncli = "x/../cli:app"\n', "module:object"),
    ('[app]\nname = "mes"\ncommand = "x"\n', "not allowed"),
    ('[app]\nname = "python"\ncli = "x:app"\n', "not allowed"),
    ('[app]\nname = "Bad"\ncommand = "x"\n', "not allowed"),
    ('[app]\ncommand = "x"\n', "no name"),
    ('[app]\nname = "a-b"\ncommand = "bash"\n', "never accepted"),
    ('[app]\nname = "a-b"\ncommand = "../x"\n', "never accepted"),
    ('[app]\nname = "a-b"\ncommand = "a b"\n', "never accepted"),
    ('[app]\nname = "a-b"\ncommand = "x"\n[data]\nfolder = "store"\n', "kept for the project"),
    ('[app]\nname = "a-b"\ncommand = "x"\n[data]\nfolder = "../x"\n', "kept for the project"),
    ('[app]\nname = "a-b"\ncommand = "x"\n[data]\ntables = ["b__c"]\n', '"__"'),
    ('[app]\nname = "a-b"\ncommand = "x"\n[data]\nrebuild = "Re build"\n', "commands"),
    ('[app]\nname = "a-b"\ncommand = "x"\n[data]\ncolour = 1\n', '"colour"'),
    ('[app]\nname = "a-b"\ncommand = "x"\n[ui]\ntables = ["b_x"]\n', "not one of the app's own tables"),
    ('[app]\nname = "a-b"\ncommand = "x"\n[ui.contributes.overview]\nsections = "../x.yaml"\n',
     "not a file in the app's dashboards folder"),
    ('[app]\nname = "a-b"\ncommand = "x"\n[ui.contributes.overview]\nfile = "dashboards/x.yaml"\n', '"file"'),
    ('[app]\nname = "a-b"\ncommand = "x"\n[apps.x]\n', '"apps"'),
])
def test_bad_app_files(text, needle):
    assert needle in _app_err(text).message


def test_cli_must_be_inside_the_package():
    assert "not inside the app's package" in _app_err('[app]\nname = "a-b"\ncli = "other.cli:app"\n',
                                                      package="mine").message
    assert manifest.loads_app('[app]\nname = "a-b"\ncli = "mine.sub.cli:app"\n', package="mine").cli


def _job(body: str) -> str:
    return f'[app]\nname = "myapp-x"\ncommand = "myapp-x"\n[jobs.sync]\n{body}\n'


@pytest.mark.parametrize("job, needle", [
    ('schedule = "daily"', 'no "run"'),
    ('run = "sync"', "no trigger"),
    ('run = "sync"\nschedule = "daily"\nmanual = true', "more than one trigger"),
    ('run = "sync"\nmanual = false', "manual"),
    ('run = "sync"\nwebhook = "yes"', "webhook"),
    ('run = "sync | tee x"\nmanual = true', "not a shell command"),
    ('run = "sync && build"\nmanual = true', "not a shell command"),
    ('run = "sync $(whoami)"\nmanual = true', "not a shell command"),
    ('run = "sync \'oops"\nmanual = true', "split into words"),
    ('run = ""\nmanual = true', "must be text"),
    ('run = "sync"\nschedule = "daily; rm"', "calendar"),
    ('run = "sync"\nmanual = true\nmemory = "lots"', "memory"),
    ('run = "sync"\nmanual = true\ntimeout = "15 minutes"', "timeout"),
    ('run = "sync"\nmanual = true\nnetwork = true', "network"),
    ('run = "sync"\nmanual = true\nnetwork = []', "network"),
    ('run = "sync"\nmanual = true\nnetwork = ["https://api.example.com"]', "host name"),
    ('run = "sync"\nmanual = true\nnetwork = ["10.0.0.1"]', "host name"),
    ('run = "sync"\nmanual = true\nnetwork = ["printer.local"]', "local network"),
    ('run = "sync"\nmanual = true\nwrites = ["home"]', "writes"),
    ('run = "sync"\nafter = "Not A Job"', "not a job name or a job id"),
    ('run = "--json sync"\nmanual = true', "not the name of one of the app's commands"),
    ('run = "./other"\nmanual = true', "not the name of one of the app's commands"),
    ('run = "sync"\nmanual = true\nrunn = "x"', 'Did you mean "run"?'),
])
def test_bad_jobs(job, needle):
    err = _app_err(_job(job))
    assert needle in err.message + " " + (err.hint or "")
    assert err.hint


@pytest.mark.parametrize("schedule", ["daily", "*-*-* 06:00:00", "Mon..Fri 08:00", "*:0/15",
                                      "Sat,Sun 10:00", "weekly Europe/Madrid", "*-*-* 06:00:00 UTC"])
def test_valid_schedules(schedule):
    assert manifest.loads_app(_job(f'run = "sync"\nschedule = "{schedule}"')).jobs["sync"].schedule == schedule


@pytest.mark.parametrize("schedule", ["every day at 6", "tomorrow", "6", "daily daily"])
def test_invalid_schedules_are_caught_without_systemd(schedule):
    err = _app_err(_job(f'run = "sync"\nschedule = "{schedule}"'))
    assert "is not a time mes can use" in err.message and "never start" in err.message


def test_network_hosts_are_normalised():
    job = manifest.loads_app(_job('run = "s"\nmanual = true\nnetwork = ["API.Example.com.", "api.example.com"]'))
    assert job.jobs["sync"].network == ("api.example.com",)
    hosts = ", ".join(f'"h{i}.example.com"' for i in range(manifest.MAX_HOSTS + 1))
    assert "more than" in _app_err(_job(f'run = "s"\nmanual = true\nnetwork = [{hosts}]')).message


# ---------------------------------------------------------------- composing a project


def test_compose_project(proj, tmp_path):
    assert list(proj.apps) == ["myproj-main", "myproj-tools"]
    main, tools = proj.apps["myproj-main"], proj.apps["myproj-tools"]
    assert (main.short, main.prefix, main.program, main.data.folder) == ("main", "main_", "myproj-main", "main")
    assert (tools.short, tools.prefix, tools.program, tools.data.folder) == ("tools", "tools_", "proj-tools", "toolbox")
    assert main.in_project and main.folder == (tmp_path / "myproj" / "apps" / "myproj_main").resolve()
    assert list(proj.jobs) == ["myproj-main.sync", "myproj-main.build", "myproj-main.export", "myproj-tools.tidy"]
    build = proj.jobs["myproj-main.build"]
    assert (build.id, build.name, build.app, build.after) == ("myproj-main.build", "build", "myproj-main",
                                                              "myproj-main.sync")
    tidy = proj.jobs["myproj-tools.tidy"]
    assert tidy.after == "myproj-main.build" and tidy.timeout == "5m"     # [settings] wins
    assert tidy.network == ("api.example.com", "extra.example.com")          # add_network adds
    assert [j.id for j in proj.dependents("myproj-main.build")] == ["myproj-tools.tidy"]
    assert proj.allowlist() == ["activities", "days", "weekly", "main_notes"]
    assert proj.db_file == "myproj.sqlite" and not proj.lock_used
    d = proj.to_dict()
    assert d["jobs"][0]["id"] == "myproj-main.sync" and d["jobs"][2]["network"] is False
    assert d["apps"][1]["location"] == "in-project"


def test_job_lookup_by_id_or_unique_name(proj):
    assert proj.job("build").id == "myproj-main.build"
    assert proj.job("myproj-tools.tidy").name == "tidy"
    with pytest.raises(NotFound) as e:
        proj.job("nope")
    assert e.value.exit_code == ExitCode.NOT_FOUND and "myproj-main.sync" in e.value.hint
    with pytest.raises(NotFound):
        proj.app("other")
    assert proj.app_for_word("tools").name == "myproj-tools" and proj.app_for_word("myproj-main").short == "main"


def test_bare_name_shared_by_two_apps_is_ambiguous(tmp_path):
    a = '[app]\nname = "pp-a"\ncommand = "pp-a"\n[jobs.sync]\nrun = "sync"\nmanual = true\n'
    b = '[app]\nname = "pp-b"\ncommand = "pp-b"\n[jobs.sync]\nrun = "sync"\nmanual = true\n'
    p = helpers.load(tmp_path, '[project]\nname = "pp"\napps = ["pp_a", "pp_b"]\n', {"pp_a": a, "pp_b": b})
    with pytest.raises(NotFound) as e:
        p.job("sync")
    assert e.value.code == "job_ambiguous" and "pp-a.sync, pp-b.sync" in e.value.hint


def test_settings_switch_a_job_off_and_replace_its_trigger(tmp_path):
    app = ('[app]\nname = "pp-a"\ncommand = "pp-a"\n[jobs.one]\nrun = "x"\nmanual = true\n'
           '[jobs.two]\nrun = "y"\nwebhook = true\n')
    p = helpers.load(tmp_path, '[project]\nname = "pp"\napps = ["pp_a"]\n[settings.pp-a]\njobs.one.enabled = false\n'
                     'jobs.two.schedule = "daily"\njobs.two.memory = "1G"\n', {"pp_a": app})
    assert list(p.jobs) == ["pp-a.two"] and p.disabled == ("pp-a.one",)
    two = p.jobs["pp-a.two"]
    assert (two.trigger, two.schedule, two.memory) == ("schedule", "daily", "1G")
    with pytest.raises(NotFound) as e:
        p.job("pp-a.one")
    assert e.value.code == "job_disabled"


def test_a_job_others_wait_for_cannot_be_switched_off(tmp_path):
    app = '[app]\nname = "pp-a"\ncommand = "pp-a"\n[jobs.one]\nrun = "x"\nmanual = true\n[jobs.two]\nrun = "y"\nafter = "one"\n'
    err = _compose_err(tmp_path, '[project]\nname = "pp"\napps = ["pp_a"]\n[settings.pp-a]\njobs.one.enabled = false\n',
                       {"pp_a": app})
    assert "pp-a.one is switched off" in err.message and "pp-a.two" in err.message


@pytest.mark.parametrize("project, needle", [
    ('apps = ["pp_a"]\n[settings.pp-b]\njobs.x.timeout = "1m"', "not the name of an installed app"),
    ('apps = ["pp_a"]\n[settings.pp-a]\njobs.nope.timeout = "1m"', 'no job called "nope"'),
    ('apps = ["pp_a"]\n[ui]\nrefresh_after = ["pp-a.nope"]', "no enabled job"),
    ('apps = ["pp_a"]\n[ui]\ntables = ["secret"]', "not a shared table"),
    ('apps = ["pp_a"]\n[tables.a_x]\nkey = ["id"]\ncolumns = { id = "text" }', "table prefix"),
    ('apps = ["pp_a"]\n[tables.t]\nkey = ["id"]\ncolumns = { id = "text" }\nprefer = ["pp-z"]', "not an installed app"),
    ('apps = ["pp_a"]\n[tables.t]\nkey = ["id"]\ncolumns = { id = "text" }\nprefer = ["pp-a"]',
     "does not write this table"),
])
def test_composition_errors(tmp_path, project, needle):
    app = '[app]\nname = "pp-a"\ncommand = "pp-a"\n[jobs.x]\nrun = "x"\nmanual = true\n'
    err = _compose_err(tmp_path, f'[project]\nname = "pp"\n{project}\n', {"pp_a": app})
    assert needle in err.message + " " + (err.hint or "")


def test_settings_keyed_by_a_package_name_suggests_the_app_name(tmp_path):
    app = '[app]\nname = "pp-a"\ncommand = "pp-a"\n[jobs.x]\nrun = "x"\nmanual = true\n'
    err = _compose_err(tmp_path, '[project]\nname = "pp"\napps = ["pp_a"]\n[settings.pp_a]\njobs.x.timeout = "1m"\n',
                       {"pp_a": app})
    assert "not its package name: for example [settings.pp-a]" in err.hint


@pytest.mark.parametrize("a, b, needle", [
    ('name = "pp-a"', 'name = "pp-a"', "both be reached"),
    ('name = "pp-walks"', 'name = "walks"', "both be reached"),
    ('name = "pp-a"', 'name = "pp-a-x"', "overlap"),
])
def test_names_and_prefixes_must_not_collide(tmp_path, a, b, needle):
    err = _compose_err(tmp_path, '[project]\nname = "pp"\napps = ["pp_a", "pp_b"]\n',
                       {"pp_a": f'[app]\n{a}\ncommand = "x1"\n', "pp_b": f'[app]\n{b}\ncommand = "x2"\n'})
    assert needle in err.message


def test_app_rules_that_need_the_project(tmp_path):
    def one(app: str) -> ManifestError:
        return _compose_err(tmp_path, '[project]\nname = "pp"\napps = ["pp_a"]\n', {"pp_a": app})

    assert "same as the project" in one('[app]\nname = "pp"\ncommand = "x"\n').message
    assert "project's own command" in one('[app]\nname = "pp-a"\ncommand = "pp"\n').message
    assert "uses for itself" in one('[app]\nname = "pp-describe"\ncommand = "x"\n').message
    assert "does not start with the app's prefix" in one(
        '[app]\nname = "pp-a"\ncommand = "x"\n[data]\ntables = ["notes"]\n').message
    err = one('[app]\nname = "pp-a"\ncommand = "x"\n[data]\nwrites = ["days"]\n')
    assert err.message == ("The app pp-a writes the shared table days, but project.toml has no [tables.days]. Add it, "
                           "with its columns, so every app writes the same columns.")
    assert "no job with that id" in one('[app]\nname = "pp-a"\ncommand = "x"\n[jobs.a]\nrun = "a"\n'
                                       'after = "pp-z.b"\n').message
    assert "circle" in one('[app]\nname = "pp-a"\ncommand = "x"\n[jobs.a]\nrun = "a"\nafter = "b"\n'
                           '[jobs.b]\nrun = "b"\nafter = "a"\n').message
    assert "dashboards/missing.yaml" in one('[app]\nname = "pp-a"\ncommand = "x"\n[ui]\ndashboards = ["missing"]\n'
                                            ).message


def test_two_apps_with_the_same_folder(tmp_path):
    err = _compose_err(tmp_path, '[project]\nname = "pp"\napps = ["pp_a", "pp_b"]\n',
                       {"pp_a": '[app]\nname = "pp-a"\ncommand = "x1"\n[data]\nfolder = "same"\n',
                        "pp_b": '[app]\nname = "pp-b"\ncommand = "x2"\n[data]\nfolder = "same"\n'})
    assert '"same"' in err.message


def test_unwritten_shared_table_is_a_warning(tmp_path):
    p = helpers.load(tmp_path, '[project]\nname = "pp"\napps = ["pp_a"]\n[tables.t]\nkey = ["id"]\n'
                     'columns = { id = "text" }\n', {"pp_a": '[app]\nname = "pp-a"\ncommand = "x"\n'})
    assert len(p.warnings) == 1 and '"t"' in p.warnings[0]


def test_ui_tables_narrows_the_allowlist(tmp_path):
    p = helpers.load(tmp_path, PROJECT.replace('dashboards = ["overview"]', 'dashboards = ["overview"]\n'
                                               'tables = ["weekly", "activities"]'),
                     {"myproj_main": MAIN, "myproj_tools": TOOLS})
    assert p.allowlist() == ["weekly", "activities"]


# ---------------------------------------------------------------- finding the apps: lock and fallback


def test_an_app_outside_the_project_needs_the_lock(tmp_path):
    root = helpers.write(tmp_path / "pp", '[project]\nname = "pp"\napps = ["pp_a"]\n')
    with pytest.raises(ManifestError) as e:
        manifest.load(root)
    assert e.value.message == ("The app pp_a is not inside this project's apps folder and the list of installed apps "
                               "has not been written yet.")
    assert e.value.hint == "Run: mes apps sync" and e.value.code == "apps_lock_missing"


def _locked(tmp_path: Path, **change) -> Path:
    root = helpers.write(tmp_path / "pp", '[project]\nname = "pp"\napps = ["pp_a", "pp_b"]\n',
                         {"pp_a": '[app]\nname = "pp-a"\ncommand = "pp-a"\n'})
    installed = tmp_path / "site" / "pp_b"
    installed.mkdir(parents=True)
    (installed / "app.toml").write_text('[app]\nname = "pp-b"\ncli = "pp_b.cli:app"\n')
    sha = hashlib.sha256((root / "project.toml").read_bytes()).hexdigest()
    root = root.resolve()
    lock = {"v": 1, "project": "pp", "project_toml_sha256": sha, "apps": [
        {"package": "pp_a", "name": "pp-a", "path": str(root / "apps" / "pp_a"), "in_project": True, "program": "pp-a"},
        {"package": "pp_b", "name": "pp-b", "path": str(installed.resolve()), "in_project": False, "program": "pp-b"},
    ]}
    for k, v in change.items():
        if k.startswith("app_"):
            lock["apps"][1][k[4:]] = v
        else:
            lock[k] = v
    (root / manifest.LOCK_FILE).write_text(json.dumps(lock))
    return root


def test_the_lock_finds_installed_apps(tmp_path):
    p = manifest.load(_locked(tmp_path))
    assert p.lock_used and list(p.apps) == ["pp-a", "pp-b"]
    assert not p.apps["pp-b"].in_project and p.apps["pp-b"].location == "installed"


@pytest.mark.parametrize("change", [
    {"v": 2},
    {"project": "other"},
    {"project_toml_sha256": "0" * 64},
    {"apps": []},
    {"app_path": "relative/path"},
    {"app_path": "/nowhere/at/all"},
    {"app_name": "pp-z"},
    {"app_program": "bash"},
    {"app_in_project": "yes"},
    {"app_extra": 1},
    {"extra": 1},
])
def test_a_stale_or_invalid_lock_is_refused(tmp_path, change):
    root = _locked(tmp_path, **change)
    with pytest.raises(ManifestError) as e:
        manifest.load(root)
    assert e.value.message == manifest.LOCK_STALE_MESSAGE and e.value.hint == "Run: mes apps sync"
    assert e.value.code == "apps_lock_stale"


def test_lock_problems_beyond_its_text(tmp_path):
    root = _locked(tmp_path)
    (root / "project.toml").write_text((root / "project.toml").read_text() + "\n")
    with pytest.raises(ManifestError) as e:
        manifest.load(root)
    assert e.value.code == "apps_lock_stale"  # project.toml changed after sync
    root2 = _locked(tmp_path / "two")
    (root2 / manifest.LOCK_FILE).write_text("x" * (manifest.MAX_LOCK_BYTES + 1))
    with pytest.raises(ManifestError):
        manifest.load(root2)
    root3 = _locked(tmp_path / "three")
    data = json.loads((root3 / manifest.LOCK_FILE).read_text())
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "app.toml").write_text('[app]\nname = "pp-a"\ncommand = "pp-a"\n')
    data["apps"][0]["path"] = str(other.resolve())
    (root3 / manifest.LOCK_FILE).write_text(json.dumps(data))
    with pytest.raises(ManifestError):
        manifest.load(root3)  # an in-project app must be in <project>/apps/<package>


def test_symlinked_app_folders_and_files_are_refused(tmp_path):
    root = helpers.write(tmp_path / "pp", '[project]\nname = "pp"\napps = ["pp_a"]\n')
    real = tmp_path / "real"
    real.mkdir()
    (real / "app.toml").write_text('[app]\nname = "pp-a"\ncommand = "x"\n')
    (root / "apps").mkdir()
    (root / "apps" / "pp_a").symlink_to(real)
    with pytest.raises(ManifestError):
        manifest.load(root)
    (root / "apps" / "pp_a").unlink()
    (root / "apps" / "pp_a").mkdir()
    (root / "apps" / "pp_a" / "app.toml").symlink_to(real / "app.toml")
    with pytest.raises(ManifestError):
        manifest.load(root)


def test_huge_files_are_refused_unread(tmp_path):
    root = helpers.write(tmp_path / "a", '[project]\nname = "pp"\napps = ["pp_a"]\n' + "#" * manifest.MAX_MANIFEST_BYTES)
    with pytest.raises(ManifestError):
        manifest.load(root)
    root = helpers.write(tmp_path / "b", '[project]\nname = "pp"\napps = ["pp_a"]\n',
                         {"pp_a": '[app]\nname = "pp-a"\ncommand = "x"\n' + "#" * manifest.MAX_APP_TOML_BYTES})
    with pytest.raises(ManifestError):
        manifest.load(root)


def test_load_file_dir_and_missing(tmp_path, proj):
    root = tmp_path / "myproj"
    assert manifest.load(root / "project.toml").path == (root / "project.toml").resolve()
    assert manifest.load_file(root).root == root.resolve()
    with pytest.raises(NotFound):
        manifest.load(tmp_path / "nowhere")


def test_find_walks_up_and_stops_at_pyproject(tmp_path):
    (tmp_path / "project.toml").write_text(PROJECT)
    deep = tmp_path / "apps" / "pkg"
    deep.mkdir(parents=True)
    assert manifest.find(deep) == (tmp_path / "project.toml").resolve()
    other = tmp_path / "other"
    (other / "sub").mkdir(parents=True)
    (other / "pyproject.toml").write_text("")
    assert manifest.find(other / "sub") is None


def test_short_names_and_prefixes():
    assert manifest.short_name("fitdata-garmin", "fitdata") == "garmin"
    assert manifest.short_name("strava-sync", "fitdata") == "strava-sync"
    assert manifest.table_prefix("strava-sync") == "strava_sync_"


# ---------------------------------------------------------------- the fitness project (contract 8.2, 8.3)


FITDATA = """
[project]
name = "fitdata"
title = "Fitness"
version = "0.1.0"
apps = ["fitdata_garmin"]

[tables.days]
key = ["date"]
columns = { date = "text", has_data = "boolean", steps = "integer", training_status = "json" }

[tables.activities]
key = ["id"]
columns = { id = "integer", date = "text", distance_m = "real" }

[ui]
accent = "#2a7f62"
dashboards = ["overview", "sleep", "activities", "trends"]
refresh_after = ["fitdata-garmin.activities", "fitdata-garmin.daily", "fitdata-garmin.backfill"]
"""

GARMIN = """
[app]
name = "fitdata-garmin"
title = "Garmin archive"
command = "fitdata-garmin"

[data]
folder = "garmin"
writes = ["days", "activities"]
tables = ["garmin_files"]
rebuild = "index"

[jobs.activities]
run = "activities-sync --days 2 --local"
schedule = "*:0/10"
network = ["connectapi.garmin.com", "connect.garmin.com", "diauth.garmin.com", "sso.garmin.com"]
memory = "512M"
timeout = "20m"

[jobs.daily]
run = "sync --days 3 --local"
schedule = "*-*-* 07,13,22:30:00"
network = ["connectapi.garmin.com", "connect.garmin.com", "diauth.garmin.com", "sso.garmin.com"]
memory = "512M"
timeout = "30m"

[jobs.backfill]
run = "backfill --max-days 90 --local"
schedule = "*-*-* 02:15:00"
network = ["connectapi.garmin.com", "connect.garmin.com", "diauth.garmin.com", "sso.garmin.com"]
memory = "512M"
timeout = "3h"
"""


def test_the_fitness_project_parses_and_renders(tmp_path):
    from mes import paths, units

    p = helpers.load(tmp_path, FITDATA, {"fitdata_garmin": GARMIN})
    assert p.apps["fitdata-garmin"].short == "garmin" and p.apps["fitdata-garmin"].data.folder == "garmin"
    assert p.jobs["fitdata-garmin.daily"].schedule == "*-*-* 07,13,22:30:00" and p.ui.accent == "#2a7f62"
    assert p.allowlist() == ["days", "activities"]
    rendered = units.render_project(p, layout=paths.server_layout("fitdata"), mes="/usr/local/bin/mes",
                                    easy_ui="/usr/local/bin/easy-ui")
    assert "OnCalendar=*-*-* 07,13,22:30:00" in rendered["mes-fitdata.job.fitdata-garmin.daily.timer"]
    assert "mes-fitdata.net.fitdata-garmin.daily.service" in rendered
    assert "mes-fitdata.ui-refresh.service" in rendered["mes-fitdata.job.fitdata-garmin.daily.service"]
