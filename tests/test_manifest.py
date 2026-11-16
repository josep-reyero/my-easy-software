from pathlib import Path

import pytest

from mes import manifest
from mes.errors import ExitCode, ManifestError, NotFound

GOOD = """
[project]
name = "myapp"
title = "My app"
version = "0.2.0"

[jobs.sync]
run = "myapp sync --days 3 --label 'two words'"
schedule = "*-*-* 06:00:00"
network = ["api.example.com"]

[jobs.build]
run = "myapp build"
after = "sync"
memory = "512M"
timeout = "2h"
writes = ["data"]

[jobs.export]
run = "myapp export"
manual = true
"""


def test_parse_good_manifest():
    m = manifest.loads(GOOD)
    assert (m.name, m.title, m.version) == ("myapp", "My app", "0.2.0")
    assert list(m.apps) == ["myapp"] and m.apps["myapp"].cli == "myapp"
    assert list(m.jobs) == ["sync", "build", "export"]
    sync = m.jobs["sync"]
    assert sync.app == "myapp" and sync.command == "sync"
    assert sync.argv == ("sync", "--days", "3", "--label", "two words")
    assert sync.trigger == "schedule" and sync.schedule == "*-*-* 06:00:00"
    assert sync.network == ("api.example.com",) and sync.network_allowed
    assert sync.memory == "256M" and sync.timeout == "15m" and sync.writes == ("data", "state")
    build = m.jobs["build"]
    assert build.trigger == "after" and build.after == "sync"
    assert build.memory_bytes == 512 * 1024**2 and build.timeout_seconds == 7200
    assert build.writes == ("data",)
    assert m.jobs["export"].trigger == "manual"
    assert m.jobs["export"].network == () and not m.jobs["export"].network_allowed
    assert [j.name for j in m.dependents("sync")] == ["build"]
    d = m.to_dict()
    assert d["jobs"][2]["network"] is False
    assert d["jobs"][0]["network"] == ["api.example.com"]
    assert d["apps"] == [{"name": "myapp", "cli": "myapp"}]
    assert d["storage"] is None and d["ui"] is None


def test_defaults_title_and_version():
    m = manifest.loads('[project]\nname = "ab"\n')
    assert m.title == "ab" and m.version == "0.0.0" and m.jobs == {}
    assert list(m.apps) == ["ab"]


def test_job_and_app_lookup_not_found():
    m = manifest.loads(GOOD)
    with pytest.raises(NotFound) as e:
        m.job("nope")
    assert e.value.exit_code == ExitCode.NOT_FOUND
    assert "sync" in e.value.hint
    with pytest.raises(NotFound):
        m.app("other")


def _err(text):
    with pytest.raises(ManifestError) as e:
        manifest.loads(text)
    return e.value


def test_unknown_key_names_key_and_allowed_keys():
    err = _err('[project]\nname = "myapp"\n[jobs.sync]\nrunn = "myapp sync"\nmanual = true\n')
    assert '"runn"' in err.message and "[jobs.sync]" in err.message
    assert "run, schedule, after, manual, webhook, memory, timeout, network, local_network, writes" in err.hint
    assert 'Did you mean "run"?' in err.hint
    assert err.code == "manifest_invalid" and err.exit_code == 1


def test_unknown_top_level_and_project_keys():
    assert '"extra"' in _err('extra = 1\n[project]\nname = "myapp"\n').message
    err = _err('[project]\nname = "myapp"\nowner = "x"\n')
    assert '"owner"' in err.message and "name, title, version" in err.hint
    # The old [app] section is not accepted any more: a project.toml starts with [project].
    assert '"app"' in _err('[app]\nname = "myapp"\n').message


@pytest.mark.parametrize("name", ["A", "a", "1app", "my_app", "x" * 32, "my app"])
def test_bad_project_names(name):
    _err(f'[project]\nname = "{name}"\n')


def test_missing_project_section_and_name():
    assert "[project]" in _err('[jobs.x]\nrun = "x y"\nmanual = true\n').message
    assert "no name" in _err('[project]\ntitle = "x"\n').message


