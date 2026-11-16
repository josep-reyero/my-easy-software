# v1 contract: projects, accounts, and what each repo must finish

This file is the shared contract for the v1 round. Where it differs from `design.md`, this file wins.

## 1. The unit is the project

Everything is split by **project**, not by app. A project is one folder (usually one git repo) with a
`project.toml` at its root. Example: `fitdata` is a project.

A project owns: one or more **apps** (command-line tools), its **jobs**, its **dashboards**, its
**data**, and on a server its own **operating-system accounts**. Projects cannot read each other.

`app.toml` is replaced by `project.toml` (no backwards compatibility needed; nothing is released):

```toml
[project]
name = "fitdata"          # ^[a-z][a-z0-9-]{1,30}\Z
title = "Fitness"
version = "0.1.0"

[apps.fitdata]            # optional; default is one app named like the project
cli = "fitdata"           # console script name inside the project's environment

[jobs.sync]
run = "fitdata sync"      # first word must be an app of this project
schedule = "*:0/10"       # or after = "<job>" or manual = true
network = ["connect.garmin.com"]

[ui]
data = "fitdata.sqlite"
tables = ["days", "activities"]
dashboards = ["today", "sleep"]
refresh_after = ["sync"]  # rebuild dashboard data after these jobs succeed

[storage]                 # optional
bucket = "archive"        # a my-bucket-storage bucket inside the project's store
```

`mes.App("name")` stays the way to write an app's CLI. `mes new <project>` creates a project.

## 2. Folders

On a personal machine (no accounts involved): `~/.local/share/mes/<project>/` (data),
`~/.local/state/mes/<project>/` (state). A project may point its data elsewhere on one machine with
`[projects.<name>] data_dir = "..."` in `~/.config/mes/config.toml`; `mes` and `easy-ui` both
honour it (this is how an existing archive is adopted without moving it).

On a server, everything for a project is under `/var/lib/mes/<project>/`:
`code/releases/<n>/` + `code/current`, `data/`, `state/`, `ui/` (published dashboards and data
bundles), `store/` (buckets).

## 3. Accounts on a server (the security core)

Two accounts per project, both without a password and without sudo:

- `mes-<project>`: owns code, data and state; runs the jobs and the SSH gate.
- `mes-<project>-web`: runs only the dashboard server; can read `ui/` and write its own sign-in
  folder `auth/` (beside `ui/`, `mes-<project>-web` 0700) and nothing else of the project (not the
  data, not the state); no network except its own unix socket.

Folders (`/var/lib/mes/<project>/`, root's 0711): `code data state store gate webhook backups`
(`mes-<project>` 0700), `ui ui/data ui/specs` (`mes-<project>`, group web, 2750), `auth` (web
account 0700), `etc home home/.ssh log` (root, group `mes-<project>`, 0750; files 0640). Every
unit that runs project code sees only its own project's folders (`TemporaryFileSystem=` over
`/var/lib/mes` plus binds), never `auth/`, `gate/` or the gate's sockets.

Nothing runs as the owner's own user or as root. Units are **system** units (so the sandbox
directives are really enforced) with `User=mes-<project>` / `User=mes-<project>-web`.

`mes` never runs sudo. `mes server plan <project>` prints, and with `--out FILE` writes, one
readable root script that creates the accounts, folders and unit files; a person reads it and runs
it with sudo once per project. `mes server check <project>` verifies the result as a checklist,
without root. `mes server remove <project>` renders the matching removal script.

Remote access is only SSH to `mes-<project>` with keys whose `authorized_keys` line forces
`mes gate --project <project> --allow read|write|deploy` and disables forwarding, pty and agent.
Admin commands never pass the gate. The `deploy` level runs only the mes operations; deploys and rollbacks
also need a separate signing key. `mes server selftest <project>` proves the sandboxes on the
server; `mes server analyze` writes systemd's own checks.

## 4. What each repo finishes in this round

### my-easy-software
- The project model above everywhere (manifest, paths, registry, gate, jobs, scaffold, guides).
- `mes new`: generated project works end to end, including a dashboard: its example stores SQLite,
  has `[ui]`, and `easy-ui validate` and `easy-ui preview` work on it.
- `mes dev`: runs the project locally: watches files, runs `easy-ui preview`, prints one URL.
- `mes server plan|check|remove` as in section 3, including system units for jobs, the
  `refresh_after` unit and the dashboard socket and service (rendered by calling `easy-ui units`).
