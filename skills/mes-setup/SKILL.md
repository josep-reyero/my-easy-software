---
name: mes-setup
description: Get a computer ready for **my-easy-software** (mes) projects, one safe step at a time, and serve a project's dashboard privately with Tailscale. Use when the user wants to install or set up mes, uv, git or Tailscale, asks why "mes" is not found, runs "mes setup" or "mes doctor", or wants to open their dashboard from their phone on their private network.
---

# Set up mes

Run `mes guide setup` and follow it exactly. If `mes` is not installed yet, the guide's first
section explains how to get it.

Then run `mes --json setup` and work through its checklist one item at a time: explain each step
to the user in plain words, ask before installing anything, and let the user type every password
or login themselves. Never run commands with sudo for them.
