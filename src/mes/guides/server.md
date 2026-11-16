# Guide: setting a project up on a server

How a project gets its own accounts, folders and system units on a Linux server, with one root script the person runs.

mes never uses sudo and never installs system software. It writes one readable script; the
person reads it and runs it once with sudo; mes checks the result without root and then proves
the sandboxes on the real server.

## 1. Install the tools where only the administrator can change them

The root script refuses a tool that another account could change: every folder and link on the
way, and the program behind a script (also behind uv's long-path trampoline), must belong to root,
must not be in a home or temporary folder, and mes must run from an isolated virtual environment
of exactly the plan's version. Never install them with `--editable`. On Arch Linux:

```
sudo pacman -S --needed uv git openssh
sudo env UV_TOOL_DIR=/opt/mes/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/mes/python \
    uv tool install --python /usr/bin/python3 "git+<my-easy-software repository address>@<commit>"
sudo env UV_TOOL_DIR=/opt/mes/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/mes/python \
    uv tool install --python /usr/bin/python3 "git+<my-easy-ui repository address>@<commit>"   # dashboards
```

This gives `/usr/local/bin/mes` (its Python: `/opt/mes/tools/my-easy-software/bin/python`),
`/usr/local/bin/easy-ui` and `/usr/bin/uv` (pass `--uv /usr/bin/uv` to the plan).

## 2. Let SSH reach only the gate

In `/etc/ssh/sshd_config.d/50-mes.conf` (then `sudo sshd -t && sudo systemctl reload sshd`):

```
Match User mes-*
    AuthenticationMethods publickey
    DisableForwarding yes
    PermitTTY no
    PermitUserRC no
```

If `AllowUsers` or `AllowGroups` is set, it must let `mes-<project>` in. sshd's StrictModes
accepts the key file as the plan writes it (root's, readable by the project's group).

## 3. Two keys, on the person's computer

- signing key, best on a hardware security key: `ssh-keygen -t ed25519-sk -f ~/.ssh/mes-sign -C mes-sign`
  (without one: `ssh-keygen -t ed25519 -f ~/.ssh/mes-sign -C mes-sign`, with a passphrase);
- login key for the gate: `ssh-keygen -t ed25519 -f ~/.ssh/mes-login -C mes-login`.
The plan refuses one key for both: a stolen login key alone must not be able to install anything.
Agents get their own read or write login keys.

## 4. Plan, read, run

Run the system-installed mes (never `uv run mes`: the plan refuses a mes inside the project or
its environment), in a copy of the project:

```
/usr/local/bin/mes server plan <project> --project-dir <folder> --uv /usr/bin/uv --out plan.sh \
    --allow-host <server>.<tailnet>.ts.net \
    --signer mes-sign.pub --key deploy=mes-login.pub [--key read=agent-read.pub]
```

`--allow-host NAME` (or `--origin https://NAME[:PORT]`) is the address the dashboards are opened
with. `--allow-server-builds` lets deploys build packages that exist only as source code on the
server (off by default). The header of `plan.sh` names the mes version and a fingerprint of the
rest; the script checks it and stops if anything was changed. Read it, then `sudo bash plan.sh`.

It creates two accounts with no password and no sudo: `mes-<project>` (code, data, state; runs
the jobs and the SSH gate; its login shell only starts the gate) and `mes-<project>-web` (runs only
the dashboard server). Folders in `/var/lib/mes/<project>/` (root's, others may only pass through):
`code data state store gate webhook backups` (the project's account, 0700), `ui ui/data ui/specs`
(project, group web, 2750), `auth` (the dashboard sign-in: web account only, 0700),
`etc home home/.ssh log` (root, group project, 0750; files 0640). It writes the keys (each forced
into `mes gate --allow <level>` with `restrict`), the signers (Ed25519, deploys and rollbacks),
and every system unit (rendered by mes itself, also the dashboard server's: easy-ui only gives
checked parameters), and switches on the timers, paths and sockets. Run the plan and the script
again whenever jobs, schedules, dashboards or keys change (`mes deploy` says when).

Job units are named after the job id: `mes-<project>.job.<app>.<job>.service` (and `.timer`,
`.path`, and `mes-<project>.net.<app>.<job>.service` for a job with hosts). A job that may write
data first runs `mes db sync` (`ExecStartPre`, as the project's account, with an empty environment
and Python's isolated mode). The plan lists, for each app, where it comes from, which hosts its
jobs may reach (`network`, and `local_network` for devices on your own network) and their memory
and time limits, so you can see what each app is allowed before you run the script. Units of the
old form (`mes-<project>.job.<job>.service`) are removed by the script like any other unit the
project no longer has.

## 5. Check and prove

- `mes server check <project> [--project-dir DIR] [--no-describe]` (your account, no sudo): accounts,
  folders and modes, files, tools (every folder on the way), forced key lines, units, the run log's
  anchor. What only root can see is reported as skipped, never as passed.
- `/usr/local/bin/mes server analyze <project> <same options as the plan> --out analyze.sh`, then
  `bash analyze.sh`: systemd's own `verify` and `security` for every unit.
- After the first deploy, from your computer: `mes server selftest <project> --host <server>
  --login-key ~/.ssh/mes-login` (and on the server, as your account, `mes server selftest
  <project>` for the checks from outside). It asks the real sandboxes (read, write, deploy and a
  job's) what they can reach and reports pass, fail or skipped per claim; only passes count.

Dashboard password: `sudo -u mes-<project>-web easy-ui set-password --ui-dir
/var/lib/mes/<project>/ui --auth-dir /var/lib/mes/<project>/auth`. The dashboards listen on
`/run/mes/<project>-web.sock` (root only); forward to it with `tailscale serve` (see `mes guide setup`).

Remove: `mes server remove <project> --out remove.sh`, then `sudo bash remove.sh --confirm`. It
stops every unit and process of both accounts first, and keeps data, state and store (handed to
root) unless made with `--delete-data`. Then deploy: `mes guide deploy`.
