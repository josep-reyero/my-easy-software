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
   sibling path dependency (`path = "../<folder>"`, such as `../my-easy-software`);
2. builds a wheel of the project and of each such dependency from that export (`uv build --wheel`);
3. signs the archive with `ssh-keygen -Y sign -n mes-deploy` and sends it through the gate, which
   accepts it only from a `deploy` key. A rollback is a short request (project, release, time,
   random nonce) signed in the namespace `mes-rollback`; the server accepts it within 5 minutes,
   once.

On the server the gate:
1. checks the signature against the project's allowed signers **before unpacking anything**,
   reading one open file that has already left the folder;
2. unpacks with a hardened extractor (plain files and folders only, size and count limits);
3. refuses an archive already installed or older than an installed one; a release that was
   interrupted is removed and never blocks sending it again;
4. in the deploy sandbox (no access to this computer's own services or the local network, uv's
   cache and Python under `code/`, no user settings): `uv sync --frozen --no-build` for the
   third-party packages, then installs the archive's own wheels offline; then asks every app to
   describe itself in a read sandbox (which cannot change any release) and checks the jobs;
5. records the release as complete, switches `current` in one step, publishes the dashboards
   (switching back if that fails), and keeps the newest 5 releases.

What remains: third-party packages still come from the package index during `uv sync` (checked
against the hashes in `uv.lock`); a package that has only source code fails unless the plan was
made with `--allow-server-builds`, and then its build code runs in the deploy sandbox. The deploy
sandbox finds names through systemd-resolved (the default on Arch); a name server on this
computer or the local network is blocked, so such a server cannot reach the index.

Errors: exit 5 `bad_signature` (wrong signing key: add it with `mes server plan --signer`),
`stale_request`/`replay` (send the rollback again), exit 4 (not a deploy key), exit 6 (unlock
the key with `ssh-add`), exit 75 (another deploy is running).
