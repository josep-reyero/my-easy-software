# Guide: setting a project up on a server

How a project gets its own accounts, folders and system units on a Linux server, with one root script the person runs.

mes never uses sudo and never installs system software. It writes one readable script; the
person reads it and runs it once with sudo; mes checks the result without root and then proves
the sandboxes on the real server.

## 1. Install the tools where only the administrator can change them

The root script refuses a tool that another account could change: every folder and link on the
way, and the program behind a script (also behind uv's long-path trampoline), must belong to
root, must be in `/usr`, `/bin`, `/lib` or `/opt/mes` (the project's services see nothing else),
mes must run from an isolated virtual environment with its own code inside it (never an
editable install), and it must be exactly the build that made the plan. Install each tool at an
exact commit, with exactly the versions in its `uv.lock`. On Arch Linux:

```
sudo pacman -S --needed uv git openssh
sudo install -d -o root -g root -m 0755 /opt/mes
sudo git clone <my-easy-software repository address> /opt/mes/my-easy-software
sudo git -C /opt/mes/my-easy-software checkout --detach <commit>
cd / && sudo env -i PATH=/usr/bin:/bin HOME=/root UV_CACHE_DIR=/opt/mes/uv-cache \
    UV_PYTHON_INSTALL_DIR=/opt/mes/python /usr/bin/uv sync --locked --no-dev --no-editable \
    --python /usr/bin/python3 --project /opt/mes/my-easy-software
sudo ln -sfn /opt/mes/my-easy-software/.venv/bin/mes /usr/local/bin/mes
# the dashboards, the same way:
sudo git clone <my-easy-ui repository address> /opt/mes/my-easy-ui
sudo git -C /opt/mes/my-easy-ui checkout --detach <commit>
cd / && sudo env -i PATH=/usr/bin:/bin HOME=/root UV_CACHE_DIR=/opt/mes/uv-cache \
    UV_PYTHON_INSTALL_DIR=/opt/mes/python /usr/bin/uv sync --locked --no-dev --no-editable \
    --python /usr/bin/python3 --project /opt/mes/my-easy-ui
sudo ln -sfn /opt/mes/my-easy-ui/.venv/bin/easy-ui /usr/local/bin/easy-ui
```

Check the result (each `find` must print nothing; `git log` must show the commit you chose):

```
sudo find /opt/mes \! -user root -print
sudo find /opt/mes -perm /022 \! -type l -print
git -C /opt/mes/my-easy-software log -1 --format=%H
/usr/local/bin/mes --version
```

This gives `/usr/local/bin/mes` (its Python: `/opt/mes/my-easy-software/.venv/bin/python`, which
`mes server plan` reads from the program's first line), `/usr/local/bin/easy-ui` and
`/usr/bin/uv` (pass `--uv /usr/bin/uv` to the plan). The plan's header names the mes version
and its build (a hash of the installed mes package's files), and the script stops if the mes on
the server is another build. To update: `git fetch`, `checkout --detach` the new commit, the same
`uv sync` line, then make and run the plan again.

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

Run the system-installed mes by its full path (never `uv run mes`: the plan refuses a mes inside
the project or its environment), in a copy of the project:

```
/usr/local/bin/mes server plan <project> --project-dir <folder> --uv /usr/bin/uv --out plan.sh \
    --allow-host <server>.<tailnet>.ts.net \
    --signer mes-sign.pub --key deploy=mes-login.pub [--key read=agent-read.pub] [--no-start]
```

`--allow-host NAME` (or `--origin https://NAME[:PORT]`) is the address the dashboards are opened
with. `--allow-server-builds` lets deploys build packages that exist only as source code on the
server (off by default; their build code then runs in the install step, which then gets a cache
and a Python of the release's own instead of the shared ones, see `mes guide deploy`). `--no-start` switches on nothing that runs jobs (their timers, webhook triggers and the
webhook receiver) and prints the exact `systemctl enable --now` line to run later; use it for a
first install on a server that is getting an existing archive (section 6). The script's last
lines say which timers and triggers are running. The header of `plan.sh` names the mes version
and build and a fingerprint of the rest; the script checks both and stops if anything was
changed. Read it, then `sudo bash plan.sh`. It needs systemd 249 or newer.

It creates two accounts with no password and no sudo: `mes-<project>` (code, data, state; runs
the jobs and the SSH gate; its login shell only starts the gate) and `mes-<project>-web` (runs only
the dashboard server). Folders in `/var/lib/mes/<project>/` (root's, others may only pass through):
`code data state store gate webhook backups` (the project's account, 0700), `ui ui/data ui/specs`
(project, group web, 2750), `auth` (the dashboard sign-in: web account only, 0700),
`etc home home/.ssh log` (root, group project, 0750; files 0640). It writes the keys (each forced
into `mes gate --allow <level>` with `restrict`), the signers (Ed25519, deploys and rollbacks),
and every system unit (rendered by mes itself, also the dashboard server's: easy-ui only gives
checked parameters), and switches on the sockets and (without `--no-start`) the timers and paths.
Run the plan and the script again whenever jobs, schedules, dashboards or keys change (`mes
deploy` says when). Units that carry the project's mark but are not in the plan are switched off
and removed.

Job units are named after the job id: `mes-<project>.job.<app>.<job>.service` (and `.timer`,
`.path`, and `mes-<project>.net.<app>.<job>.service` for a job with hosts). A job that may write
data first runs `mes db sync` (`ExecStartPre`, as the project's account, with an empty environment
and Python's isolated mode). The plan lists, for each app, where it comes from, which hosts its
jobs may reach (`network`, and `local_network` for devices on your own network) and their memory
and time limits, so you can see what each app is allowed before you run the script. Installing
an app gives its code the right to run as the project's account inside these sandboxes.

What every unit that runs project code sees: the project's own folders (bound back one by one,
read-only unless the unit writes them), `/usr` and `/etc` read-only without the system's secrets
(`/etc/shadow`, `/etc/ssh`, `/etc/sudoers*`, `/etc/ssl/private`, network and VPN settings and
keys, ...), `/opt/mes` and the program folders of `/usr/local`. `/var`, `/run`, the rest of
`/opt` and `/usr/local` are empty; `/home`, `/root`, `/srv`, `/mnt`, `/media` and `/boot` are not
there. So no service socket of the server (systemd, D-Bus, systemd-resolved, userdb, the journal,
Tailscale, Docker, containerd, Podman, nscd, CUPS, Avahi) can be reached. A job without network
has a private network, with its own abstract unix sockets. A job that lists hosts shares the
server's network, where other programs' abstract unix sockets live, so it may not open unix
sockets at all (socket pairs inside the job still work).

The install step of a deploy (and `mes server run-as --network any`) has the internet, but not
this computer, link-local, multicast, private, Tailscale (`100.64.0.0/10`) or IPv6 unique-local
addresses. Not covered, because they cannot be known when the units are written and can change
while the server runs: this server's own public addresses (a service listening on all addresses
can be reached through them) and the global IPv6 addresses of other machines on the local
network. The plan's header says the same.

## 5. Check and prove

- `mes server check <project> [--project-dir DIR] [--no-describe]` (your account, no sudo): accounts,
  folders and modes, files, tools (every folder on the way), forced key lines, units, the run log's
  anchor. What only root can see is reported as skipped, never as passed.
- `/usr/local/bin/mes server analyze <project> <same options as the plan> --out analyze.sh`, then
  `bash analyze.sh`: systemd's own `verify` and `security` for every unit.
- After the first deploy, from your computer: `mes server selftest <project> --host <server>
  --login-key ~/.ssh/mes-login` (and on the server, as your account, `/usr/local/bin/mes server
  selftest <project>` for the checks from outside). It asks the real sandboxes (read, write,
  install, publish, a job's and a job's that lists hosts) what they can reach and reports pass,
  fail or skipped per claim; only passes count. When it cannot tell whether systemd's address
  filter is attached, it says so (todo) instead of passing.

Dashboard password (always `cd /` first and full paths after `sudo -u`):
`cd / && sudo -u mes-<project>-web /usr/local/bin/easy-ui set-password --ui-dir
/var/lib/mes/<project>/ui --auth-dir /var/lib/mes/<project>/auth`. The dashboards listen on
`/run/mes/<project>-web.sock` (root only); forward to it with `tailscale serve` (see `mes guide
setup`). `tailscale serve` on port 443 replaces whatever is already served there: look first with
`tailscale serve status`, and if 443 is taken use another port (`--https=8443`) and make the plan
with `--origin https://<server>.<tailnet>.ts.net:8443`. Never `tailscale funnel`: it would put
the dashboards on the public internet.

## 6. Moving an existing project onto a server

1. Stop the old schedule first (the jobs on the old computer: its timers, cron lines or a loop),
   so two copies never fetch or write at the same time. Run one schedule at a time.
2. Make and run the plan with `--no-start`, then deploy (`mes guide deploy`).
3. Copy the data as the project's account, never as root, so every file belongs to it: for
   example `cd / && sudo -u mes-<project> /usr/bin/tar -xf /path/to/data.tar -C /var/lib/mes/<project>/data`
   (or `mes restore`, see `mes guide backup`). Never copy a database the framework builds (the
   shared `<project>.sqlite` and the dashboards' data copies): mes and the dashboard refresh
   make them again from the apps' own files (`mes db rebuild`).
4. Check owners and modes: `sudo find /var/lib/mes/<project>/data \! -user mes-<project> -print`
   must print nothing, and nothing in data/ may be readable by others
   (`sudo find /var/lib/mes/<project>/data -perm /077 -print`).
5. Switch the schedules on with the `systemctl enable --now` line the script printed.

## 7. Interactive commands

Some app commands need a terminal, such as an app's login that asks for a password or a code.
They run as the project's account, in a transient unit with the same sandbox as a job, started by
the owner with `systemd-run --pty`. mes prints the exact command (it never runs sudo itself):

```
/usr/local/bin/mes server run-as <project> --project-dir <folder> -- <app program> <command> [options]
```

Run the `sudo systemd-run --pty ...` line it prints, on the server, in a terminal. The command
sees the project's data, state and store (writable), its code (read-only) and nothing else of
the server. It has no network at all unless asked, and then cannot even look names up:

- `--network HOST` (repeatable): only the internet addresses those hosts have now (looked up
  when the command is printed; print it again if they change), with names through
  systemd-resolved's socket. A host that has only local-network addresses is refused.
- `--network any`: the internet, but not this computer, link-local, multicast, private,
  Tailscale (`100.64.0.0/10`) or IPv6 unique-local addresses. Not covered: this server's own
  public addresses and the global IPv6 addresses of other machines on the local network; the
  command it prints says so too.

## 8. Remove

`mes server remove <project> --out remove.sh`, then `sudo bash remove.sh --confirm`. It stops
every unit and process of both accounts first, and keeps data, state and store (handed to root)
unless made with `--delete-data`. Then deploy: `mes guide deploy`.
