# Guide: setting a project up on a server

How a project gets its own accounts, folders and system units on a Linux server, with one root script the person runs.

mes never uses sudo and never installs system software. It writes one readable script; the
person reads it and runs it once with sudo; mes checks the result without root.

Before: on the server, install mes, uv and (for dashboards) easy-ui system-wide, where only the
administrator can change them (default `/usr/local/bin/mes`, `/usr/local/bin/uv`,
`/usr/local/bin/easy-ui`; others with `--mes`, `--uv`, `--easy-ui`). The script stops if a tool
or a folder above it could be changed by another account.

Keys (on the person's computer, each with a passphrase the person types):
- signing key for deploys: `ssh-keygen -t ed25519 -f ~/.ssh/mes-deploy -C mes-deploy`
- an SSH key per level for agents, if wanted (read, write).

Plan (in the project folder; nothing changes anywhere):

```
mes server plan <project> --out plan.sh \
    --signer ~/.ssh/mes-deploy.pub --key deploy=~/.ssh/mes-deploy.pub --key read=agent-read.pub
```

Then copy `plan.sh` to the server, read it, and run `sudo bash plan.sh`. It:
- creates `mes-<project>` (owns code, data and state; runs the jobs and the SSH gate) and
  `mes-<project>-web` (runs only the dashboard server and can read only `ui/`). Neither has a
  password or sudo; `mes-<project>`'s login shell only starts the gate.
- creates `/var/lib/mes/<project>/` (code, data, state, store, ui, web, etc, home, log), each
  readable only by the account that needs it;
- writes the SSH keys, each forced into `mes gate --project <project> --allow <level>` with
  `restrict` (no shell, terminal, forwarding or agent), and the deploy signers (Ed25519 only);
- writes the project's system units (jobs, timers, the network helper for jobs with hosts, the
  dashboard data refresh, the gate's per-request sandboxes, webhooks, and easy-ui's dashboard
  socket and service) and switches on the timers and sockets; removes units of the project that
  are no longer planned.
It touches nothing else, and running it again is safe. Run `mes server plan` and the script
again whenever jobs, schedules, dashboards or keys change (`mes deploy` tells you when).

Check (on the server, no sudo): `mes server check <project> [--project-dir DIR]`, a checklist of
accounts, folders, files, tools, units and whether they are switched on.

Remove: `mes server remove <project> --out remove.sh`, then `sudo bash remove.sh --confirm`. It
keeps the data (handed to root) unless the script was made with `--delete-data`.

Then deploy from your computer: `mes guide deploy`.
