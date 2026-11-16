"""Tests for ``mes expose`` (sharing): BLAKE3, the confirmation, tokens, link mode, adapters.

easy-ui is replaced by a small fake program (``MES_EASY_UI``) that writes an export, and every
hosting provider by a fake HTTP server on 127.0.0.1, given to the adapters through their
constructor (``Adapter(base=..., fetch=...)``; nothing in the environment can point mes at another
API). The fake answers like the real providers do when a name is taken: the site gets a different
address than the one asked for. Nothing here talks to a real provider.
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import os
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from mes import cli
from mes.errors import MesError, NeedsPerson, NotAvailableYet, NotFound, Refused, UsageError
from mes.expose import adapters, blake3, expose, fingerprint

TOKEN = "tok_SECRET_value_1234567890"
ACCOUNT = "0123456789abcdef0123456789abcdef"
KEY = "kEyKeYkEyKeYkEyKeYkEyKeYkEyKeYkEyKeY-_ab"

PROJECT = """
[project]
name = "notes"
title = "Notes"

[jobs.rebuild]
run = "notes rebuild"
manual = true

[ui]
data = "notes.sqlite"
tables = ["daily", "secret"]
dashboards = ["main", "other"]
"""

FAKE_EASY_UI = r'''#!{python}
import json, os, sys, secrets
args = sys.argv[1:]
assert args[0] == "--json" and args[1] == "--project-dir", args
assert args[3] == "export", args
out = args[args.index("--out") + 1]
dashboards = [args[i + 1] for i, a in enumerate(args) if a == "--dashboard"]
encrypt = "--encrypt" in args
os.makedirs(os.path.join(out, "data"))
with open(os.path.join(out, "index.html"), "w") as f:
    f.write("<html>dashboards</html>")
key = "{key}"
body = os.environ.get("FAKE_DATA", "rows")
if encrypt:
    body = "ENC:" + secrets.token_hex(8)
    if os.environ.get("FAKE_LEAK_KEY"):
        body += key
with open(os.path.join(out, "data", "main.json"), "w") as f:
    f.write(body)
rows = 3 if os.environ.get("FAKE_DATA") is None else 4
answer = {{"out": out, "files": ["index.html", "data/main.json"], "dashboards": dashboards, "bytes": 10,
          "encrypted": encrypt, "fragment": "#k=" + key if encrypt else None,
          "visible": [{{"slug": d, "title": os.environ.get("FAKE_TITLE") or d.title(),
                       "values": {{"who": os.environ["FAKE_VALUE"]}} if os.environ.get("FAKE_VALUE") else {{}},
                       "queries": [{{"name": "daily", "columns": ["day", "notes"], "rows": rows, "truncated": False}}]}}
                      for d in dashboards]}}
if os.environ.get("FAKE_NO_FRAGMENT"):
    answer["fragment"] = None
if os.environ.get("FAKE_ALWAYS_ENCRYPT"):
    answer["encrypted"] = True
if os.environ.get("FAKE_NO_VISIBLE"):
    answer.pop("visible")
print(json.dumps(answer))
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "notes"
    (root / "ui" / "dashboards").mkdir(parents=True)
    (root / "project.toml").write_text(PROJECT)
    (root / "ui" / "dashboards" / "main.yaml").write_text("spec: easy-ui/v1\n")
    (root / "ui" / "dashboards" / "other.yaml").write_text("spec: easy-ui/v1\n")
    fake = tmp_path / "bin" / "easy-ui"
    fake.parent.mkdir()
    fake.write_text(FAKE_EASY_UI.format(python=sys.executable, key=KEY))
    fake.chmod(0o755)
    monkeypatch.setenv("MES_EASY_UI", str(fake))
    for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "NETLIFY_AUTH_TOKEN", "VERCEL_TOKEN",
                 "VERCEL_TEAM_ID", "MES_EXPOSE_API_BASE", "FAKE_LEAK_KEY", "FAKE_DATA", "FAKE_TITLE", "FAKE_VALUE"):
        monkeypatch.delenv(name, raising=False)
    return root


ACCESS_LOGIN = "https://team.cloudflareaccess.com/cdn-cgi/access/login/x"


class Fake:
    """A fake provider API that records every request, and a fake "stranger" opening the site."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fetched: list[str] = []
        self.pages_exists = False
        self.subdomain: str | None = "notes-x7k.pages.dev"  # Cloudflare added a suffix: the name was taken
        self.apps: list[dict] = []
        self.policies: list[dict] = []
        self.cf_deployments = [{"id": "old-1"}]
        self.protected = True
        self.netlify_url: str | None = "https://notes-77.netlify.app"
        self.vercel_answer: dict = {"id": "dpl_new", "url": "notes-abc.vercel.app", "alias": ["notes-team.vercel.app"]}
        self.refuse_delete = False
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN002
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append({"method": self.command, "path": self.path,
                                      "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "body": body})
                status, data = fake.route(self.command, self.path, body)
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = _do

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def fetch(self, url):
        self.fetched.append(url)
        return (302, ACCESS_LOGIN) if self.protected else (200, None)

    def _project(self):
        return {"name": "notes", "subdomain": self.subdomain, "domains": [self.subdomain] if self.subdomain else []}

    def route(self, method, path, body):
        acc = f"/accounts/{ACCOUNT}"
        if method == "DELETE":
            if self.refuse_delete and "/deployments/" in path:
                return 400, {"errors": [{"message": "cannot delete"}]}
            if "/policies/" in path:
                pid = path.rsplit("/", 1)[1]
                self.policies = [p for p in self.policies if p["id"] != pid]
            return 200, {"result": {}}
        if path == f"{acc}/pages/projects/notes" and method == "GET":
            return (200, {"result": self._project()}) if self.pages_exists else (404, {"errors": [{"message": "nf"}]})
        if path == f"{acc}/pages/projects" and method == "POST":
            self.pages_exists = True
            return 200, {"result": self._project()}
        if path.endswith("/upload-token"):
            return 200, {"result": {"jwt": "JWT-abc"}}
        if path == "/pages/assets/check-missing":
            return 200, {"result": json.loads(body)["hashes"]}
        if path in ("/pages/assets/upload", "/pages/assets/upsert-hashes"):
            return 200, {"success": True}
        if path.endswith("/pages/projects/notes/deployments") and method == "GET":
            return 200, {"result": self.cf_deployments}
        if path.endswith("/pages/projects/notes/deployments") and method == "POST":
            return 200, {"result": {"id": "d1", "url": f"https://abc123.{self.subdomain}"}}
        if path == f"{acc}/access/apps" and method == "GET":
            return 200, {"result": self.apps}
        if path == f"{acc}/access/apps" and method == "POST":
            return 200, {"result": {"id": "app-1"}}
        if path.endswith("/policies") and method == "GET":
            return 200, {"result": self.policies}
        if path.endswith("/policies") and method == "POST":
            self.policies.append({"id": "pol-1"})
            return 200, {"result": {"id": "pol-1"}}
        if path.startswith(f"{acc}/access/apps/") and method == "PUT":
            return 200, {"result": {"id": path.rsplit("/", 1)[1]}}
        if path.startswith("/api/v1/sites?"):
            return 200, []
        if path == "/api/v1/sites":
            return 200, {"id": "site-1", "ssl_url": self.netlify_url}
        if path == "/api/v1/sites/site-1/deploys" and method == "GET":
            return 200, [{"id": "old-n"}]
        if path == "/api/v1/sites/site-1/deploys":
            return 200, {"id": "dep-1", "ssl_url": self.netlify_url}
        if path.startswith("/v6/deployments?"):
            return 200, {"deployments": [{"uid": "old-v"}]}
        if path == "/v2/files":
            return 200, {}
        if path == "/v13/deployments":
            return 200, self.vercel_answer
        return 500, {"errors": [{"message": "unexpected " + path}]}

    def deleted(self):
        return [q["path"] for q in self.requests if q["method"] == "DELETE"]

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    for cls in (adapters.CloudflarePages, adapters.Netlify, adapters.Vercel):
        monkeypatch.setitem(adapters.ADAPTERS, cls.name, cls(base=f.base, fetch=f.fetch, wait=lambda s: None))
    yield f
    f.stop()


def _run(project, **kw):
    kw.setdefault("provider", "cloudflare-pages")
    kw.setdefault("mode", "public")
    return expose("notes", project_dir=str(project), **kw)


def _cf_env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)


# ---------------------------------------------------------------- BLAKE3


def test_blake3_known_vectors():
    assert blake3.hexdigest(b"") == "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    assert blake3.hexdigest(b"abc") == "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"


@pytest.mark.parametrize("n, expected", [
    (1, "2d3adedff11b61f14c886e35afa036736dcd87a74d27b5c1510225d0f592e213"),
    (1024, "42214739f095a406f3fc83deb889744ac00df831c10daa55189b5d121c855af7"),
    (1025, "d00278ae47eb27b34faecf67b4fe263f82d5412916c1ffd97c8cb7fb814b8444"),
    (2048, "e776b6028c7cd22a4d0ba182a8bf62205d2ef576467e838ed6f2529b85fba24a"),
    (8192, "aae792484c8efe4f19e2ca7d371d8c467ffb10748d8a5a1ae579948f718a2a63"),
])
def test_blake3_official_vectors_across_chunks(n, expected):
    # The official test vectors use the input bytes 0, 1, ..., 250, 0, 1, ...
    assert blake3.hexdigest(bytes(i % 251 for i in range(n))) == expected


def test_blake3_tree_is_consistent_for_many_lengths():
    seen = {blake3.hexdigest(bytes(i % 251 for i in range(n))) for n in (2049, 3072, 3073, 4096, 5121)}
    assert len(seen) == 5


# ---------------------------------------------------------------- confirmation


def test_without_yes_asks_and_lists_exactly_what_is_shared(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    r = _run(project, mode="people", people=["Friend@Example.com", "other@example.org"])
    assert r["status"] == "needs_input" and r["id"] == "expose-confirm"
    q = r["question"]
    assert "Dashboards: main, other." in q
    assert '3 row(s) of "day", "notes"' in q and 'main (title "Main"' in q and "secret" not in q
    assert "friend@example.com, other@example.org" in q
    assert "notes.pages.dev" in q and q.startswith("Provider: Cloudflare.")
    assert r["recommended"] == "no" and "--answer confirm=<value>" in r["rerun"]
    assert fake.requests == []  # nothing was sent anywhere


@pytest.mark.parametrize("mode, who", [("link", "anyone holding the link"), ("public", "anyone on the internet")])
def test_question_says_who_can_open_it(project, fake, mode, who):
    assert who in _run(project, mode=mode)["question"]


def test_matching_answer_uploads_and_changed_export_asks_again(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    first = _run(project)
    digest = first["options"][0]["value"]
    monkeypatch.setenv("FAKE_DATA", "rows changed since")
    again = _run(project, answer=digest)
    assert again["status"] == "needs_input" and "has changed since that answer" in again["question"]
    assert fake.requests == []
    monkeypatch.delenv("FAKE_DATA")
    done = _run(project, answer=digest)
    assert done["shared"] is True and done["url"] == "https://notes-x7k.pages.dev"


def test_answer_for_other_people_does_not_upload(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    digest = _run(project, mode="people", people=["a@example.com"])["options"][0]["value"]
    r = _run(project, mode="people", people=["a@example.com", "b@example.com"], answer=digest)
    assert r["status"] == "needs_input" and fake.requests == []


def test_answer_no_shares_nothing(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    assert _run(project, answer="no")["shared"] is False and fake.requests == []


def test_yes_uploads(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    r = _run(project, yes=True)
    assert r["shared"] is True and r["mode"] == "public"
    paths = [q["path"] for q in fake.requests]
    assert f"/accounts/{ACCOUNT}/pages/projects" in paths  # created because it did not exist
    assert any(q["method"] == "POST" and q["path"].endswith("/deployments") for q in fake.requests)


def test_dry_run_uploads_nothing_and_needs_no_token(project, fake):
    r = _run(project, mode="people", people=["a@example.com"], dry_run=True)
    assert r["dry_run"] is True and fake.requests == []
    steps = [s["path"] for s in r["steps"]]
    assert steps[0].endswith("/pages/projects/notes") and any(s.endswith("/access/apps") for s in steps)
    assert steps.index(next(s for s in steps if s.endswith("/access/apps"))) < steps.index("/pages/assets/upload")
    assert fake.fetched == []
    assert {f["path"] for f in r["files"]} == {"index.html", "data/main.json"}
    assert r["would_become_visible"]["data"][0]["queries"][0]["columns"] == ["day", "notes"]


def test_fingerprint_covers_people_mode_and_files(project):
    f = adapters.ExportFile("a", project / "project.toml", 1, "x")
    base = {"project": "notes", "mode": "public", "people": []}
    assert fingerprint(base, [f], encrypted=False) != fingerprint({**base, "mode": "link"}, [f], encrypted=False)
    g = adapters.ExportFile("a", project / "project.toml", 1, "y")
    assert fingerprint(base, [f], encrypted=False) != fingerprint(base, [g], encrypted=False)
    assert fingerprint(base, [f], encrypted=True) == fingerprint(base, [g], encrypted=True)


# ---------------------------------------------------------------- tokens


def test_token_from_owner_only_file(project, fake, monkeypatch, mes_home):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    tok = mes_home["config"].parent / "tokens" / "cloudflare-pages"
    tok.parent.mkdir(parents=True)
    tok.write_text(TOKEN + "\n")
    tok.chmod(0o600)
    r = _run(project, yes=True)
    assert r["shared"] is True
    assert fake.requests[0]["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_group_readable_token_file_is_refused(project, fake, monkeypatch, mes_home):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    tok = mes_home["config"].parent / "tokens" / "cloudflare-pages"
    tok.parent.mkdir(parents=True)
    tok.write_text(TOKEN)
    tok.chmod(0o640)
    with pytest.raises(Refused) as e:
        _run(project, yes=True)
    assert "Other accounts" in e.value.message and TOKEN not in e.value.message + e.value.hint
    assert fake.requests == []


def test_symlinked_token_file_is_refused(project, fake, monkeypatch, mes_home, tmp_path):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    real = tmp_path / "real-token"
    real.write_text(TOKEN)
    real.chmod(0o600)
    tok = mes_home["config"].parent / "tokens" / "cloudflare-pages"
    tok.parent.mkdir(parents=True)
    tok.symlink_to(real)
    with pytest.raises(Refused) as e:
        _run(project, yes=True)
    assert "not a plain file" in e.value.message


def test_missing_token_needs_the_person(project, fake, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    with pytest.raises(NeedsPerson) as e:
        _run(project, yes=True)
    assert e.value.payload["status"] == "needs_human" and "CLOUDFLARE_API_TOKEN" in e.value.payload["action"]
    assert fake.requests == []


def test_tokens_never_appear_in_output_or_errors(project, fake, monkeypatch, capsys):
    _cf_env(monkeypatch)
    code = cli.run(["--json", "expose", "notes", "--project-dir", str(project), "--to", "cloudflare-pages",
                    "--mode", "public", "--yes"])
    out = capsys.readouterr()
    assert code == 0 and TOKEN not in out.out + out.err
    assert json.loads(out.out)["shared"] is True
    # A provider error that echoes the token is cleaned too.
    fake.route = lambda m, p, b: (400, {"errors": [{"message": f"bad token {TOKEN} and JWT-abc"}]})
    code = cli.run(["--json", "expose", "notes", "--project-dir", str(project), "--to", "cloudflare-pages",
                    "--mode", "public", "--yes"])
    out = capsys.readouterr()
    assert code == 1 and TOKEN not in out.out + out.err and "[hidden]" in out.out


def test_tokens_are_not_accepted_as_arguments(project, capsys):
    code = cli.run(["--json", "expose", "notes", "--to", "netlify", "--mode", "public", "--token", TOKEN])
    assert code == 2


def test_expose_is_admin(project, monkeypatch, capsys):
    monkeypatch.setenv("MES_ALLOW", "write")
    code = cli.run(["--json", "expose", "notes", "--project-dir", str(project), "--to", "netlify",
                    "--mode", "public", "--yes"])
    assert code == 4


# ---------------------------------------------------------------- link mode


def test_link_mode_key_only_in_the_fragment(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    r = _run(project, mode="link", yes=True)
    assert r["url"] == f"https://notes-x7k.pages.dev#k={KEY}"
    raw_key = base64.urlsafe_b64decode(KEY + "=" * (-len(KEY) % 4))
    for q in fake.requests:
        blob = q["path"].encode() + q["body"] + json.dumps(q["headers"]).encode()
        assert KEY.encode() not in blob and raw_key not in blob
        if q["path"] == "/pages/assets/upload":
            for item in json.loads(q["body"]):
                assert KEY.encode() not in base64.b64decode(item["value"])


def test_link_mode_refuses_an_export_that_contains_its_key(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    monkeypatch.setenv("FAKE_LEAK_KEY", "1")
    with pytest.raises(Refused) as e:
        _run(project, mode="link", yes=True)
    assert e.value.code == "key_in_export" and fake.requests == []


def test_link_mode_confirmation_survives_a_new_key(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    digest = _run(project, mode="link")["options"][0]["value"]
    assert _run(project, mode="link", answer=digest)["shared"] is True


# ---------------------------------------------------------------- people mode


def test_people_mode_protects_before_uploading(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    r = _run(project, mode="people", people=["a@example.com", "b@example.com"], yes=True)
    assert r["shared"] is True
    paths = [q["path"] for q in fake.requests]
    app_post = next(i for i, q in enumerate(fake.requests)
                    if q["method"] == "POST" and q["path"] == f"/accounts/{ACCOUNT}/access/apps")
    policy = next(q for q in fake.requests if q["path"].endswith("/policies") and q["method"] == "POST")
    assert app_post < paths.index("/pages/assets/upload")
    app_body = json.loads(fake.requests[app_post]["body"])
    assert app_body["self_hosted_domains"] == ["notes-x7k.pages.dev", "*.notes-x7k.pages.dev"]
    emails = [i["email"]["email"] for i in json.loads(policy["body"])["include"]]
    assert emails == ["a@example.com", "b@example.com"]


def test_people_mode_updates_an_existing_access_app(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    fake.apps = [{"id": "app-9", "domain": "notes-x7k.pages.dev"}]
    _run(project, mode="people", people=["a@example.com"], yes=True)
    assert any(q["method"] == "PUT" and q["path"] == f"/accounts/{ACCOUNT}/access/apps/app-9" for q in fake.requests)


def test_people_mode_is_refused_on_netlify(project, fake, monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", TOKEN)
    with pytest.raises(Refused) as e:
        _run(project, provider="netlify", mode="people", people=["a@example.com"], yes=True)
    assert "--mode link" in e.value.hint and fake.requests == []


@pytest.mark.parametrize("people", [[], ["not an email"], ["a@b"]])
def test_people_mode_needs_plain_emails(project, people):
    with pytest.raises(UsageError):
        _run(project, mode="people", people=people)


# ---------------------------------------------------------------- adapters and safety


def test_cloudflare_manifest_uses_the_blake3_scheme(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    _run(project, yes=True)
    dep = next(q for q in fake.requests if q["path"].endswith("/deployments") and q["method"] == "POST")
    text = dep["body"].decode()
    manifest = json.loads(text.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])
    content = b"<html>dashboards</html>"
    expected = blake3.hexdigest(base64.b64encode(content) + b"html")[:32]
    assert manifest["/index.html"] == expected
    upload = next(q for q in fake.requests if q["path"] == "/pages/assets/upload")
    assert upload["headers"]["authorization"] == "Bearer JWT-abc"
    assert {i["key"] for i in json.loads(upload["body"])} == set(manifest.values())


def test_netlify_uploads_one_zip(project, fake, monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", TOKEN)
    r = _run(project, provider="netlify", yes=True)
    assert r["url"] == "https://notes-77.netlify.app"
    dep = next(q for q in fake.requests if q["path"].endswith("/deploys") and q["method"] == "POST")
    assert dep["headers"]["content-type"] == "application/zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(dep["body"])).namelist()) == ["data/main.json", "index.html"]


def test_vercel_uploads_files_then_deploys(project, fake, monkeypatch):
    monkeypatch.setenv("VERCEL_TOKEN", TOKEN)
    r = _run(project, provider="vercel", mode="link", yes=True)
    assert r["url"].startswith("https://notes-team.vercel.app#k=")
    uploads = [q for q in fake.requests if q["path"] == "/v2/files"]
    assert len(uploads) == 2 and all(len(q["headers"]["x-vercel-digest"]) == 40 for q in uploads)
    dep = json.loads(next(q for q in fake.requests if q["path"] == "/v13/deployments")["body"])
    assert dep["target"] == "production" and {f["file"] for f in dep["files"]} == {"index.html", "data/main.json"}


@pytest.mark.parametrize("provider", ["github-pages", "render"])
def test_not_available_yet_providers(project, provider):
    with pytest.raises(NotAvailableYet) as e:
        _run(project, provider=provider, yes=True)
    assert "cloudflare-pages" in e.value.hint


@pytest.mark.parametrize("base", ["http://example.com", "ftp://127.0.0.1", "http://10.0.0.1:8080"])
def test_api_base_must_be_https_or_local(base):
    with pytest.raises(Refused) as e:
        adapters.Netlify(base=base)
    assert e.value.code == "unsafe_api_base"


def test_the_environment_cannot_send_the_token_elsewhere(project, monkeypatch):
    """Audit F2: MES_EXPOSE_API_BASE sent the real token to any https host it named."""
    monkeypatch.setenv("MES_EXPOSE_API_BASE", "https://collector.example.net")
    for cls in (adapters.CloudflarePages, adapters.Netlify, adapters.Vercel):
        assert cls().base == cls.default_base
    assert not hasattr(adapters, "api_base")
    import mes.expose.adapters as mod

    assert "MES_EXPOSE_API_BASE" not in Path(mod.__file__).read_text().replace("MES_EXPOSE_API_BASE`` replaces", "")


def test_redirects_are_never_followed(project, monkeypatch):
    class Redirect(http.server.BaseHTTPRequestHandler):
        hits = []

        def log_message(self, *a):  # noqa: ANN002
            pass

        def do_GET(self):  # noqa: N802
            Redirect.hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setitem(adapters.ADAPTERS, "netlify", adapters.Netlify(base=f"http://127.0.0.1:{srv.server_address[1]}"))
        monkeypatch.setenv("NETLIFY_AUTH_TOKEN", TOKEN)
        with pytest.raises(MesError) as e:
            _run(project, provider="netlify", yes=True)
        assert "/elsewhere" not in Redirect.hits and e.value.code in ("provider_redirect", "provider_refused")
    finally:
        srv.shutdown()


def test_easy_ui_missing_gives_a_plain_message(project, monkeypatch):
    monkeypatch.setenv("MES_EASY_UI", str(project / "no-such-program"))
    monkeypatch.setenv("PATH", str(project))
    with pytest.raises(NotFound) as e:
        _run(project, yes=True)
    assert "easy-ui" in e.value.message and "my-easy-ui" in e.value.hint


def test_project_without_ui_is_refused(project):
    (project / "project.toml").write_text('[project]\nname = "notes"\n')
    with pytest.raises(Refused) as e:
        _run(project)
    assert e.value.code == "no_dashboards"


def test_unknown_dashboard_and_wrong_project(project):
    with pytest.raises(NotFound):
        _run(project, dashboards=["nope"])
    with pytest.raises(UsageError):
        expose("other", provider="netlify", mode="public", project_dir=str(project))


def test_export_with_a_symlink_is_refused(project, tmp_path):
    out = tmp_path / "exp"
    out.mkdir()
    (out / "a.html").write_text("x")
    (out / "link").symlink_to("/etc/passwd")
    from mes.expose import collect_files

    with pytest.raises(Refused):
        collect_files(out)


def test_cli_needs_input_exit_code(project, fake, capsys):
    code = cli.run(["--json", "expose", "notes", "--project-dir", str(project), "--to", "cloudflare-pages",
                    "--mode", "public"])
    out = json.loads(capsys.readouterr().out)
    assert code == 6 and out["status"] == "needs_input"



@pytest.mark.parametrize("env, mode", [("FAKE_NO_FRAGMENT", "link"), ("FAKE_ALWAYS_ENCRYPT", "public"),
                                       ("FAKE_NO_VISIBLE", "public")])
def test_export_answers_that_do_not_add_up_share_nothing(project, fake, monkeypatch, env, mode):
    _cf_env(monkeypatch)
    monkeypatch.setenv(env, "1")
    with pytest.raises(MesError) as e:
        _run(project, mode=mode, yes=True)
    assert e.value.code == "export_bad_answer" and fake.requests == []


def test_link_mode_asks_again_when_the_data_changes(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    digest = _run(project, mode="link")["options"][0]["value"]
    monkeypatch.setenv("FAKE_DATA", "more rows")
    again = _run(project, mode="link", answer=digest)
    assert again["status"] == "needs_input" and fake.requests == []


# ---------------------------------------------------------------- audit: real addresses (F1)


def test_people_mode_protects_the_address_cloudflare_really_gave(project, fake, monkeypatch):
    """Audit F1: when pages.dev added a suffix (notes-x7k.pages.dev), the sign-in rule was made for
    notes.pages.dev, so the real site stayed public while mes said it was restricted."""
    _cf_env(monkeypatch)
    r = _run(project, mode="people", people=["a@example.com"], yes=True)
    app = json.loads(next(q for q in fake.requests if q["method"] == "POST"
                          and q["path"] == f"/accounts/{ACCOUNT}/access/apps")["body"])
    assert app["domain"] == "notes-x7k.pages.dev"
    assert app["self_hosted_domains"] == ["notes-x7k.pages.dev", "*.notes-x7k.pages.dev"]
    assert all("//notes.pages.dev" not in json.dumps(q["body"].decode(errors="replace")) for q in fake.requests)
    assert r["url"] == "https://notes-x7k.pages.dev"
    # mes opened the real addresses like a stranger and saw the sign-in page before saying so.
    assert set(fake.fetched) == {"https://notes-x7k.pages.dev/", "https://abc123.notes-x7k.pages.dev/"}
    assert "asked for a sign-in" in r["message"]


def test_link_mode_link_uses_the_address_cloudflare_gave(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    r = _run(project, mode="link", yes=True)
    assert r["url"].startswith("https://notes-x7k.pages.dev#k=")


def test_no_address_in_the_answer_means_nothing_is_uploaded(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    fake.subdomain = None
    with pytest.raises(MesError) as e:
        _run(project, mode="people", people=["a@example.com"], yes=True)
    assert e.value.code == "provider_no_address"
    assert not any(q["path"].startswith("/pages/assets") for q in fake.requests)
    assert not any(q["path"].endswith("/access/apps") and q["method"] == "POST" for q in fake.requests)


def test_a_site_that_does_not_ask_for_a_sign_in_is_deleted_again(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    fake.protected = False
    with pytest.raises(Refused) as e:
        _run(project, mode="people", people=["a@example.com"], yes=True)
    assert e.value.code == "access_not_working" and "deleted the new version" in e.value.message
    assert f"/accounts/{ACCOUNT}/pages/projects/notes/deployments/d1?force=true" in fake.deleted()
    assert not any(p.endswith("/old-1?force=true") for p in fake.deleted())  # the earlier one is left alone
    assert len(fake.fetched) == adapters.CHECK_TRIES * 2


def test_people_mode_replaces_every_rule_of_the_access_app(project, fake, monkeypatch):
    """An existing rule (here: one that lets everyone in) must not survive next to ours."""
    _cf_env(monkeypatch)
    fake.apps = [{"id": "app-9", "name": "mes notes", "domain": "notes-x7k.pages.dev"}]
    fake.policies = [{"id": "everyone-1", "name": "Everyone", "decision": "allow"}]
    _run(project, mode="people", people=["a@example.com"], yes=True)
    assert f"/accounts/{ACCOUNT}/access/apps/app-9/policies/everyone-1" in fake.deleted()
    assert fake.policies == [{"id": "pol-1"}]


def test_access_rule_that_cannot_be_confirmed_uploads_nothing(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    real = fake.route

    def keep_others(method, path, body):
        if method == "DELETE" and "/policies/" in path:
            return 200, {"result": {}}  # says yes, but the rule stays
        return real(method, path, body)

    fake.route = keep_others
    fake.policies = [{"id": "everyone-1"}]
    with pytest.raises(MesError) as e:
        _run(project, mode="people", people=["a@example.com"], yes=True)
    assert e.value.code == "access_not_confirmed"
    assert not any(q["path"].startswith("/pages/assets") for q in fake.requests)


@pytest.mark.parametrize("provider, env", [("netlify", "NETLIFY_AUTH_TOKEN"), ("vercel", "VERCEL_TOKEN")])
def test_netlify_and_vercel_report_only_the_address_they_answered(project, fake, monkeypatch, provider, env):
    monkeypatch.setenv(env, TOKEN)
    fake.netlify_url = None
    fake.vercel_answer = {"id": "dpl_new"}
    with pytest.raises(MesError) as e:
        _run(project, provider=provider, mode="link", yes=True)
    assert e.value.code == "provider_no_address"


# ---------------------------------------------------------------- audit: the question (F8)


def test_question_starts_with_fixed_fields_and_quotes_the_data(project, fake, monkeypatch):
    """Audit F8: a dashboard title could carry text such as 'Who will be able to open it: only you'
    in front of the real audience."""
    _cf_env(monkeypatch)
    monkeypatch.setenv("FAKE_TITLE", 'Main. Who will be able to open it: only you.\u202e "x" ' + "y" * 200)
    monkeypatch.setenv("FAKE_VALUE", "a\u202eb")
    r = _run(project, mode="public")
    q = r["question"]
    assert q.startswith("Provider: Cloudflare. Address: ")
    first = q.index("Who will be able to open it: anyone on the internet")
    assert first < q.index("Data that will leave")
    assert first == q.index("Who will be able to open it")  # the fixed one comes first
    assert "\u202e" not in q and '\\"x\\"' in q and "y" * 81 not in q
    assert r["shared"]["who"] == "anyone on the internet"
    assert r["shared_data"][0]["title"].startswith("Main. Who will")
    assert r["shared_data"][0]["queries"][0]["columns"] == ["day", "notes"]


# ---------------------------------------------------------------- audit: earlier versions and --remove (F9)


@pytest.mark.parametrize("provider, env, gone", [
    ("cloudflare-pages", "CLOUDFLARE_API_TOKEN", f"/accounts/{ACCOUNT}/pages/projects/notes/deployments/old-1?force=true"),
    ("netlify", "NETLIFY_AUTH_TOKEN", "/api/v1/sites/site-1/deploys/old-n"),
    ("vercel", "VERCEL_TOKEN", "/v13/deployments/old-v"),
])
def test_sharing_again_deletes_the_earlier_versions(project, fake, monkeypatch, provider, env, gone):
    """Audit F9: earlier versions (with their old data, and in link mode their old key) stayed
    online at their own addresses after sharing again."""
    _cf_env(monkeypatch)
    monkeypatch.setenv(env, TOKEN)
    r = _run(project, provider=provider, mode="link", yes=True)
    assert gone in fake.deleted() and r["removed_earlier_versions"] == 1 and r["earlier_versions_kept"] == []
    assert "old links stop working" in r["message"]


def test_an_earlier_version_that_could_not_be_deleted_is_reported(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    fake.refuse_delete = True
    r = _run(project, mode="link", yes=True)
    assert r["shared"] is True and len(r["earlier_versions_kept"]) == 1
    assert "still online" in r["message"] and "--remove" in r["message"]


def test_remove_asks_then_takes_the_site_down(project, fake, monkeypatch):
    _cf_env(monkeypatch)
    fake.pages_exists = True
    fake.apps = [{"id": "app-9", "name": "mes notes", "domain": "notes-x7k.pages.dev"}]
    from mes.expose import remove

    asked = remove("notes", provider="cloudflare-pages", project_dir=str(project))
    assert asked["status"] == "needs_input" and fake.requests == []
    dry = remove("notes", provider="cloudflare-pages", project_dir=str(project), dry_run=True)
    assert dry["dry_run"] and fake.requests == []
    done = remove("notes", provider="cloudflare-pages", project_dir=str(project), answer="remove")
    assert done["removed"] is True
    deleted = fake.deleted()
    assert f"/accounts/{ACCOUNT}/pages/projects/notes" in deleted
    assert deleted[-1] == f"/accounts/{ACCOUNT}/access/apps/app-9"  # the sign-in rule goes last


def test_remove_on_the_command_line(project, fake, monkeypatch, capsys):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", TOKEN)
    fake.route = lambda m, p, b: (200, [{"id": "site-1", "name": "notes"}]) if p.startswith("/api/v1/sites?") \
        else (200, {})
    code = cli.run(["--json", "expose", "notes", "--project-dir", str(project), "--to", "netlify", "--remove",
                    "--yes"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["removed"] is True and "/api/v1/sites/site-1" in fake.deleted()
    code = cli.run(["--json", "expose", "notes", "--project-dir", str(project), "--to", "netlify", "--remove",
                    "--mode", "public"])
    assert code == 2
    capsys.readouterr()
    code = cli.run(["--json", "expose", "notes", "--project-dir", str(project), "--to", "netlify"])
    assert code == 2
