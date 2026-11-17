# Guide: deploying a project to a server

How to install a new version of a project on a server set up with mes server plan, and how to go back.

```
mes --json deploy my-server --dry-run     # every step, nothing signed or sent
mes --json deploy my-server               # export, build, sign, send, install, switch
mes --json deploy my-server --releases    # what is installed
mes --json deploy my-server --rollback [--to N]
```

`my-server` is the server's SSH name; mes connects as `mes-<project>@my-server` (give
`user@host` to change it). Two keys (`mes guide server`): the **login key** reaches the gate
(`--login-key`, or `[deploy] login_key` in `~/.config/mes/config.toml`, or what SSH offers), the
**signing key** signs (`--key`, `[deploy] key`, default `~/.ssh/mes-sign`; best a hardware key).

On this computer mes:
1. exports the last commit (`HEAD`) straight from git's objects, never the working folder:
   untracked files and uncommitted changes are not sent (the notes say so), links are refused,
   `project.toml`, `pyproject.toml` and `uv.lock` must be committed; plus the last commit of each
   sibling path dependency (`path = "../<folder>"`, such as `../my-easy-software`). Path
   dependencies inside the project, such as `vendor/<app>`, are part of its export.
   `apps.lock.json` is never sent: it holds this computer's paths;
2. builds a wheel of the project and of each such dependency (inside the project too) from that
   export (`uv build --wheel`);
3. signs the archive with `ssh-keygen -Y sign -n mes-deploy` and sends it through the gate, which
   accepts it only from a `deploy` key. A rollback is a short request (project, release, time,
   random nonce) signed in the namespace `mes-rollback`; the server accepts it within 5 minutes,
   once.

On the server the gate (which itself runs no project code):
1. checks the signature against the project's allowed signers **before unpacking anything**,
   reading one open file that has already left the folder;
2. unpacks with a hardened extractor (plain files and folders only, size and count limits);
3. refuses an archive accepted before or made before the newest accepted one (kept in
   `gate/accepted.json`, which no sandbox can change, so it holds even when old releases were
   removed); a release that was interrupted is removed and never blocks sending it again. If
   that record is missing while releases were installed, or is damaged, every deploy stops
   (exit 5 `missing_record` or `damaged_record`) until the owner re-creates it from the
   releases' own records with the command the error names
   (`sudo /usr/local/bin/mes server rebuild-accepted <project>`); a project with no releases
   yet starts with an empty record;
4. **install step**, in the deploy sandbox: the package index, but not this computer, link-local,
   multicast, private, Tailscale (`100.64.0.0/10`) or IPv6 unique-local addresses (not covered:
   this server's own public addresses and the global IPv6 addresses of other machines on the
   local network, which cannot be known when the units are written); writable: only the new
   release (the link `code/installing` the gate points at it) and uv's cache and Python under
   `code/`; no data, state, ui, store or gate folder; no user settings. `uv sync --frozen --no-build --no-dev --no-install-project` puts the locked
   third-party packages into the release's fresh `.venv` without ever starting its Python (which
   would run every `.pth` startup file of those packages), then mes unpacks the archive's own
   wheels itself, with no Python started;
5. **describe step**, in a read sandbox (no network; cannot change any release): finds every app
   (as `mes apps sync` does) and asks each app's program to describe itself. The gate checks that
   answer, writes the release's own `apps.lock.json` and checks the jobs' commands (problems
   become notes);
6. records the release as complete with a fingerprint of every file in it, switches `current`
   in one step, and in the **publish step** (no network, the code read-only, `ui/` writable, no
   data or state) publishes the dashboards (switching back if that fails). Before the first data
   there is nothing to publish: the answer says `dashboards: waiting for first data` (exit 0),
   and the dashboard refresh publishes them after the first job that brings data;
7. keeps the newest 5 releases. A rollback switches only to a complete release whose files are
   still exactly as they were installed (it checks the fingerprint).

What remains: third-party packages come from the package index during `uv sync` (checked
against the hashes in `uv.lock`); a package that has only source code fails unless the plan was
made with `--allow-server-builds`, and then its build code runs in the install step (with the
internet and the new release writable). Its install step then gets a cache and a Python of the
release's own (`.uv/` inside the release), and the shared `code/uv-cache` and `code/uv-python`
are not there for it at all, so nothing build code writes is used by another deploy. The install step finds names through
systemd-resolved's socket (the default on Arch); a name server on this computer or the local
network is blocked, so such a server cannot reach the index.

Errors: exit 5 `bad_signature` (wrong signing key: add it with `mes server plan --signer`),
`stale_request`/`replay` (send the rollback again), exit 4 (not a deploy key), exit 6 (unlock
the key with `ssh-add`), exit 75 (another deploy is running).
