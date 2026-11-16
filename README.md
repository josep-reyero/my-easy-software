# my-easy-software

> **Consumer apps should ship APIs that let people consume their own data.
> People then set up their own dashboards and own their data, in an AI-native,
> fully flexible way.**

That is the idea behind this project. Today most apps keep your data behind their own screens:
you see what they chose to show, the way they chose to show it. We think it should work the
other way round:

- **Apps expose your data.** Every app ships an API or a command-line tool for getting your
  data out.
- **You own the data.** It lives in storage you control, on your own machine or a server you
  choose, in formats you can always export.
- **Dashboards are yours and disposable.** A dashboard is a small, flexible view over your
  data. Make one, change it, throw it away.
- **AI-native.** Every app has a command-line tool an AI agent can use. Your own coding agent
  can query your data and reshape your dashboards on the fly, with the permissions you give it.

my-easy-software is a framework for building small personal apps this way, as easily as
starting a web project: create an app, preview it, deploy it to your own machine or server.

You do not need to be a developer. Setup is guided by an AI agent that explains each step in
plain words, installs what is needed with your permission, and keeps your apps private to your
own devices unless you choose to share them.

## Status

Version 1 round (in progress). A **project** (`project.toml`) holds one or more apps, their
jobs and their dashboards. What works:

- `mes new` creates a project whose app stores data in SQLite and whose dashboard works at once;
  `mes dev` shows it on this computer with one link (needs [my-easy-ui](https://github.com/josep-reyero/my-easy-ui)).
- `mes server plan` writes one readable root script that sets a project up on a Linux server with
  its own two accounts, folders and sandboxed system units; you run it once with sudo, and
  `mes server check` verifies it. mes itself never uses sudo.
- `mes deploy` sends signed releases (OpenSSH signatures, checked before anything is unpacked)
  through the server's SSH gate, with rollback.
- Jobs on a server have no network unless they list hosts, and then reach only those hosts.
- Agents reach apps through one SSH entry point (`mes gate`), each request in its own sandbox.
- `mes expose` shares chosen dashboards (Cloudflare Pages, Netlify, Vercel) with named people,
  anyone with the link (encrypted), or everyone, always after your explicit yes.
- Signed webhooks as job triggers, `mes backup` / `mes restore`, agent skills in `skills/`.

The server parts are tested on this computer with fakes; see `docs/design.md` for what still
needs a real Linux server to be confirmed.

## Quick start

You need [uv](https://docs.astral.sh/uv/) (it installs Python and the projects). Your AI agent can
do every step below for you; `mes` answers in plain words and, with `--json`, in data an agent
can read.

```sh
# 1. Get mes (it is not published yet): download this repository, then
uv tool install --editable ./my-easy-software      # "mes" now works in any folder

# 2. Check this computer and set it up, one step at a time
mes setup

# 3. Create a project next to the my-easy-software folder and try it
mes new my-notes
cd my-notes && uv sync
uv run my-notes add "My first note"
uv run mes jobs run rebuild
uv run mes dev                                     # one link to the dashboard

# 4. Later: put it on your own server (see: mes guide server, mes guide deploy)
mes server plan my-notes --out plan.sh --signer ~/.ssh/mes-deploy.pub --key deploy=~/.ssh/mes-deploy.pub
mes deploy my-server
```

Without installing mes, `uv run --project ./my-easy-software mes ...` works too. Inside a
project, `uv run mes ...` always works. Guides for agents: `mes guide` lists them. Agent skills
(and a Claude Code plugin manifest in `.claude-plugin/`) are in `skills/`. Instructions for
agents working on this repository: [AGENTS.md](AGENTS.md).

Pieces:

- **my-easy-software** (this repo): the framework and its `mes` command for creating, previewing,
  deploying and sharing projects.
- **[my-easy-ui](https://github.com/josep-reyero/my-easy-ui)**: the dashboard server and renderer. Dashboards are plain description files with
  no code in them.
- **[my-bucket-storage](https://github.com/josep-reyero/my-bucket-storage)**: space-efficient storage with an SDK.
- **[fitdata-garmin](https://github.com/josep-reyero/fitdata-garmin)**: the first app, a personal
  archive of Garmin fitness data.

## Principles

- **Secure by default.** Private-network access unless you open it, sign-in always on, each app
  boxed in, and no path from a server back to your laptop.
- **Light.** Nothing runs when nothing is in use.
- **No lock-in.** Your data can always be exported as plain files.
- **Nothing personal in the repos.** Machine names, paths and credentials live only in
  per-machine configuration.

## Licence

MIT
