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

Early design. Nothing here is usable yet.

Planned pieces:

- **my-easy-software** (this repo): the framework and its `mes` command for creating, previewing
  and deploying apps.
- **my-easy-ui**: the dashboard server and renderer. Dashboards are plain description files with
  no code in them.
- **my-bucket-storage**: space-efficient storage with an SDK.
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
