# Guide: backups (mes backup and mes restore)

How to keep a checked copy of a project's data and state, and how to put it back.

## Making a backup

```sh
mes --json backup                      # the project in this folder
mes --json backup myproject --to /media/backup-disk/mes
```

It copies the project's data folder and state folder (only plain files; links are skipped and
listed under `skipped`) into one archive, `<project>-<UTC time>.tar.gz`, then reads the archive
back and checks every file's size and SHA-256 against the list inside it (`verified: true`).

- The project's shared database (`<project>.sqlite`) is copied with SQLite's own backup, which
  waits for a writer to finish, so the archive never holds half of a transaction (its
  `-journal` file is left out). A file that is not a readable database is copied as it is, with a
  note.
- **Signed.** When a signing key is set up (the same key as for deploys: `--key`, `[deploy] key`
  in the config file, or `~/.ssh/mes-sign`), the backup is signed with it
  (`ssh-keygen -Y sign -n mes-backup`) and the signature is written next to it as
  `<archive>.sig`. Keep the two files together. A hardware key asks for a touch.
- **Unsigned** (no key here, or `--no-sign`): the answer gives the archive's SHA-256. The check
  inside the archive only proves it is complete and unchanged since it was written
  (integrity), not who made it (origin). Write the SHA-256 down somewhere else and compare it
  before restoring.
- Never in a backup: the run log (`state/runs.jsonl`) and the gate's log (`state/gate/`). They
  record what happened, so a restore must not replace them.
- If the project has a my-bucket-storage store, mes asks `mybucket snapshot` for a consistent
  copy, checks it with `mybucket verify`, and packs that copy. Without mybucket installed, mes
  refuses rather than copy a store that may be in use; `--no-store` leaves the store out (and
  says so).
- Default place: `~/.local/state/mes-backups/<project>/` (a private folder, mode 0700; each
  archive mode 0600). Use `--to` for another disk: a backup on the same disk does not survive
  the disk. mes refuses to write a backup inside the project's own folders.
- On a server, run it as the project's account with `--server-root /var/lib/mes`
  (`sudo -u mes-<project> mes backup <project> --server-root /var/lib/mes`). The default place
  is then `/var/lib/mes/<project>/backups/`, which the root script makes and which no job or gate
  request can see or change (never `state/`, which the project's own jobs can write).
- Only the account that owns the project's folders may run it, and never root: as root, mes
  would follow whatever the project's code left in its folders. Every file is opened without
  following links, and is hashed from the same bytes that go into the archive.
- Best done while none of the project's jobs is running. If a file changes while the backup is
  written, the check notices, the archive is removed, and mes asks you to run it again.

Kind: `write` (it writes a new file outside the project).

## Restoring

```sh
mes --json restore myproject /media/backup-disk/mes/myproject-20260101T080000Z.tar.gz
```

1. The signature is checked first, before anything is unpacked. A signed backup must verify
   against a trusted key: your own signing key's public half, any `--signer <key.pub>`, and on
   a server the project's deploy signing keys. If it does not, it is refused (exit 5). An
   unsigned backup is refused when a signing key is set up here, unless you add
   `--allow-unsigned` (only when you know where the file came from).
2. The archive is unpacked with mes's hardened reader (regular files only, no links, no absolute
   or `..` paths, size and count limits) into a private folder inside each destination folder,
   and every file is checked against the list inside it. A changed, damaged or hostile archive is
   refused before anything is put in place. A backup of another project is refused too, and so is
   one whose time of making is not a real date and time.
3. Without `--yes` the answer is `needs_input`, with the recommended answer **no**: it says, in
   mes's own words only, what goes where, what would be moved aside, and whether the backup is
   signed (or only its SHA-256, when it is not). Show it to the user; run the `rerun` command
   only after they agree.
4. If the project's folders already have contents, the restore is refused unless `--replace` is
   given. With `--replace`, the current contents are moved into `.before-restore-<time>/` inside
   each folder. Nothing is ever deleted; the user can remove those folders when they are happy.
   Every move is a rename inside the folder, never over an existing name.
5. The run log, the gate's log and mes's own working folders are never restored, never moved
   aside and never written into, even when an (old or hostile) archive holds them; they are
   listed under `not_restored`.

Only the account that owns the project's folders may restore, never root. Kind: `admin` (it
replaces the project's data), so sessions with `MES_ALLOW=read` or `write` cannot restore.

## Off-machine copies

Copy the archive (and its `.sig`) to another disk or computer you own. For a project with a
store, my-bucket-storage's own replication (`mybucket sync`) is the other way to keep a second
copy of the store.
