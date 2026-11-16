from pathlib import Path

import pytest

from mes import manifest
from mes.errors import ExitCode, ManifestError, NotFound

GOOD = """
[app]
name = "myapp"
title = "My app"
version = "0.2.0"

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
"""


def test_parse_good_manifest():
    m = manifest.loads(GOOD)
    assert (m.name, m.title, m.version) == ("myapp", "My app", "0.2.0")
    assert list(m.jobs) == ["sync", "build", "export"]
    sync = m.jobs["sync"]
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


def test_defaults_title_and_version():
    m = manifest.loads('[app]\nname = "ab"\n')
    assert m.title == "ab" and m.version == "0.0.0" and m.jobs == {}


def test_job_lookup_not_found():
    m = manifest.loads(GOOD)
    with pytest.raises(NotFound) as e:
        m.job("nope")
    assert e.value.exit_code == ExitCode.NOT_FOUND
    assert "sync" in e.value.hint


def _err(text):
    with pytest.raises(ManifestError) as e:
        manifest.loads(text)
    return e.value


def test_unknown_key_names_key_and_allowed_keys():
    err = _err('[app]\nname = "myapp"\n[jobs.sync]\nrunn = "sync"\nmanual = true\n')
    assert '"runn"' in err.message and "[jobs.sync]" in err.message
    assert "run, schedule, after, manual, memory, timeout, network, writes" in err.hint
    assert 'Did you mean "run"?' in err.hint
    assert err.code == "manifest_invalid" and err.exit_code == 1


def test_unknown_top_level_and_app_keys():
    assert '"extra"' in _err('extra = 1\n[app]\nname = "myapp"\n').message
    err = _err('[app]\nname = "myapp"\nowner = "x"\n')
    assert '"owner"' in err.message and "name, title, version" in err.hint


@pytest.mark.parametrize("name", ["A", "a", "1app", "my_app", "x" * 32, "my app"])
def test_bad_app_names(name):
    _err(f'[app]\nname = "{name}"\n')


def test_missing_app_section_and_name():
    assert "[app]" in _err('[jobs.x]\nrun = "x"\nmanual = true\n').message
    assert "no name" in _err('[app]\ntitle = "x"\n').message


@pytest.mark.parametrize(
    "job, needle",
    [
        ('schedule = "daily"', "no \"run\""),
        ('run = "sync"', "no trigger"),
        ('run = "sync"\nschedule = "daily"\nmanual = true', "more than one trigger"),
        ('run = "sync"\nmanual = false', "manual"),
        ('run = "sync | tee x"\nmanual = true', "not a shell command"),
        ('run = "sync && build"\nmanual = true', "not a shell command"),
        ('run = "sync $(whoami)"\nmanual = true', "not a shell command"),
        ('run = "sync \'oops"\nmanual = true', "split into words"),
        ('run = ""\nmanual = true', "must be text"),
        ('run = "sync"\nschedule = "daily; rm"', "calendar"),
        ('run = "sync"\nmanual = true\nmemory = "lots"', "memory"),
        ('run = "sync"\nmanual = true\nmemory = 256', "memory"),
        ('run = "sync"\nmanual = true\ntimeout = "15 minutes"', "timeout"),
        ('run = "sync"\nmanual = true\nnetwork = true', "network"),
        ('run = "sync"\nmanual = true\nnetwork = ["https://api.example.com"]', "host name"),
        ('run = "sync"\nmanual = true\nwrites = ["home"]', "writes"),
        ('run = "sync"\nafter = "missing"', "no job with that name"),
        ('run = "sync"\nafter = "sync"', "after itself"),
    ],
)
def test_bad_jobs(job, needle):
    err = _err(f'[app]\nname = "myapp"\n[jobs.sync]\n{job}\n')
    assert needle in err.message
    assert err.hint


def test_after_cycle():
    err = _err(
        '[app]\nname = "myapp"\n'
        '[jobs.a]\nrun = "a"\nafter = "b"\n'
        '[jobs.b]\nrun = "b"\nafter = "a"\n'
    )
    assert "circle" in err.message


