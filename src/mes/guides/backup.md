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

- If the project has a my-bucket-storage store, mes asks `mybucket snapshot` for a consistent
  copy, checks it with `mybucket verify`, and packs that copy. Without mybucket installed, mes
  refuses rather than copy a store that may be in use; `--no-store` leaves the store out (and
  says so).
- Default place: `~/.local/state/mes-backups/<project>/` (a private folder, mode 0700; each
  archive mode 0600). Use `--to` for another disk: a backup on the same disk does not survive
  the disk. mes refuses to write a backup inside the project's own folders.
- On a server, run it as the project's account with `--server-root /var/lib/mes`; the default
  place is then the project's `state/backups/`, which later backups leave out.
- Best done while none of the project's jobs is running. If a file changes while the backup is
  written, the check notices, the archive is removed, and mes asks you to run it again.

Kind: `write` (it writes a new file outside the project).

## Restoring

```sh
mes --json restore myproject /media/backup-disk/mes/myproject-20260101T080000Z.tar.gz
```

1. The archive is unpacked into a temporary folder with mes's hardened reader (regular files
   only, no links, no absolute or `..` paths, size and count limits), and every file is checked
   against the list inside it. A changed, damaged or hostile archive is refused (exit 5) before
   anything is put in place. A backup of another project is refused too.
2. Without `--yes` the answer is `needs_input`: it says what goes where and what would be moved
   aside. Show it to the user; run the `rerun` command only after they agree.
3. If the project's folders already have contents, the restore is refused unless `--replace` is
   given. With `--replace`, the current contents are moved into `.before-restore-<time>/` inside
   each folder. Nothing is ever deleted; the user can remove those folders when they are happy.

Kind: `admin` (it replaces the project's data), so sessions with `MES_ALLOW=read` or `write`
cannot restore.

## Off-machine copies

Copy the archive to another disk or computer you own. For a project with a store, my-bucket-
storage's own replication (`mybucket sync`) is the other way to keep a second copy of the store.