@pytest.mark.parametrize(
    "job, needle",
    [
        ('schedule = "daily"', "no \"run\""),
        ('run = "myapp sync"', "no trigger"),
        ('run = "myapp sync"\nschedule = "daily"\nmanual = true', "more than one trigger"),
        ('run = "myapp sync"\nmanual = false', "manual"),
        ('run = "myapp sync"\nwebhook = "yes"', "webhook"),
        ('run = "myapp sync | tee x"\nmanual = true', "not a shell command"),
        ('run = "myapp sync && build"\nmanual = true', "not a shell command"),
        ('run = "myapp sync $(whoami)"\nmanual = true', "not a shell command"),
        ('run = "myapp sync \'oops"\nmanual = true', "split into words"),
        ('run = ""\nmanual = true', "must be text"),
        ('run = "myapp sync"\nschedule = "daily; rm"', "calendar"),
        ('run = "myapp sync"\nmanual = true\nmemory = "lots"', "memory"),
        ('run = "myapp sync"\nmanual = true\nmemory = 256', "memory"),
        ('run = "myapp sync"\nmanual = true\ntimeout = "15 minutes"', "timeout"),
        ('run = "myapp sync"\nmanual = true\nnetwork = true', "network"),
        ('run = "myapp sync"\nmanual = true\nnetwork = []', "network"),
        ('run = "myapp sync"\nmanual = true\nnetwork = ["https://api.example.com"]', "host name"),
        ('run = "myapp sync"\nmanual = true\nnetwork = ["10.0.0.1"]', "host name"),
        ('run = "myapp sync"\nmanual = true\nwrites = ["home"]', "writes"),
        ('run = "myapp sync"\nafter = "missing"', "no job with that name"),
        ('run = "myapp sync"\nafter = "sync"', "after itself"),
        ('run = "other sync"\nmanual = true', "not an app of this project"),
        ('run = "myapp"\nmanual = true', "not the name of one of the app's commands"),
    ],
)
def test_bad_jobs(job, needle):
    err = _err(f'[project]\nname = "myapp"\n[jobs.sync]\n{job}\n')
    assert needle in err.message
    assert err.hint


def test_after_cycle():
    err = _err(
        '[project]\nname = "myapp"\n'
        '[jobs.a]\nrun = "myapp a"\nafter = "b"\n'
        '[jobs.b]\nrun = "myapp b"\nafter = "a"\n'
    )
    assert "circle" in err.message


def test_bad_job_name():
    assert "job name" in _err('[project]\nname = "myapp"\n[jobs.Sync]\nrun = "myapp s"\nmanual = true\n').message


def test_invalid_toml():
    err = _err('[project\nname = "x"')
    assert "typing mistake on line 1 (a bracket is not closed)" in err.message


def test_load_file_dir_and_missing(tmp_path):
    (tmp_path / "project.toml").write_text(GOOD)
    assert manifest.load(tmp_path).name == "myapp"
    m = manifest.load(tmp_path / "project.toml")
    assert m.path == (tmp_path / "project.toml").resolve()
    assert m.root == tmp_path.resolve()
    with pytest.raises(NotFound):
        manifest.load(tmp_path / "nowhere")


