# Guide: jobs and triggers

How to make an app do work on its own: on a schedule, after another job, or on request.

A job runs one of the app's own CLI commands as a short, sandboxed process that exits when done.
Jobs are declared in `app.toml`:

```toml
[jobs.sync]
run = "sync --days 3"            # the app's subcommand and arguments (not a shell line)
schedule = "*-*-* 06:00:00"      # systemd calendar expression, e.g. "daily", "hourly"
network = ["api.example.com"]    # default false: no network at all (see the note below)
memory = "256M"                  # default 256M
timeout = "15m"                  # default 15m
writes = ["data", "state"]       # default; which app folders the job may change

[jobs.build]
run = "build"
after = "sync"                   # starts when sync has finished

[jobs.export]
run = "export"
manual = true                    # only when someone asks
```

Rules:
- Exactly one trigger per job: `schedule`, `after` or `manual = true`.
- `run` is never a shell command: no pipes, `;`, `&&` or redirections. Chain jobs with `after`.
- Leave `network = false` unless the job really needs the internet. When it does, list the hosts
  it needs. Be honest with the user: this version of mes cannot yet limit a job to those hosts,
  so a job with a host list has full internet access (`mes jobs list` says so too). Only
  `network = false` is enforced (the job gets no network at all).
- The first word of `run` must be one of the app's own `read` or `write` commands; mes checks it
  against the app before running or installing (`admin` commands cannot be jobs).
- Schedules are checked by mes on any computer (`daily`, `hourly`, `Mon..Fri 08:00`,
  `*-*-* 06:00:00`, `*:0/15`, ...), and again by systemd on Linux before they are switched on.
- Unknown settings are an error, so a typo cannot switch off a limit.

Commands:
- `mes --json jobs list`: the jobs of the app in the current folder and their triggers.
- `mes --json jobs run <job>`: run a job now.
- `mes --json jobs install` / `uninstall`: turn the schedules on or off (Linux with systemd only;
  on a Mac, run jobs by hand). `mes jobs install --render-only` shows the files on any computer.
- `mes --json jobs log` / `mes jobs log --verify`: past runs, and a check that the log was not changed.
- `mes --json jobs status`: when each job last ran and whether it worked.

Tell the user in plain words what a job will do and when, before installing it.
