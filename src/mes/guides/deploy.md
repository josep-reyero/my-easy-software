# Guide: deploying a project to a server

How to install a new version of a project on a server set up with mes server plan, and how to go back.

```
mes --json deploy my-server --dry-run     # every step, nothing signed or sent
mes --json deploy my-server               # pack, sign, send, install, switch
mes --json deploy my-server --releases    # what is installed
mes --json deploy my-server --rollback [--to N]
```

`my-server` is the server's SSH name; mes connects as `mes-<project>@my-server` (give
`user@host` to change it) with your deploy key in your SSH agent (`ssh-add ~/.ssh/mes-deploy`).

On this computer mes:
1. packs only the files tracked by git (links are refused; `project.toml`, `pyproject.toml` and
   `uv.lock` must be tracked) plus sibling path dependencies (`path = "../<folder>"`, such as
   `../my-easy-software`; paths inside the project, such as `vendor/<app>`, are packed with it).
   `apps.lock.json` is never packed: it holds this computer's paths. Names that look like secrets
   are listed in the notes.
2. signs the archive with `ssh-keygen -Y sign -n mes-deploy` and your Ed25519 key
   (default `~/.ssh/mes-deploy`, or `--key`, or `[deploy] key` in `~/.config/mes/config.toml`).
3. sends it through the server's gate, which only accepts it from a `deploy` key.

On the server the gate:
1. checks the signature against the project's allowed signers **before unpacking anything**;
2. unpacks with a hardened extractor (plain files and folders only, no links, no absolute or `..`
   paths, size and count limits) into a new release folder;
3. refuses an archive that is already installed or older than an installed one (an old signed
   archive cannot quietly come back; use `--rollback` on purpose);
4. runs `uv sync --frozen` in its own sandbox, then `mes apps sync` there: it finds every app, asks
   each to describe itself, checks the jobs' commands (problems become notes) and writes the
   release's own `apps.lock.json`;
5. switches `current` to the new release in one step, publishes the dashboards (switching back if
   that fails), and keeps the newest 5 releases (always the running one and the one before).

If the jobs or dashboards changed since `mes server plan`, the answer says so: run the plan and
its script again so the schedules match.

Errors: exit 5 `bad_signature` (wrong or missing signing key on the server: add it with
`mes server plan --signer`), exit 4 (the key is not a deploy key), exit 6 (needs the person:
unlock the key with `ssh-add`), exit 75 (another deploy is running).