- `mes deploy <host>`: exports the last commit from git's objects (and sibling path dependencies),
  builds their wheels on the client, signs the archive with `ssh-keygen -Y sign` (OpenSSH
  signatures, Ed25519, namespace `mes-deploy`) with a signing key that is never the login key,
  sends it through the gate's `deploy` level; the server verifies the signature against the
  project's `allowed_signers`, unpacks with a hardened extractor into a new release, runs `uv sync
  --frozen --no-build` and installs the wheels offline in the deploy sandbox, describes the apps
  in a read sandbox, records the release as complete, switches `current` atomically, publishes
  dashboards, keeps 5 releases, and can roll back with a signed, replay-protected request
  (namespace `mes-rollback`). `--dry-run` shows every step.
- Gate: on a server the SSH side runs no project code; every request runs in a socket-activated
  sandbox per level that checks its peer (an SSH session of the project's account). A server
  without the sockets refuses. The `deploy` level runs only the mes operations.
- Job network limits: `network = [hosts]` (and `local_network`) enforced on Linux system units with
  `IPAddressDeny=any` plus resolved internet-only `IPAddressAllow=` refreshed before each run by a
  root helper that also writes the job's own pinned hosts file (no name server for jobs), stated
  honestly where it cannot be enforced.
- `mes expose <project>`: builds a static export through `easy-ui export` and sends it to a hosting
  adapter. Adapters: Cloudflare Pages (first, complete), then Vercel, Netlify, GitHub Pages,
  Render, each behind one small interface; modes `people` (provider access control where the
  provider has it), `link` (AES-256-GCM, key only in the URL fragment), `public`. Tokens come from
  the environment or a file with owner-only permissions, never arguments. Never uploads without
  `--yes` or a `needs_input` confirmation that lists exactly what will become visible and to whom.
- Signed webhooks as a job trigger (`webhook = true`): HMAC-SHA256 with timestamp and replay
  window, verified by the dashboard server's account-separated endpoint design or documented as
  rendered configuration if it cannot be made safe in this round.
- `mes backup` / `mes restore` for a project's data and state (uses my-bucket-storage snapshots when
  the project has a store; plain tar otherwise), with a verify step.
- Agent skills: `skills/` in Agent Skills format plus a Claude Code plugin manifest, thin, each
  calling `mes guide <topic>`: set up, new project, add a command, add a job, add or change a
  dashboard, deploy, share, troubleshoot.
- Tailscale: `mes setup` covers serving a project's dashboard privately with `tailscale serve`
  (rendered commands, never run by mes).

### my-easy-ui
- Project model: `project.toml`, `--project-dir`, the `data_dir` config override, server paths.
- Passkeys (WebAuthn) as the main sign-in, password as fallback and for first registration;
  recovery codes. This also fixes the new-device lockout: throttling no longer blocks passkey
  sign-in.
- `easy-ui export`: a static, self-contained copy of chosen dashboards with their query results for
  the chosen control values, no server needed; `--encrypt` produces the link mode bundle
  (AES-256-GCM via WebCrypto in the page, key from the URL fragment).
- `easy-ui screenshot` for agents (uses a locally installed Chromium if present; otherwise says so).
- Webhook receiving endpoint if section 4's webhook design needs it.
- The recorded visual items: stacked table cards on phones, chart readout setting and partial-week
  marker, axis steps and units, `[ui]` first dashboard as the landing page.
- Units match section 3 (system units, `mes-<project>-web`).

### my-bucket-storage
- Remote use: `mybucket serve --stdio` protocol over SSH (no network listener) and a client in the
  SDK so `open_store("ssh://host/path")` works with the same API; streaming both ways.
- Access profiles: read-only / read-write / per-bucket and per-prefix scopes enforced on the serving
  side, for use behind a forced SSH command.
- Trained zstd dictionaries per bucket for many small similar objects (train, store, use the
  `dict_id` the format reserved; old objects stay readable; measured in the benchmark).
- Range reads on large objects (seekable frames) and `get(key, start, end)`.
- Replication: `mybucket sync SRC DST` between two stores (local or ssh), incremental, verified,
  safe to interrupt; this is the off-machine backup path.
- Keys: allow spaces and UTF-8 (NFC-normalised, still no control characters, no `.`/`..`, no
  leading/trailing space), with export/import handling them safely on case-insensitive disks.
- Tested on Python 3.12, 3.13 and 3.14 (`uv run --python 3.14`), including the stdlib zstd path.
- Not building, on purpose: an S3-compatible HTTP API, a TypeScript client.

### fitdata (the fitness project)
- Becomes a mes project: `project.toml` with its jobs (activities, daily, backfill) and
  `[ui] refresh_after`, its CLI answering `describe --json` with command kinds so the gate and jobs
  work, exit codes per the contract. The CLI keeps its current commands and its own remote mode.
- Data location solved through the `data_dir` override (section 2), no symlinks.
- Optional storage backend: `storage = "files" | "bucket"`; with `bucket`, raw and processed files
  live in a my-bucket-storage bucket, with `fitdata migrate-storage` (verified, reversible) and the
  measured saving reported.
- Docs updated (`sql --limit`, dashboards, the project model).
