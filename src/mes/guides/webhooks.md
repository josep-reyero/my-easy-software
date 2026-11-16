# Guide: signed webhooks (start a job from another service)

How another service (a git host, a form, a phone shortcut) can start one of a project's jobs on a server, safely.

## In project.toml

Give the job, in its app's `app.toml`, the trigger `webhook = true` (instead of `schedule`, `after` or `manual`):

```toml
[jobs.import]
run = "import"
webhook = true
```

The job still runs in its own sandboxed system unit, with its own limits and network list. The
webhook only says "start now"; it never passes data to the job (the request body is checked,
then thrown away; only its SHA-256 is recorded).

## What the server sets up

`mes server plan <project>` adds, for a project with a webhook job:

- `mes-<project>.webhook.socket`: listens on `/run/mes-<project>-webhook.sock`, owned by root,
  mode 0600. Nothing on the network can reach it directly.
- `mes-<project>.webhook.service`: started by the socket on the first request. It runs
  `mes webhook serve` as `mes-<project>`, with no network of its own, no access to the data, and
  only `state/triggers/` writable. It stops after 60 seconds without requests.
- `mes-<project>.job.<app>.<job>.path`: starts the job when `state/triggers/<app>.<job>` changes.
- The root script writes the secret to `/var/lib/mes/<project>/etc/webhook.key` (32 random bytes,
  owned by root, readable by the project's account). The person reads it with
  `sudo cat /var/lib/mes/<project>/etc/webhook.key` and gives it to the sending service. Check it
  with `mes webhook secret-check --project <project>` as the project's account.

To receive requests from other machines, the person sets up a local proxy that runs as root and
forwards to the socket, for example Tailscale (`tailscale serve`, or `tailscale funnel` if the
sender is on the public internet). mes never runs or configures that proxy; check your Tailscale
version's documentation for forwarding to a unix socket.

## How a sender signs a request

```
POST /hooks/<app>.<job>          (the job id, for example /hooks/myproject-main.import)
X-Mes-Timestamp: <unix seconds now>
X-Mes-Signature: sha256=<hex of HMAC-SHA256(secret, "<timestamp>." + body)>
```

The secret is the raw bytes the key file holds (hex or base64 text, decoded). Example with curl,
for a secret stored as hex in `webhook.key`:

```sh
ts=$(date +%s); body='{"ref":"main"}'
sig=$(printf '%s.%s' "$ts" "$body" | openssl dgst -sha256 -mac HMAC -macopt hexkey:"$(cat webhook.key)" | sed 's/^.* //')
curl -X POST "https://<your-server-name>/hooks/myproject-main.import" \
  -H "X-Mes-Timestamp: $ts" -H "X-Mes-Signature: sha256=$sig" --data "$body"
```

To test a signature on your own computer: `mes webhook sign --secret-file <file> < body.json`
prints the two header lines (the file must be readable only by you).

## What the receiver refuses

- anything but `POST /hooks/<app>.<job>` (405 or 404);
- a missing `Content-Length` or a chunked body (411), a body over 64 KiB (413), headers over
  8 KiB (400), a request that is not complete within 10 seconds (408);
- a timestamp more than 5 minutes away from the server's clock, a wrong signature, or the exact
  same signed request a second time (401). Seen signatures are remembered for the window, also
  across restarts;
- a job that does not exist or is not `webhook = true` (404, after the signature check, so
  strangers cannot list your jobs).

Answers are short JSON bodies (`{"accepted": true}` with 202, or `{"error": "<code>"}`) without
internal details. A sender that gets 202 knows the job was asked to start, not that it finished:
see `mes jobs log --project <project> --server-root /var/lib/mes` on the server.

## Honest limits

The receiver and its checks are tested on a local unix socket. The systemd side (socket
activation, the `.path` unit noticing the trigger file, and a proxy forwarding to the socket) has
not been run on a real Linux server in this version.
