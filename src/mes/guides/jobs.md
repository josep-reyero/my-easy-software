# Guide: jobs and triggers

How to make a project's app do work on its own: on a schedule, after another job, on request or from a signed webhook.

A job runs one app command as a short process that exits when done. Jobs belong to the app that
runs them, in its `app.toml`; their id is `<app>.<job>` (for example `notes-main.sync`):

```toml
[jobs.sync]
run = "sync --days 3"            # one of this app's commands and its arguments (never a shell line)
schedule = "*-*-* 06:00:00"      # systemd calendar expression: "daily", "hourly", "*:0/15", ...
network = ["api.example.com"]    # default false: no network at all
local_network = ["nas.home.arpa"] # devices on your own network it may also reach (default none)
memory = "256M"                  # default 256M
timeout = "15m"                  # default 15m
writes = ["data", "state"]       # default; which project folders the job may change

[jobs.build]
run = "build"
after = "sync"                   # starts when sync of this app has succeeded

[jobs.export]
run = "export"
manual = true                    # only when someone asks

[jobs.hook]
run = "sync --quick"
webhook = true                   # started by a signed webhook on the server (mes guide webhooks)
```

`after` names a job of the same app, or the id of another app's job (`after = "notes-main.build"`).
The project can change an app's jobs without touching the app, in `project.toml`:

```toml
[settings.notes-main]            # the app's name, not its package
jobs.sync.schedule = "*-*-* 07:00:00"   # replaces the trigger
jobs.sync.timeout = "30m"
jobs.sync.add_network = ["cdn.example.com"]
jobs.export.enabled = false     # switched off (refused if another job waits for it)
```

Rules:
- Exactly one trigger: `schedule`, `after`, `manual = true` or `webhook = true`.
- `run` is never a shell command: no pipes, `;`, `&&` or redirections. Chain jobs with `after`.
- The command must be a `read` or `write` command of that app (`admin` cannot be a job).
- `network = false` (default): on a server the job gets no network at all. A list of hosts: just
  before each run root looks them up, writes the job's own hosts file (its only name lookup: it
  gets no name server and no system name service) and lets systemd allow exactly those addresses
  and block everything else. Only internet addresses count; a host's address on this computer,
  on the local network or link-local (cloud metadata) is dropped, unless the host is listed in
  `local_network` (then local-network addresses are allowed; this computer never). Host names
  must be names: numbers in any spelling (`0x7f.1`, `2130706433`) are refused.
- Honest limits: an allowed address is allowed on every port, and a shared address (a CDN) also
  serves other sites; addresses that change during a run are not followed. On this computer
  (`mes jobs run`) mes cannot limit the network (`mes jobs list` says so), but the job gets only
  a small environment (no SSH agent, no hosting tokens).
- Exit code 75 means "a temporary problem, try again later" (for example the service was down):
  the run is recorded as `skipped`, not failed, and the jobs that wait for it (and the dashboard
  refresh) do not run. On a server the units count it as a success (`SuccessExitStatus=75`), so
  `systemctl --failed` only lists real failures; the next scheduled run tries again.
- A job that lists hosts cannot open unix sockets (it shares the server's network, where other
  programs' sockets live); socket pairs inside the job still work. A job without network has a
  private network. Neither sees the server's service sockets or anything outside the project's
  own folders (`mes guide server`).
- Unknown settings are errors, so a typo cannot switch off a limit.

Commands:
- `mes --json jobs list`: the jobs, their triggers and what their network setting means here. It
  checks each job's command by asking the app (which runs the project's code as a read command,
  never as root); `--no-describe` skips that.
- `mes --json jobs run <app>.<job>`: run a job now, on this computer, then the jobs that wait for it
  (`--no-chain` runs only that one). A bare job name works when only one app has a job of that name.
  A job that may write data first brings the shared database in line with `project.toml`.
- `mes --json jobs log` and `mes --json jobs status` (results: `success`, `skipped`, `exit-code`,
  `timeout`, ...). `--verify` checks the chain of records; on
  a personal computer that shows mistakes, not a determined edit. On a server root also keeps an
  anchor (record count and newest hash) after every run, so `--verify --server-root /var/lib/mes`
  also catches a log that was cut, emptied or replaced.

Schedules run on a server: `mes server plan` turns every job into a sandboxed system unit
(`mes-<project>.job.<app>.<job>.service`, run as the project's own account, no access to other projects or home folders, memory and time limits), and
`mes deploy` installs new versions. On a laptop, run jobs by hand. Tell the user in plain words
what a job does and when, before adding it.
