# Guide: setting up mes on this computer

How to get a computer ready for mes apps, driven by you (the agent) without blocking on prompts.

0. Get `mes` itself (it is not published yet): with the user's yes, download the my-easy-software
   repository and run `uv tool install --editable <the my-easy-software folder>`. `mes` is then
   available everywhere. (Without installing it: `uv run --project <the my-easy-software folder> mes ...`.
   Inside an app made with `mes new`, `uv run mes ...` always works.) If `uv` itself is missing,
   `mes doctor` cannot run yet: install uv from its official source first (https://docs.astral.sh/uv/).

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

Tailscale (private network): if it is missing, explain that it links the user's own devices into
a private network only they can enter, that it is free for personal use and takes about two
minutes, and offer to install it. If they decline, apps stay in local-only mode on this computer:
say so.

Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 4 not allowed, 5 refused for safety, 6 needs the
user, 75 temporary failure (retry later).
