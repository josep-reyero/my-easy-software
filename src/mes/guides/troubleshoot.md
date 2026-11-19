# Guide: troubleshooting

What to do when a mes command, a job, a deploy or a dashboard does not work.

Start with the exit code and the error's `message` and `hint` (with `--json`:
`{"error": {"code", "message", "hint"}}`). Exit codes: 1 error, 2 command not understood,
3 not found, 4 not allowed at this level, 5 refused for safety (do not look for a way around it),
6 needs the person (show the question or action, then run its `rerun`), 75 try again later.

On this computer:
- `mes doctor`: checks tools and settings, changes nothing.
- `uv run <app> --json describe`: the app's commands. A failure here means the app itself is
  broken: run `uv sync`, then read the error.
- `mes --json jobs list`: problems with jobs (unknown commands, wrong kinds, bad schedules).
- `mes --json jobs log --verify`: past runs and whether the chain of records is intact.
- `mes dev` prints what easy-ui said when the preview does not start; `easy-ui validate` explains
  dashboard mistakes.

On a server:
- `mes server check <project> --project-dir <folder>`: accounts, folders, keys, tools, units, run log.
- `mes server selftest <project> --host <server>`: what each sandbox really can and cannot reach.
- `mes deploy my-server --releases`: what is installed and which release runs.
- `systemctl status 'mes-<project>.*'` and `journalctl -u 'mes-<project>.*'` (the person runs
  these): what the jobs and gate sandboxes did.
- A run missing from `mes jobs log --project <project> --server-root /var/lib/mes`: each run is
  added by the job's record unit; the person checks `systemctl status
  mes-<project>.record.<app>.<job>.service`. A failed record unit never changes the job's own
  result. `not_from_systemd`: systemd knows of no finished run of that job that is not in the log
  yet; `already_recorded`: that run is in the log already (nothing to do).
- A job with `after` (or the dashboard refresh) fails at once with exit 255
  (`should_run_broken`): root's check before it could not ask systemd how the job before it
  ended. Run `mes server plan` and its script again, so the units match the installed mes, then
  `mes server selftest`.
- "differ from what the server was set up for": run `mes server plan` and its script again.
- `bad_signature`: the server does not know the signing key; plan again with `--signer`.
- `sandbox_unavailable` (exit 75): the gate's sandbox socket is missing or not answering; the
  person checks `systemctl status mes-<project>.gate-<level>.socket` (an old plan: run the plan
  and its script again). `sandbox_peer`: something other than an SSH session of the project's
  account tried the sandbox socket.
- `tool_in_project`: the plan was made with a mes inside the project; use `/usr/local/bin/mes`.
- `same_key`: the login key is also a signing key; make a separate signing key.
- `no_wheels` / `sync_failed` on deploy: a package has only source code; see `mes guide deploy`.
- A job with hosts cannot connect: the host list of the job (in its app's `app.toml`, plus
  `add_network` in `project.toml`) must include every host it uses (redirects to other hosts are
  blocked too); a device on your own network belongs in `local_network`.
- A job on a server says "Could not resolve host", "Timeout while contacting DNS servers" or
  "Name or service not known": the job uses a host it does not list. A job knows only the names
  in its `network` list (it has no DNS), so add the host there (or with `add_network`), then run
  `mes server plan` and its script again. When the host is listed and it still happens, the
  units or their name-service files are older than the installed mes: run the plan and its
  script again, then `mes server selftest` (its `net-name-service` line).

Never fix a refusal by loosening permissions, switching off a check or copying data into the
repo. Explain the problem to the user in plain words and ask.