def test_bad_job_name():
    assert "job name" in _err('[app]\nname = "myapp"\n[jobs.Sync]\nrun = "s"\nmanual = true\n').message


def test_invalid_toml():
    err = _err('[app\nname = "x"')
    assert "typing mistake on line 1 (a bracket is not closed)" in err.message


def test_load_file_dir_and_missing(tmp_path):
    (tmp_path / "app.toml").write_text(GOOD)
    assert manifest.load(tmp_path).name == "myapp"
    m = manifest.load(tmp_path / "app.toml")
    assert m.path == (tmp_path / "app.toml").resolve()
    with pytest.raises(NotFound):
        manifest.load(tmp_path / "nowhere")


def test_find_walks_up_and_stops_at_project(tmp_path):
    (tmp_path / "app.toml").write_text(GOOD)
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert manifest.find(deep) == (tmp_path / "app.toml").resolve()
    other = tmp_path / "other"
    (other / "sub").mkdir(parents=True)
    (other / "pyproject.toml").write_text("")
    assert manifest.find(other / "sub") is None


def test_validate_app_name():
    assert manifest.validate_app_name("fitness") == "fitness"
    with pytest.raises(ManifestError):
        manifest.validate_app_name("../etc")


# ---------------------------------------------------------------- review fixes

def _job(**settings) -> str:
    body = "\n".join(f"{k} = {v}" for k, v in settings.items())
    return f'[app]\nname = "myapp"\n[jobs.sync]\n{body}\n'


@pytest.mark.parametrize("text", [
    '[app]\nname = "myapp\\n"\n',
    '[app]\nname = "myapp"\n[jobs."sync\\n"]\nrun = "s"\nmanual = true\n',
    _job(run='"s"', manual="true", memory='"256M\\n"'),
    _job(run='"s"', manual="true", network='["a.com\\n"]'),
    _job(run='"s"', manual="true", timeout='"5m\\n"'),
])
def test_trailing_newline_is_refused(text):
    _err(text)


def test_app_name_regex_refuses_trailing_newline():
    assert not manifest.APP_NAME_RE.match("myapp\n")
    with pytest.raises(ManifestError):
        manifest.validate_app_name("myapp\n")


@pytest.mark.parametrize("key,value", [("timeout", '"999999999999999999999h"'), ("memory", '"99999999999G"')])
def test_sizes_have_a_bound(key, value):
    assert "digits)" in _err(_job(run='"s"', manual="true", **{key: value})).hint


@pytest.mark.parametrize("run", ['"-c \'import os\'"', '"--json sync"', '"./other"', '"Sync"'])
def test_run_must_start_with_a_command_name(run):
    assert "not the name of one of the app's commands" in _err(_job(run=run, manual="true")).message


@pytest.mark.parametrize("schedule", ["daily", "*-*-* 06:00:00", "Mon..Fri 08:00", "*:0/15",
                                      "Sat,Sun 10:00", "weekly Europe/Madrid", "*-*-* 06:00:00 UTC"])
def test_valid_schedules(schedule):
    m = manifest.loads(_job(run='"s"', schedule=f'"{schedule}"'))
    assert m.jobs["sync"].schedule == schedule


@pytest.mark.parametrize("schedule", ["every day at 6", "tomorrow", "6", "daily daily"])
def test_invalid_schedules_are_caught_without_systemd(schedule):
    err = _err(_job(run='"s"', schedule=f'"{schedule}"'))
    assert "is not a time mes can use" in err.message and "never start" in err.message


def test_network_list_is_reported_as_not_enforced():
    m = manifest.loads(_job(run='"s"', manual="true", network='["api.example.com"]'))
    d = m.jobs["sync"].to_dict()
    assert d["network"] == ["api.example.com"] and d["network_enforced"] is False
    assert "full internet access" in d["network_note"]
    off = manifest.loads(_job(run='"s"', manual="true")).jobs["sync"].to_dict()
    assert off["network_enforced"] is True and off["network_note"] is None