def test_load_refuses_huge_file(tmp_path):
    (tmp_path / "project.toml").write_text('[project]\nname = "myapp"\n' + "#" * (manifest.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ManifestError):
        manifest.load(tmp_path)


def test_find_walks_up_and_stops_at_pyproject(tmp_path):
    (tmp_path / "project.toml").write_text(GOOD)
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert manifest.find(deep) == (tmp_path / "project.toml").resolve()
    other = tmp_path / "other"
    (other / "sub").mkdir(parents=True)
    (other / "pyproject.toml").write_text("")
    assert manifest.find(other / "sub") is None


def test_validate_names():
    assert manifest.validate_project_name("fitness") == "fitness"
    with pytest.raises(ManifestError):
        manifest.validate_project_name("../etc")
    assert manifest.validate_app_name("fitness") == "fitness"
    with pytest.raises(ManifestError):
        manifest.validate_app_name("mes")


# ---------------------------------------------------------------- [apps]


def test_explicit_apps_and_jobs_of_each_app():
    m = manifest.loads(
        '[project]\nname = "fitdata"\n'
        '[apps.fitdata]\ncli = "fitdata"\n'
        '[apps.fit-tools]\ncli = "fit-tools.cli"\n'
        '[jobs.sync]\nrun = "fitdata sync"\nmanual = true\n'
        '[jobs.tidy]\nrun = "fit-tools tidy --all"\nafter = "sync"\n'
    )
    assert list(m.apps) == ["fitdata", "fit-tools"]
    assert m.app("fit-tools").cli == "fit-tools.cli"
    assert m.jobs["tidy"].app == "fit-tools" and m.jobs["tidy"].argv == ("tidy", "--all")


def test_app_cli_defaults_to_its_name():
    m = manifest.loads('[project]\nname = "proj"\n[apps.other]\n')
    assert m.apps == {"other": manifest.AppSpec("other", "other")}
    # The project's own name is not an app when [apps] is given.
    err = _err('[project]\nname = "proj"\n[apps.other]\n[jobs.x]\nrun = "proj x"\nmanual = true\n')
    assert "not an app" in err.message


@pytest.mark.parametrize("cli", ["python", "python3.13", "bash", "sh", "uv", "pip", "env", "mes", "easy-ui",
                                 "../x", "/bin/ls", "a b", "-x", ""])
def test_unsafe_cli_names_are_refused(cli):
    err = _err(f'[project]\nname = "proj"\n[apps.proj]\ncli = "{cli}"\n')
    assert "[apps.proj]" in err.message


@pytest.mark.parametrize("text, needle", [
    ('[project]\nname = "proj"\n[apps.mes]\n', '"mes"'),
    ('[project]\nname = "mes"\n', '"mes"'),
    ('apps = 1\n[project]\nname = "proj"\n', "[apps]"),
    ('[project]\nname = "proj"\n[apps]\n', "at least one"),
    ('[project]\nname = "proj"\n[apps.Bad]\n', "app name"),
    ('[project]\nname = "proj"\n[apps.proj]\nrun = "x"\n', '"run"'),
    ('[project]\nname = "proj"\n[apps.proj]\ncli = 3\n', '"cli"'),
])
def test_bad_apps(text, needle):
    assert needle in _err(text).message


# ---------------------------------------------------------------- review fixes

def _job(**settings) -> str:
    body = "\n".join(f"{k} = {v}" for k, v in settings.items())
    return f'[project]\nname = "myapp"\n[jobs.sync]\n{body}\n'


@pytest.mark.parametrize("text", [
    '[project]\nname = "myapp\\n"\n',
    '[project]\nname = "myapp"\ntitle = "two\\nlines"\n',
    '[project]\nname = "myapp"\n[jobs."sync\\n"]\nrun = "myapp s"\nmanual = true\n',
    '[project]\nname = "myapp"\n[apps."myapp\\n"]\n',
    '[project]\nname = "myapp"\n[apps.myapp]\ncli = "myapp\\n"\n',
    _job(run='"myapp s"', manual="true", memory='"256M\\n"'),
    _job(run='"myapp s"', manual="true", timeout='"5m\\n"'),
    '[project]\nname = "myapp"\n[ui]\ndata = "a\\n.sqlite"\n',
])
def test_trailing_newline_is_refused(text):
    _err(text)


def test_surrounding_space_is_trimmed_never_kept():
    m = manifest.loads('[project]\nname = "myapp"\nversion = "1.0\\n"\n'
                       '[jobs.sync]\nrun = "myapp s"\nmanual = true\nnetwork = ["a.com\\n"]\n')
    assert m.version == "1.0" and m.jobs["sync"].network == ("a.com",)


def test_name_regex_refuses_trailing_newline():
    assert not manifest.PROJECT_NAME_RE.match("myapp\n")
    with pytest.raises(ManifestError):
        manifest.validate_project_name("myapp\n")


@pytest.mark.parametrize("key,value", [("timeout", '"999999999999999999999h"'), ("memory", '"99999999999G"')])
def test_sizes_have_a_bound(key, value):
    assert "digits)" in _err(_job(run='"myapp s"', manual="true", **{key: value})).hint


@pytest.mark.parametrize("run", ['"myapp -c \'import os\'"', '"myapp --json sync"', '"myapp ./other"', '"myapp Sync"'])
def test_run_needs_a_command_name_after_the_app(run):
    assert "not the name of one of the app's commands" in _err(_job(run=run, manual="true")).message


@pytest.mark.parametrize("run", ['"python -c x"', '"./myapp sync"', '"/usr/bin/myapp sync"', '"sync"'])
def test_run_must_start_with_an_app(run):
    assert "not an app of this project" in _err(_job(run=run, manual="true")).message


@pytest.mark.parametrize("schedule", ["daily", "*-*-* 06:00:00", "Mon..Fri 08:00", "*:0/15",
                                      "Sat,Sun 10:00", "weekly Europe/Madrid", "*-*-* 06:00:00 UTC"])
def test_valid_schedules(schedule):
    m = manifest.loads(_job(run='"myapp s"', schedule=f'"{schedule}"'))
    assert m.jobs["sync"].schedule == schedule


@pytest.mark.parametrize("schedule", ["every day at 6", "tomorrow", "6", "daily daily"])
def test_invalid_schedules_are_caught_without_systemd(schedule):
    err = _err(_job(run='"myapp s"', schedule=f'"{schedule}"'))
    assert "is not a time mes can use" in err.message and "never start" in err.message


def test_webhook_trigger():
    m = manifest.loads(_job(run='"myapp s"', webhook="true"))
    job = m.jobs["sync"]
    assert job.trigger == "webhook" and job.schedule is None and job.after is None
    assert job.to_dict()["trigger"] == "webhook"
    assert "more than one trigger" in _err(_job(run='"myapp s"', webhook="true", manual="true")).message


def test_network_hosts_are_normalised_and_reported():
    m = manifest.loads(_job(run='"myapp s"', manual="true", network='["API.Example.com.", "api.example.com"]'))
    job = m.jobs["sync"]
    assert job.network == ("api.example.com",)
    d = job.to_dict()
    assert d["network"] == ["api.example.com"]
    assert d["network_enforced"] == {"server": True, "this_computer": False}
    assert "full internet access" in d["network_note"]
    off = manifest.loads(_job(run='"myapp s"', manual="true")).jobs["sync"].to_dict()
    assert off["network_enforced"] == {"server": True, "this_computer": True} and off["network_note"] is None


def test_too_many_hosts():
    hosts = ", ".join(f'"h{i}.example.com"' for i in range(manifest.MAX_HOSTS + 1))
    assert "more than" in _err(_job(run='"myapp s"', manual="true", network=f"[{hosts}]")).message


# ---------------------------------------------------------------- [ui] (read by my-easy-ui)

UI_GOOD = """
[project]
name = "fitness"
title = "Fitness"

[jobs.sync]
run = "fitness sync"
manual = true

[ui]
data = "fitness.sqlite"
tables = ["days", "activities"]
accent = "#2A7F62"
dashboards = ["overview", "sleep"]
refresh_after = ["sync"]
"""


def test_ui_table_is_accepted():
    m = manifest.loads(UI_GOOD)
    assert m.ui == manifest.UI(data="fitness.sqlite", tables=("days", "activities"), accent="#2a7f62",
                               dashboards=("overview", "sleep"), refresh_after=("sync",))
    assert m.to_dict()["ui"]["tables"] == ["days", "activities"]
    assert m.to_dict()["ui"]["refresh_after"] == ["sync"]
    assert manifest.loads('[project]\nname = "myapp"\n').ui is None
    assert manifest.loads('[project]\nname = "myapp"\n[ui]\n').ui == manifest.UI()


def test_ui_with_jobs():
    m = manifest.loads(GOOD + '\n[ui]\ndata = "a.sqlite"\ntables = ["days"]\n')
    assert m.ui.data == "a.sqlite" and set(m.jobs) == {"sync", "build", "export"}


@pytest.mark.parametrize(
    "ui, needle",
    [
        ('tabels = ["days"]', 'Did you mean "tables"?'),
        ('data = "/etc/passwd"', "inside the project's data folder"),
        ('data = "../x.sqlite"', "inside the project's data folder"),
        ('data = "~/x.sqlite"', "inside the project's data folder"),
        ("data = 3", "inside the project's data folder"),
        ('tables = "days"', "must be a list"),
        ('tables = ["days; DROP"]', "not one of table names"),
        ('tables = ["days", "days"]', "more than once"),
        ('accent = "blue"', "not a colour"),
        ('dashboards = ["Overview"]', "not one of dashboard names"),
        ('refresh_after = ["missing"]', "no job with that name"),
        ('refresh_after = "sync"', "must be a list"),
    ],
)
def test_bad_ui(ui, needle):
    err = _err(f'[project]\nname = "myapp"\n[ui]\n{ui}\n')
    assert needle in err.message + " " + (err.hint or "") and "[ui]" in err.message


def test_ui_must_be_a_table():
    assert "[ui] must be a section" in _err('ui = 1\n[project]\nname = "myapp"\n').message


# ---------------------------------------------------------------- [storage]


def test_storage_bucket():
    m = manifest.loads('[project]\nname = "myapp"\n[storage]\nbucket = "archive"\n')
    assert m.storage == manifest.Storage("archive")
    assert m.to_dict()["storage"] == {"bucket": "archive"}


@pytest.mark.parametrize("storage, needle", [
    ('bucket = "Archive"', "bucket"),
    ('bucket = "../x"', "bucket"),
    ("bucket = 1", "bucket"),
    ("", "bucket"),
    ('bucket = "a"\nsize = 3', '"size"'),
])
def test_bad_storage(storage, needle):
    assert needle in _err(f'[project]\nname = "myapp"\n[storage]\n{storage}\n').message


def test_storage_must_be_a_table():
    assert "[storage] must be a section" in _err('storage = "x"\n[project]\nname = "myapp"\n').message


@pytest.mark.parametrize("host", ["localhost", "db", "x.localhost", "printer.local", "nas.lan", "api.internal"])
def test_local_network_hosts_are_refused(host):
    from mes import manifest

    text = ('[project]\nname = "demo"\n[apps.demo]\ncli = "demo"\n'
            f'[jobs.sync]\nrun = "demo sync"\nmanual = true\nnetwork = ["{host}"]\n')
    with pytest.raises(ManifestError) as e:
        manifest.loads(text)
    assert "local network" in e.value.message


@pytest.mark.parametrize("host", ["0x7f.0.0.1", "0x7f.1", "2130706433", "127.1", "1.1.1.1", "x.0x7f",
                                  "169.254.169.254", "017700000001"])
def test_numeric_hosts_in_any_spelling_are_refused(host):
    """Audit F6: 0x7f.0.0.1 and 0x7f.1 passed the check and resolve to this computer."""
    from mes import manifest

    for key in ("network", "local_network"):
        text = ('[project]\nname = "demo"\n[apps.demo]\ncli = "demo"\n'
                f'[jobs.sync]\nrun = "demo sync"\nmanual = true\n{key} = ["{host}"]\n')
        with pytest.raises(ManifestError):
            manifest.loads(text)


def test_local_network_hosts_are_allowed_where_marked():
    from mes import manifest

    p = manifest.loads('[project]\nname = "demo"\n[apps.demo]\ncli = "demo"\n[jobs.sync]\nrun = "demo sync"\n'
                       'manual = true\nnetwork = ["api.example.com"]\nlocal_network = ["nas.home.arpa", "api.example.com"]\n')
    job = p.jobs["sync"]
    assert job.local_network == ("nas.home.arpa",) and job.hosts == ("api.example.com", "nas.home.arpa")
    for bad in ("localhost", "x.localhost"):
        with pytest.raises(ManifestError):
            manifest.loads('[project]\nname = "demo"\n[apps.demo]\ncli = "demo"\n[jobs.sync]\nrun = "demo sync"\n'
                           f'manual = true\nlocal_network = ["{bad}"]\n')


REAL_WORLD = """
[project]
name = "fitdata"
title = "Fitness"
version = "0.1.0"

[apps.fitdata]
cli = "fitdata"

[jobs.activities]
run = "fitdata activities-sync --days 2 --local"
schedule = "*:0/10"
network = ["connectapi.garmin.com", "connect.garmin.com"]
memory = "512M"
timeout = "10m"

[jobs.daily]
run = "fitdata sync --days 3 --local"
schedule = "*-*-* 07,13,22:30:00"
network = ["connectapi.garmin.com"]
memory = "512M"
timeout = "30m"

[jobs.backfill]
run = "fitdata backfill --max-days 90 --local"
schedule = "*-*-* 02:15:00"
network = ["connectapi.garmin.com"]
timeout = "3h"

[ui]
data = "fitdata.sqlite"
tables = ["days", "activities"]
accent = "#2a7f62"
dashboards = ["overview", "sleep", "activities", "trends"]
refresh_after = ["activities", "daily", "backfill"]
"""


def test_a_real_world_project_parses_and_renders():
    from mes import manifest, paths, units

    p = manifest.loads(REAL_WORLD)
    assert p.jobs["daily"].schedule == "*-*-* 07,13,22:30:00" and p.ui.accent == "#2a7f62"
    rendered = units.render_project(p, layout=paths.server_layout("fitdata"), mes="/usr/local/bin/mes",
                                    easy_ui="/usr/local/bin/easy-ui")
    assert "OnCalendar=*-*-* 07,13,22:30:00" in rendered["mes-fitdata.job.daily.timer"]
    assert "mes-fitdata.net.daily.service" in rendered
