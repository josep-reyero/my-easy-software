# Guide: setting up mes on this computer

How to get a computer ready for mes projects, driven by you (the agent) without blocking on prompts.

0. Get `mes` itself (it is not published on a package index; never install a package called
   my-easy-software from one, since anyone could publish that name there): with the user's yes,
   clone the repository the user names (`git clone <my-easy-software repository address>`), check
   that it is the one they mean (the address they gave, and `git log -1` shows the commit they
   expect), and run `uv tool install <the my-easy-software folder>`. `mes` is then available
   everywhere. (Without installing it: `uv run --project <the my-easy-software folder> mes ...`.
   Inside a project made with `mes new`, `uv run mes ...` works on this computer; on a server use
   the system-installed mes, see `mes guide server`.) If `uv` itself is missing, `mes doctor`
   cannot run yet: install uv with the system's package manager or as uv's own instructions say
   (https://docs.astral.sh/uv/getting-started/installation/).

1. Run `mes --json setup`. It returns a checklist:
   `{"ok": bool, "items": [{"id", "title", "status", "why", "fix", "needs_human", "detail"}]}`.
   `status` is `done`, `todo`, `blocked` or `skipped`.
2. For each item that is not `done` or `skipped`:
   - `needs_human: false` and a `fix` command: tell the user in plain words what it does and why
     (use `why`), and run `fix` only after they say yes.
   - `needs_human: true`: the user must do it themselves (create an account, sign in, type a
     password, touch a hardware key). Show them `title` and `why`, wait until they say it is done.
3. Run `mes --json setup` again until `ok` is true. Use `mes --json doctor` later to check that
   everything still works.

Special results (exit code 6):
- `{"status": "needs_input", "question", "why", "options", "recommended", "rerun"}`: ask the user
  the question, show each option's `label` and `meaning`, say which one is recommended, then run
  `rerun` with their answer.
- `{"status": "needs_human", "action", "why", "url", "rerun"}`: show the action (and the link if
  there is one); when the user has done it, run `rerun`.

Talking to the user:
- They may not be a developer. Say what is happening, why it matters to them and what comes next.
  Offer technical detail only if they ask.
- Safety steps: say what they protect ("this keeps your data on your own devices").
- Never skip a safety step silently. If the user declines one, tell them what will and will not
  work without it.
- Install software only after the user says yes, and only from official package sources.
- Never ask the user to edit a file or type a command, except their own passwords.

Tailscale (private network): mes never runs `tailscale up` (it changes the computer's network
settings); when a sign-in is needed, it tells the person to run it. If Tailscale is missing, explain that it links the user's own devices into
a private network only they can enter, that it is free for personal use and takes about two
minutes, and offer to install it. If they decline, projects stay in local-only mode on this
computer: say so.

Serving a project's dashboard on the private network (on the server): run
`mes --json setup --project <name>` (inside a project folder whose project.toml has `[ui]`, plain
`mes --json setup` does the same). The checklist then ends with the item `tailscale-serve`,
"Serve the dashboard on your private network". It is always `needs_human`: mes never runs its
commands. Its `why` (and, line by line, its `detail`) gives:
- the command to run once on the server, with the user's yes:
  `sudo tailscale serve --bg --https=443 unix:/run/mes/<name>-web.sock`
  (`--socket PATH` changes the dashboard server's socket, `--port N` the HTTPS port; use another
  port for each project served from the same server);
- the private name the dashboard server must accept, given when its units are made:
  `mes server plan <name> --allow-host <server>.<tailnet>.ts.net` (the same as
  `--origin https://<server>.<tailnet>.ts.net`; mes passes it to `easy-ui units --origin` and
  renders the units `mes-<name>-web.socket` and `mes-<name>-web.service` itself), then the
  script it prints is run as before;
- the dashboard password, set once on the server:
  `sudo -u mes-<name>-web easy-ui set-password --ui-dir /var/lib/mes/<name>/ui --auth-dir /var/lib/mes/<name>/auth`. The name comes from `tailscale status` when this
  computer is the server, otherwise it is a placeholder to replace;
- how to check (`tailscale serve status`) and how to stop (`sudo tailscale serve --https=443 off`).
Tell the user it makes the dashboard open on their own devices only. Never use `tailscale funnel`:
that would put the dashboard on the public internet. To share a dashboard with other people, see
`mes guide share`. The item is `blocked` until Tailscale runs and is signed in, and `skipped` in
local-only mode.

Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 4 not allowed, 5 refused for safety, 6 needs the
user, 75 temporary failure (retry later).
