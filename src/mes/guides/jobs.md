# Guide: jobs and triggers

How to make a project's app do work on its own: on a schedule, after another job, on request or from a signed webhook.

A job runs one app command as a short process that exits when done. Jobs live in `project.toml`:

```toml
[jobs.sync]
run = "notes sync --days 3"      # the app's name, its command and arguments (never a shell line)
schedule = "*-*-* 06:00:00"      # systemd calendar expression: "daily", "hourly", "*:0/15", ...
network = ["api.example.com"]    # default false: no network at all
memory = "256M"                  # default 256M
timeout = "15m"                  # default 15m
writes = ["data", "state"]       # default; which project folders the job may change

[jobs.build]
run = "notes build"
after = "sync"                   # starts when sync has succeeded

[jobs.export]
run = "notes export"
manual = true                    # only when someone asks

[jobs.hook]
run = "notes sync"
webhook = true                   # started by a signed webhook on the server (mes guide webhooks)
```

Rules:
- Exactly one trigger: `schedule`, `after`, `manual = true` or `webhook = true`.
- `run` is never a shell command: no pipes, `;`, `&&` or redirections. Chain jobs with `after`.
- The command must be a `read` or `write` command of that app (`admin` cannot be a job).
- `network = false` (default): on a server the job gets no network at all. A list of hosts: on a
  server the job may reach only those hosts (and the DNS servers); mes resolves them just before
  each run and lets systemd block everything else. On this computer (`mes jobs run`) mes cannot
  limit the network, and `mes jobs list` says so.
- Unknown settings are errors, so a typo cannot switch off a limit.

Commands:
- `mes --json jobs list`: the jobs, their triggers and what their network setting means here.
- `mes --json jobs run <job>`: run a job now, on this computer.
- `mes --json jobs log` (`--verify` checks the log was not changed) and `mes --json jobs status`.

Schedules run on a server: `mes server plan` turns every job into a sandboxed system unit
(its own account, no access to other projects or home folders, memory and time limits), and
`mes deploy` installs new versions. On a laptop, run jobs by hand. Tell the user in plain words
what a job does and when, before adding it.
