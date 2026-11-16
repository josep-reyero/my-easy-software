"""``mes expose <project>``: share chosen dashboards as a static copy on a hosting provider.

Sharing publishes a **static export** made by ``easy-ui export``: the renderer, the chosen
dashboards and only the data those dashboards use, with the query results for the control values.
No server code of ours runs on the internet and nothing listens on this computer: mes pushes the
files to the provider with the person's own account, and that is all.

Steps
-----
1. Check the request: the project has ``[ui]``, the provider supports the mode, the emails (for
   ``people``) and the site name are plain.
2. Build the export into a new private temporary folder (``easy-ui export``) and look at every
   file it wrote (regular files only, safe paths, size limits).
3. In ``link`` mode, check that the key is in no file.
4. Ask the person (``needs_input``, exit 6) with a question that lists exactly which dashboards,
   which tables and columns, which provider and site, and who will be able to open it. The answer
   is a fingerprint (``--answer confirm=<fingerprint>``) of all of that and of every exported
   file, so a yes only ever covers what was shown: when anything changed, mes asks again.
   ``--yes`` skips the question (the person said yes beforehand).
5. Upload with the adapter (``mes.expose.adapters``). ``--dry-run`` stops before this and lists
   every request the adapter would make; it needs no token.
6. In ``link`` mode the link is ``<site>#k=<key>``: the key is only in the part after ``#``,
   which browsers never send to the provider, and the adapter never receives it.

Public API
----------
``MODES``, ``WHO``, ``expose(...) -> dict``, ``fingerprint(summary, files, *, encrypted) -> str``,
``collect_files(folder) -> list[ExportFile]``, ``register(subparsers)``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from mes import manifest as manifest_mod
from mes import paths, protocol, siblings
from mes.app import check_allowed
from mes.errors import MesError, NotFound, Refused, UsageError
from mes.expose.adapters import ADAPTERS, Adapter, ExportFile, Http, api_base, load_token

MODES = ("people", "link", "public")
WHO = {
    "people": "only the people listed, after they sign in with their email address",
    "link": "anyone holding the link (the data is encrypted; the key is only in the link)",
    "public": "anyone on the internet",
}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+\Z")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}\Z")
MAX_FILES = 20_000
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_PEOPLE = 100
EXPORT_TIMEOUT = 600


# --------------------------------------------------------------------------- checks


def _project(project_name: str, project_dir: str | None) -> manifest_mod.Project:
    paths.check_name(project_name)
    if project_dir is not None:
        project = manifest_mod.load(Path(project_dir).expanduser())
    else:
        found = manifest_mod.find()
        if found is None:
            raise NotFound(
                "There is no project in this folder (no project.toml here or above), so mes does not know what "
                "to share.", hint="Run the command inside the project's folder, or add --project-dir <folder>.")
        project = manifest_mod.load(found)
    if project.name != project_name:
        raise UsageError(f'The project in this folder is "{project.name}", not "{project_name}", so nothing was '
                         "shared.", hint=f"Run: mes expose {project.name} ...")
    if not project.has_dashboards:
        raise Refused(f'The project "{project.name}" has no dashboards ([ui] in project.toml), so there is nothing '
                      "to share.", hint="Add dashboards first (mes guide dashboards).", code="no_dashboards")
    return project


def _people(mode: str, people: list[str]) -> list[str]:
    if mode != "people":
        if people:
            raise UsageError("--people only goes with --mode people.",
                             hint="Leave --people out, or use --mode people.")
        return []
    if not people:
        raise UsageError("People mode needs the email address of everyone who may open the dashboards.",
                         hint="Add --people someone@example.com (once per person).")
    clean = []
    for p in people:
        e = p.strip().lower()
        if not EMAIL_RE.match(e):
            raise UsageError(f"{p[:80]!r} is not a plain email address, so nothing was shared.",
                             hint="Write each address like name@example.com.")
        clean.append(e)
    if len(set(clean)) > MAX_PEOPLE:
        raise UsageError(f"At most {MAX_PEOPLE} people can be listed.", hint="Share with fewer people.")
    return sorted(set(clean))


def _dashboards(project: manifest_mod.Project, chosen: list[str]) -> list[str]:
    root = project.root
    listed = list(project.ui.dashboards) if project.ui else []
    names = list(dict.fromkeys(chosen)) or listed
    if not names and root is not None:
        names = sorted(p.stem for p in (root / "ui" / "dashboards").glob("*.yaml"))
    if not names:
        raise UsageError("No dashboard was chosen, and the project lists none.",
                         hint="Name them with --dashboard <name> (once per dashboard).")
    for n in names:
        if not manifest_mod.UI_DASHBOARD_RE.match(n):
            raise UsageError(f"{n[:60]!r} is not a dashboard name.", hint="Use the dashboard's file name without .yaml.")
        exists = root is not None and ((root / "ui" / "dashboards" / f"{n}.yaml").is_file()
                                       or (root / "ui" / "dashboards" / f"{n}.yml").is_file())
        exists = exists or any(n in a.ui.dashboards for a in project.apps.values())
        if not exists and n not in listed:
            raise NotFound(f'The project has no dashboard called "{n}", so nothing was shared.',
                           hint="See its dashboards in ui/dashboards/.")
    return names


def _site(site: str | None, project: manifest_mod.Project, adapter: Adapter) -> str:
    name = site or project.name
    if not adapter.site_re.match(name):
        raise UsageError(f"{name[:80]!r} cannot be used as a site name on {adapter.title}.",
                         hint="Use lowercase letters, digits and dashes, for example --site my-dashboards.")
    return name


def _unsafe(problem: str) -> Refused:
    return Refused(f"The export {problem}, so it was not shared.",
                   hint="This is a problem in easy-ui's export; nothing was uploaded.", code="export_unsafe")


def collect_files(folder: Path) -> list[ExportFile]:
    """Every file of the export, checked: regular files only, safe paths, within the limits."""
    out: list[ExportFile] = []
    total = 0
    base = folder.resolve()
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        for name in dirnames:
            if (d / name).is_symlink():
                raise _unsafe("contains a link to another place")
        for name in filenames:
            p = d / name
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                raise _unsafe("contains something that is not a plain file")
            rel = p.relative_to(base).as_posix()
            if any(ord(c) < 32 or ord(c) == 127 for c in rel) or "\\" in rel or len(rel) > 400:
                raise _unsafe("contains a file with an unusual name")
            if st.st_size > MAX_FILE_BYTES:
                raise Refused(f"The export has a file larger than {MAX_FILE_BYTES // (1024 * 1024)} MB ({rel}), "
                              "which hosting providers do not accept, so nothing was shared.",
                              hint="Share fewer dashboards, or dashboards that use less data.", code="export_too_big")
            total += st.st_size
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
            out.append(ExportFile(rel, p, st.st_size, h.hexdigest()))
            if len(out) > MAX_FILES or total > MAX_TOTAL_BYTES:
                raise Refused("The export is too large to share, so nothing was shared.",
                              hint="Share fewer dashboards.", code="export_too_big")
    if not out:
        raise MesError("easy-ui made an empty export, so there is nothing to share.",
                       hint="Check the dashboards with: easy-ui validate", code="export_empty")
    return sorted(out, key=lambda f: f.path)


FRAGMENT_RE = re.compile(r"^#k=([A-Za-z0-9_-]{22,128})\Z")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")


def _short(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = "".join(c for c in text if c.isprintable())
    return text[:limit]


def _visible(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """easy-ui's ``visible``: per dashboard its title, control values and, per query, the columns
    and the number of rows the copy holds. This is what the person is asked about."""
    raw = answer.get("visible")
    bad = MesError("easy-ui did not say what the export shows, so mes cannot ask you about it and shared nothing.",
                   hint="Update my-easy-ui.", code="export_bad_answer")
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        raise bad
    out = []
    for d in raw:
        if not isinstance(d, dict) or not isinstance(d.get("slug"), str) or not _SLUG_RE.match(d["slug"]):
            raise bad
        values = d.get("values") or {}
        queries = d.get("queries")
        if not isinstance(values, dict) or not isinstance(queries, list) or len(queries) > 200:
            raise bad
        qs = []
        for q in queries:
            if not isinstance(q, dict) or not isinstance(q.get("name"), str) or not isinstance(q.get("columns"), list) \
                    or not all(isinstance(c, str) and 0 < len(c) <= 128 for c in q["columns"]) \
                    or not isinstance(q.get("rows"), int) or isinstance(q.get("rows"), bool) or q["rows"] < 0:
                raise bad
            qs.append({"name": _short(q["name"], 64), "columns": [_short(c, 128) for c in q["columns"]],
                       "rows": q["rows"], "truncated": bool(q.get("truncated"))})
        out.append({"slug": d["slug"], "title": _short(d.get("title") or d["slug"], 120),
                    "values": {_short(k, 64): _short(v) for k, v in sorted(values.items())}, "queries": qs})
    return out


def _link_key(answer: dict[str, Any]) -> str:
    fragment = answer.get("fragment")
    m = FRAGMENT_RE.match(fragment) if isinstance(fragment, str) else None
    if answer.get("encrypted") is not True or not m:
        raise MesError("easy-ui did not give the key of the encrypted copy, so link mode cannot work and nothing "
                       "was shared.", hint="Update my-easy-ui.", code="export_bad_answer")
    return m.group(1)


def _key_in_files(key: str, files: list[ExportFile]) -> bool:
    needles = [key.encode("ascii")]
    try:
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        if len(raw) >= 16:
            std = base64.b64encode(raw)
            needles += [raw, std, std.rstrip(b"="), raw.hex().encode("ascii")]
    except ValueError:
        pass
    return any(any(n in f.read() for n in needles) for f in files)


def fingerprint(summary: dict[str, Any], files: list[ExportFile], *, encrypted: bool,
                content_sha256: str | None = None) -> str:
    """SHA-256 of everything the person is asked about, and of the exported files.

    In link mode the files are encrypted with a new key on every export, so their bytes change
    each time; the fingerprint then covers the file names and what easy-ui says is visible
    (dashboards, control values, columns and row counts, which are in ``summary``)."""
    body = dict(summary)
    if encrypted:
        body["files"] = [f.path for f in files]
        body["content_sha256"] = content_sha256
    else:
        body["files"] = [[f.path, f.sha256] for f in files]
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _question(summary: dict[str, Any], project: manifest_mod.Project, adapter: Adapter) -> str:
    parts = []
    for d in summary["visible"]:
        values = ", ".join(f"{k}={v}" for k, v in d["values"].items()) or "no controls"
        rows = "; ".join(f"{q['rows']} row(s) of {', '.join(q['columns']) or 'nothing'}"
                         + (" (cut short)" if q["truncated"] else "") for q in d["queries"]) or "no data"
        parts.append(f"{d['title']} ({d['slug']}, for {values}): {rows}")
    tables = " | ".join(parts)
    who = WHO[summary["mode"]]
    if summary["mode"] == "people":
        who += ": " + ", ".join(summary["people"])
    return (
        f'Share these dashboards of "{project.title}" on {adapter.title}, at {adapter.site_url(summary["site"])}? '
        f"Dashboards: {', '.join(summary['dashboards'])}. "
        f"Data that will leave this computer (a copy of only what those dashboards show): {tables}. "
        f"Who will be able to open it: {who}."
    )


def _rerun(args: dict[str, Any]) -> str:
    words = ["mes", "expose", args["project"], "--to", args["provider"], "--mode", args["mode"]]
    for d in args["dashboards"]:
        words += ["--dashboard", d]
    for p in args["people"]:
        words += ["--people", p]
    if args.get("site"):
        words += ["--site", args["site"]]
    if args.get("project_dir"):
        words += ["--project-dir", args["project_dir"]]
    if args.get("working"):
        words.append("--working")
    return " ".join(shlex.quote(w) for w in words) + " --answer confirm=<value>"


# --------------------------------------------------------------------------- the command


def expose(project_name: str, *, provider: str, mode: str, dashboards: list[str] | None = None,
           people: list[str] | None = None, site: str | None = None, project_dir: str | None = None,
           yes: bool = False, answer: str | None = None, dry_run: bool = False,
           working: bool = False) -> dict[str, Any]:
    if provider not in ADAPTERS:
        raise UsageError(f"{provider[:40]!r} is not a hosting provider mes knows.",
                         hint="Use one of: " + ", ".join(ADAPTERS))
    adapter = ADAPTERS[provider]
    if not adapter.available:
        raise adapter.refuse()  # type: ignore[attr-defined]
    if mode not in MODES:
        raise UsageError(f"{mode[:40]!r} is not a sharing mode.", hint="Use people, link or public.")
    if mode not in adapter.modes:
        raise Refused(
            f"{adapter.title} cannot limit a site to people you name, so people mode is not possible there and "
            "nothing was shared.",
            hint="Use --mode link (only people you give the link to can open it), or --to cloudflare-pages for "
            "people mode.", code="mode_not_supported")
    project = _project(project_name, project_dir)
    people_list = _people(mode, list(people or []))
    names = _dashboards(project, list(dashboards or []))
    site_name = _site(site, project, adapter)
    encrypted = mode == "link"

    tmp = Path(tempfile.mkdtemp(prefix="mes-expose-"))
    try:
        os.chmod(tmp, 0o700)
        out_dir = tmp / "site"
        argv = siblings.easy_ui_argv(project.root or Path.cwd(), "export", "--out", str(out_dir))
        for n in names:
            argv += ["--dashboard", n]
        if encrypted:
            argv.append("--encrypt")
        if working:
            argv.append("--working")
        result = siblings.run_json(argv, cwd=project.root, timeout=EXPORT_TIMEOUT,
                                   what="Making the static copy of the dashboards (easy-ui export)")
        if out_dir.is_symlink() or not out_dir.is_dir():
            raise MesError("easy-ui did not write the static copy where mes asked, so nothing was shared.",
                           hint="Update my-easy-ui.", code="export_bad_answer")
        files = collect_files(out_dir)
        shown = _visible(result)
        if encrypted:
            key = _link_key(result)
            if _key_in_files(key, files):
                raise Refused("The encrypted copy contains its own key, so anyone could read it. Nothing was shared.",
                              hint="This is a problem in easy-ui's export; report it. Nothing was uploaded.",
                              code="key_in_export")
        else:
            if result.get("encrypted") is True or result.get("fragment"):
                raise MesError("easy-ui made an encrypted copy although none was asked for, so nothing was shared.",
                               hint="Update my-easy-ui.", code="export_bad_answer")
            key = None
        summary = {"project": project.name, "provider": provider, "site": site_name, "mode": mode,
                   "people": people_list, "dashboards": names, "visible": shown}
        content = result.get("content_sha256") if isinstance(result.get("content_sha256"), str) else None
        digest = fingerprint(summary, files, encrypted=encrypted, content_sha256=content)
        visible = {"dashboards": names, "data": shown, "who": WHO[mode], "people": people_list,
                   "where": adapter.site_url(site_name)}

        if dry_run:
            return {
                "dry_run": True, "project": project.name, "provider": provider, "site": site_name, "mode": mode,
                "would_become_visible": visible,
                "files": [{"path": f.path, "bytes": f.size} for f in files],
                "steps": adapter.plan(site_name, files, mode, people_list),
                "message": "Nothing was uploaded: these are the steps mes would take. No token was needed or read.",
            }
        if answer == "no":
            return {"shared": False, "message": "Nothing was shared, as you chose."}
        if not yes and answer != digest:
            question = _question(summary, project, adapter)
            if answer is not None:
                question = "What would be shared has changed since that answer, so mes asks again. " + question
            return protocol.needs_input(
                id="expose-confirm",
                question=question,
                why=("Sharing sends a copy of this data outside your own devices. Only you can decide that, and a yes "
                     "covers exactly what is listed here."),
                options=[
                    protocol.option(digest, "Yes, share exactly this",
                                    f"The files are uploaded to {adapter.title} and {WHO[mode]} can open them."),
                    protocol.option("no", "No, do not share", "Nothing is uploaded."),
                ],
                recommended="no",
                rerun=_rerun({"project": project.name, "provider": provider, "mode": mode, "dashboards": names,
                              "people": people_list, "site": site, "project_dir": project_dir,
                              "working": working}),
            )

        token = load_token(adapter)
        http = Http(api_base(adapter), provider=adapter.title,
                    secrets_to_hide=(token,) + ((key,) if key else ()))
        done = adapter.upload(site_name, out_dir, files, mode, people_list, token, http)
        url = str(done.get("url") or adapter.site_url(site_name))
        link = f"{url}#k={key}" if encrypted else url
        out = {
            "shared": True, "project": project.name, "provider": provider, "site": site_name, "mode": mode,
            "url": link, "visible": visible, "files": len(files), "requests": http.log,
        }
        if mode == "link":
            out["message"] = ("Shared. Anyone holding this link can open the dashboards; the key is the part after "
                              "#, which is never sent to the provider. To stop the link from working, run mes "
                              "expose again (a new key is made) and share only the new link.")
        elif mode == "people":
            out["message"] = ("Shared. Only the listed people can open it, after signing in with their email. To "
                              "change who can open it, run mes expose again with the new list.")
        else:
            out["message"] = "Shared. Anyone on the internet can open it."
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _parse_answer(raw: list[str]) -> str | None:
    value = None
    for entry in raw or []:
        k, sep, v = entry.partition("=")
        if not sep or k.strip() != "confirm" or not v.strip():
            raise UsageError(f'The answer "{entry[:80]}" is not understood.',
                             hint="Answer with --answer confirm=<value> from the question.")
        value = v.strip()
    return value


def _handle(args: argparse.Namespace) -> Any:
    check_allowed("admin")  # sending data outside your own devices is the most powerful thing mes does
    return expose(args.project, provider=args.to, mode=args.mode, dashboards=args.dashboard, people=args.people,
                  site=args.site, project_dir=args.project_dir, yes=args.yes, answer=_parse_answer(args.answer),
                  working=args.working,
                  dry_run=args.dry_run)


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "expose",
        help="Share chosen dashboards with other people as a static copy on a hosting provider.",
        description="Build a static copy of chosen dashboards (easy-ui export) and publish it with your own "
        "hosting account. Nothing is uploaded until you confirm exactly what becomes visible and to whom. "
        "Tokens come from the environment or an owner-only file, never the command line. See: mes guide share",
    )
    p.add_argument("project", help="the project's name")
    p.add_argument("--to", required=True, choices=list(ADAPTERS), help="the hosting provider")
    p.add_argument("--mode", required=True, choices=list(MODES),
                   help="people: only named people; link: anyone with the link (encrypted); public: anyone")
    p.add_argument("--dashboard", action="append", default=[], metavar="NAME",
                   help="a dashboard to share (repeat; default: those listed in [ui] dashboards)")
    p.add_argument("--people", action="append", default=[], metavar="EMAIL",
                   help="with --mode people: an email address that may open it (repeat)")
    p.add_argument("--site", default=None, help="the site's name at the provider (default: the project's name)")
    p.add_argument("--project-dir", default=None, metavar="DIR", help="the project's folder (default: here)")
    p.add_argument("--working", action="store_true",
                   help="share the dashboard files as they are now in the project folder (default: the published ones)")
    p.add_argument("--yes", action="store_true", help="the person has already said yes to sharing exactly this")
    p.add_argument("--answer", action="append", default=[], metavar="confirm=VALUE",
                   help="the answer to the confirmation question")
    p.add_argument("--dry-run", action="store_true", help="show every step and upload nothing (no token needed)")
    p.set_defaults(func=_handle)
