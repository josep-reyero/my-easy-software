"""Hosting adapters for ``mes expose``: one small class per provider.

Each adapter talks to the provider's own HTTP API with the person's own token (standard library
only). It never receives the link-mode key and never sees anything but the exported files.

Interface
---------
``Adapter.name``       the provider name used with ``--to``.
``Adapter.token_env``  the environment variable holding the token.
``Adapter.modes``      the sharing modes it supports (``people``, ``link``, ``public``).
``Adapter.default_base``  the API address. ``Adapter(base=..., fetch=..., wait=...)`` replaces it
                       and the site check (tests only; nothing in the environment can change it).
``Adapter.plan(site, files, mode, people) -> list[dict]``  every step, for ``--dry-run``.
``Adapter.upload(site, files_dir, files, mode, people, token, http) -> dict``  with ``url``: always
                       an address the provider answered with, never one built from the name.
                       Earlier deployments of the site are deleted after the new one is live.
``Adapter.remove_plan(site)`` / ``Adapter.remove(site, token, http) -> dict``  take the site down
                       (every deployment) at the provider.

``files`` is a list of ``ExportFile(path, abs_path, size, sha256)``.

Also here: ``Http`` (a tiny JSON/bytes client that never follows redirects, has timeouts and a
response size limit, and keeps the token out of every message), ``check_base(url)``,
``fetch_site(url) -> (status, location)`` (no credentials, no redirects), ``access_answer(status,
location) -> bool``, ``load_token(adapter)``, ``ADAPTERS``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mes import paths, protocol
from mes.errors import MesError, NeedsPerson, NotAvailableYet, Refused, TemporaryFailure
from mes.expose.blake3 import hexdigest as blake3_hex

MAX_RESPONSE = 16 * 1024 * 1024
TIMEOUT = 60.0
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
TOKEN_FILE_MAX = 4096
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,4096}\Z")
_HOST_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
CHECK_TRIES = 10
CHECK_WAIT_SEC = 6.0


@dataclass(frozen=True)
class ExportFile:
    path: str        # relative, with "/" separators
    abs_path: Path
    size: int
    sha256: str

    def read(self) -> bytes:
        return self.abs_path.read_bytes()


def content_type(path: str) -> str:
    guess, _ = mimetypes.guess_type(path)
    return guess or "application/octet-stream"


# --------------------------------------------------------------------------- HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # a redirect could carry the token to another host: never follow one


class Http:
    """A small HTTP client for one provider API. Every request is recorded (without headers)
    in ``self.log`` for the result."""

    def __init__(self, base: str, *, provider: str, secrets_to_hide: tuple[str, ...] = (),
                 timeout: float = TIMEOUT, max_response: int = MAX_RESPONSE) -> None:
        self.base = base.rstrip("/")
        self.provider = provider
        self.hide = tuple(s for s in secrets_to_hide if s)
        self.timeout = timeout
        self.max_response = max_response
        self.log: list[str] = []
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _clean(self, text: str) -> str:
        for s in self.hide:
            text = text.replace(s, "[hidden]")
        return text

    def request(self, method: str, path: str, *, token: str | None = None, body: bytes | None = None,
                json_body: Any = None, headers: dict[str, str] | None = None,
                ok_missing: bool = False) -> Any:
        url = self.base + path
        hdrs = {"Accept": "application/json", "User-Agent": "mes-expose"}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        self.log.append(f"{method} {path.split('?', 1)[0]}")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read(self.max_response + 1)
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read(64 * 1024)
            except OSError:
                raw = b""
            if status == 404 and ok_missing:
                return None
            raise self._error(status, raw) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TemporaryFailure(
                f"{self.provider} could not be reached, so nothing more was sent. Anything not uploaded yet "
                "stays on this computer.",
                hint="Check the internet connection and try again.",
                code="provider_unreachable",
                detail=self._clean(str(getattr(e, "reason", e)))[:300],
            ) from None
        if len(raw) > self.max_response:
            raise MesError(f"{self.provider} answered with far more data than expected, so mes stopped.",
                           hint="Try again later.", code="provider_bad_answer")
        if 300 <= status < 400:
            raise MesError(f"{self.provider} tried to send the request somewhere else, which mes never follows "
                           "(it could carry your token to another place). Nothing more was sent.",
                           hint="Try again later; if it keeps happening, check the provider's status page.",
                           code="provider_redirect")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise MesError(f"{self.provider} answered with something that is not the data mes expected, so mes "
                           "stopped.", hint="Try again later.", code="provider_bad_answer") from None

    def _error(self, status: int, raw: bytes) -> MesError:
        said = ""
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                errs = data.get("errors")
                if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                    said = str(errs[0].get("message") or "")
                said = said or str(data.get("message") or data.get("error") or "")
                if isinstance(data.get("error"), dict):
                    said = str(data["error"].get("message") or "")
        except (UnicodeDecodeError, ValueError):
            pass
        detail = self._clean(said)[:500] or None
        if status in (401, 403):
            return MesError(
                f"{self.provider} did not accept the token (it may be wrong, expired, or not allowed to do this), "
                "so nothing more was sent.",
                hint="Create a new token with the permissions the share guide lists (mes guide share) and save it "
                "again.", code="provider_token_refused", detail=detail, detail_label="What the provider said:")
        if status == 429 or status >= 500:
            return TemporaryFailure(f"{self.provider} is busy or having a problem (answer {status}), so mes stopped.",
                                    hint="Try again in a few minutes.", code="provider_busy", detail=detail)
        return MesError(f"{self.provider} refused a step of the upload (answer {status}), so mes stopped.",
                        hint="Read what the provider said below; fix that and run mes expose again.",
                        code="provider_refused", detail=detail, detail_label="What the provider said:")


# --------------------------------------------------------------------------- tokens


def check_base(url: str) -> str:
    """An API address mes may send a token to: https, or plain http on this computer (tests)."""
    parsed = urllib.parse.urlsplit(url)
    local = parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS
    if not (parsed.scheme == "https" and parsed.hostname) and not local:
        raise Refused("The provider's API address is not https (or this computer), so mes will not send your token "
                      "or your data there.", hint="This is a mistake in mes; report it.", code="unsafe_api_base")
    return url.rstrip("/")


def fetch_site(url: str) -> tuple[int, str | None]:
    """Open ``url`` the way a stranger would: no token, no cookie, no redirect followed. Returns
    the status and the redirect address (0 when the site could not be reached)."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "mes-expose-check"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location") if e.headers else None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def access_answer(status: int, location: str | None) -> bool:
    """What a protected site answers a stranger: a redirect to Cloudflare Access's sign-in page,
    or "sign in first" / "not allowed"."""
    if status in (401, 403):
        return True
    if status in (302, 303, 307) and isinstance(location, str):
        loc = urllib.parse.urlsplit(location)
        return loc.scheme == "https" and (loc.hostname or "").endswith(".cloudflareaccess.com")
    return False


def _host(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v.startswith("https://"):
        v = v[len("https://"):].rstrip("/")
    return v if _HOST_RE.match(v) else None


def token_file(adapter: "Adapter") -> Path:
    return paths.config_path().parent / "tokens" / adapter.name


def _read_token_file(p: Path) -> str | None:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise Refused(f"The token file {p} is not a plain file (it may be a link to another place), so mes will "
                      "not read it.", hint=f"Replace it with a plain file that holds only the token: {p}",
                      code="unsafe_token_file")
    if st.st_uid != os.getuid():
        raise Refused(f"The token file {p} belongs to another account, so mes will not use it.",
                      hint="Create the file yourself, readable only by you (chmod 600).", code="unsafe_token_file")
    if st.st_mode & 0o077:
        raise Refused(f"Other accounts on this computer can read the token file {p}, so mes will not use it: "
                      "anyone who can read it could publish in your name.",
                      hint=f"Make it private with: chmod 600 {p}  (then create a new token if you think it was read).",
                      code="unsafe_token_file")
    if st.st_size > TOKEN_FILE_MAX:
        raise Refused(f"The token file {p} is too large to be a token.", hint="It must hold only the token.",
                      code="unsafe_token_file")
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def load_token(adapter: "Adapter") -> str:
    value = os.environ.get(adapter.token_env)
    source = "env"
    if not value:
        value = _read_token_file(token_file(adapter))
        source = "file"
    if not value:
        raise NeedsPerson(protocol.needs_human(
            id=f"{adapter.name}-token",
            action=(f"Create an access token in your {adapter.title} account ({adapter.token_help}) and save it "
                    f"yourself in the file {token_file(adapter)} (readable only by you: chmod 600), or put it in "
                    f"the environment variable {adapter.token_env}."),
            why=(f"mes publishes with your own {adapter.title} account. The token is a password for that account, "
                 "so only you should type it; mes never accepts it on the command line and never shows it."),
            rerun="mes expose ... (the same command again)",
            url=adapter.token_url,
        ))
    if not _TOKEN_RE.match(value):
        raise Refused(f"The {adapter.title} token (from the {'environment' if source == 'env' else 'token file'}) "
                      "contains spaces or other characters a token never has, so mes did not use it.",
                      hint="Save only the token itself, on one line.", code="token_invalid")
    return value


# --------------------------------------------------------------------------- adapters


class Adapter:
    name = ""
    title = ""
    token_env = ""
    token_help = ""
    token_url: str | None = None
    modes: tuple[str, ...] = ("link", "public")
    default_base = ""
    available = True
    site_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?\Z")

    def __init__(self, base: str | None = None, *, fetch: Any = None, wait: Any = None) -> None:
        self.base = check_base(base) if base else self.default_base
        self.fetch = fetch or fetch_site
        self.wait = wait or time.sleep

    def site_url(self, site: str) -> str:
        """The address the site would normally get. Only shown before uploading, as what is asked
        for; the result always reports the address the provider answered with."""
        raise NotImplementedError

    def plan(self, site: str, files: list[ExportFile], mode: str, people: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def upload(self, site: str, files_dir: Path, files: list[ExportFile], mode: str, people: list[str],
               token: str, http: Http) -> dict[str, Any]:
        raise NotImplementedError

    def remove_plan(self, site: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def remove(self, site: str, token: str, http: Http) -> dict[str, Any]:
        raise NotImplementedError


def _no_address(provider: str) -> MesError:
    return MesError(f"{provider} did not say at which address the site is, so mes cannot tell you where it is or "
                    "who can open it, and reports no address.",
                    hint="Look at the site in your provider account, or run mes expose again later.",
                    code="provider_no_address")


def _delete_each(http: Http, token: str, paths_: list[str]) -> tuple[int, list[str]]:
    """Delete earlier deployments; a failure is reported, never hidden."""
    removed, kept = 0, []
    for path in paths_:
        try:
            http.request("DELETE", path, token=token, ok_missing=True)
            removed += 1
        except MesError as e:
            kept.append(f"{path.rsplit('/', 1)[-1].split('?', 1)[0]}: {e.message[:120]}")
    return removed, kept


def _step(step: str, method: str, path: str, sends: str) -> dict[str, str]:
    return {"step": step, "method": method, "path": path, "sends": sends}


def cloudflare_hash(f: ExportFile, data: bytes | None = None) -> str:
    """How wrangler names an uploaded file: blake3(base64(content) + extension) in hex, 32 characters."""
    content = f.read() if data is None else data
    ext = Path(f.path).suffix[1:]
    return blake3_hex(base64.b64encode(content) + ext.encode("utf-8"))[:32]


def _account_id() -> str:
    value = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not value:
        cfg = paths.load_config().get("expose", {})
        value = cfg.get("cloudflare_account_id") if isinstance(cfg, dict) else None
    if not value:
        raise NeedsPerson(protocol.needs_human(
            id="cloudflare-account-id",
            action=("Find your Cloudflare account ID (on the Cloudflare dashboard, in the right-hand column of "
                    "\"Workers & Pages\") and put it in the environment variable CLOUDFLARE_ACCOUNT_ID, or under "
                    "[expose] cloudflare_account_id in mes's settings file."),
            why="mes needs to know which Cloudflare account to publish to. The ID is not a secret.",
            rerun="mes expose ... (the same command again)",
            url="https://dash.cloudflare.com/",
        ))
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise Refused("The Cloudflare account ID is not 32 letters and digits (0-9, a-f), so mes did not use it.",
                      hint="Copy the account ID again from the Cloudflare dashboard.", code="account_id_invalid")
    return value


def _result(data: Any) -> Any:
    return data.get("result") if isinstance(data, dict) else None


class CloudflarePages(Adapter):
    name = "cloudflare-pages"
    title = "Cloudflare"
    token_env = "CLOUDFLARE_API_TOKEN"
    token_help = ("My Profile > API Tokens > Create Token, with the permissions \"Cloudflare Pages: Edit\" and, "
                  "for people mode, \"Access: Apps and Policies: Edit\"")
    token_url = "https://dash.cloudflare.com/profile/api-tokens"
    modes = ("people", "link", "public")
    default_base = "https://api.cloudflare.com/client/v4"
    upload_bucket_bytes = 20 * 1024 * 1024

    def site_url(self, site: str) -> str:
        return f"https://{site}.pages.dev"

    def plan(self, site, files, mode, people):
        acc = "{account}"
        steps = [
            _step("Check that the Pages project exists (create it if not) and read its real address", "GET",
                  f"/accounts/{acc}/pages/projects/{site}", "nothing"),
            _step("List the site's earlier versions (deleted once the new one is live)", "GET",
                  f"/accounts/{acc}/pages/projects/{site}/deployments", "nothing"),
        ]
        if mode == "people":
            steps += [
                _step("Find an existing sign-in rule for the site", "GET", f"/accounts/{acc}/access/apps", "nothing"),
                _step("Create or update the Cloudflare Access application (before any file is uploaded)", "POST",
                      f"/accounts/{acc}/access/apps",
                      "the site's real address from Cloudflare's answer, its per-version addresses and custom domains"),
                _step("Remove every other rule of that application, then allow only these people", "POST",
                      f"/accounts/{acc}/access/apps/{{app}}/policies", "the email addresses: " + ", ".join(people)),
            ]
        steps += [
            _step("Ask for an upload permission for this one upload", "GET",
                  f"/accounts/{acc}/pages/projects/{site}/upload-token", "nothing"),
            _step("Ask which files Cloudflare does not have yet", "POST", "/pages/assets/check-missing",
                  f"{len(files)} file fingerprints"),
            _step("Upload the missing files", "POST", "/pages/assets/upload",
                  f"up to {len(files)} files, {sum(f.size for f in files)} bytes in all"),
            _step("Confirm the fingerprints", "POST", "/pages/assets/upsert-hashes", f"{len(files)} file fingerprints"),
            _step("Publish this version of the site", "POST", f"/accounts/{acc}/pages/projects/{site}/deployments",
                  "the list of file paths and fingerprints"),
        ]
        if mode == "people":
            steps.append(_step("Open the site like a stranger and require Cloudflare's sign-in page (otherwise the "
                               "new version is deleted again)", "GET", "https://<the site's address>/", "nothing"))
        steps.append(_step("Delete the earlier versions", "DELETE",
                           f"/accounts/{acc}/pages/projects/{site}/deployments/{{id}}", "nothing"))
        return steps

    def remove_plan(self, site):
        acc = "{account}"
        return [
            _step("List the site's versions", "GET", f"/accounts/{acc}/pages/projects/{site}/deployments", "nothing"),
            _step("Delete each version", "DELETE", f"/accounts/{acc}/pages/projects/{site}/deployments/{{id}}",
                  "nothing"),
            _step("Delete the Pages project", "DELETE", f"/accounts/{acc}/pages/projects/{site}", "nothing"),
            _step("Delete mes's sign-in rule for it (after the site is gone)", "DELETE",
                  f"/accounts/{acc}/access/apps/{{app}}", "nothing"),
        ]

    def _project_hosts(self, project: Any) -> tuple[str, list[str]]:
        """The site's real address (``subdomain``, which is not always ``<name>.pages.dev``: Cloudflare
        adds a suffix when the name is taken) and its custom domains, from Cloudflare's answer."""
        if not isinstance(project, dict):
            raise _no_address("Cloudflare")
        sub = _host(project.get("subdomain"))
        if not sub or not sub.endswith(".pages.dev"):
            raise _no_address("Cloudflare")
        custom = []
        for d in project.get("domains") or []:
            h = _host(d)
            if h and h != sub and not h.endswith("." + sub):
                custom.append(h)
        return sub, sorted(set(custom))[:20]

    def _deployments(self, acc: str, site: str, token: str, http: Http) -> list[str]:
        listed = _result(http.request("GET", f"/accounts/{acc}/pages/projects/{site}/deployments", token=token,
                                      ok_missing=True))
        return [d["id"] for d in listed if isinstance(d, dict) and isinstance(d.get("id"), str)
                and _ID_RE.match(d["id"])] if isinstance(listed, list) else []

    def upload(self, site, files_dir, files, mode, people, token, http):
        acc = _account_id()
        proj = http.request("GET", f"/accounts/{acc}/pages/projects/{site}", token=token, ok_missing=True)
        if proj is None:
            proj = http.request("POST", f"/accounts/{acc}/pages/projects", token=token,
                                json_body={"name": site, "production_branch": "main"})
        sub, custom = self._project_hosts(_result(proj))
        earlier = self._deployments(acc, site, token, http)
        access = None
        if mode == "people":
            access = self._protect(acc, site, [sub, *custom], people, token, http)  # before any file is uploaded
        jwt = _result(http.request("GET", f"/accounts/{acc}/pages/projects/{site}/upload-token", token=token))
        jwt = jwt.get("jwt") if isinstance(jwt, dict) else None
        if not isinstance(jwt, str) or not jwt:
            raise MesError("Cloudflare did not give an upload permission, so nothing was uploaded.",
                           hint="Try again in a minute.", code="provider_bad_answer")
        http.hide = http.hide + (jwt,)
        hashes: dict[str, str] = {}
        contents: dict[str, bytes] = {}
        for f in files:
            data = f.read()
            h = cloudflare_hash(f, data)
            hashes[f.path] = h
            contents[h] = data
        missing = _result(http.request("POST", "/pages/assets/check-missing", token=jwt,
                                       json_body={"hashes": sorted(set(hashes.values()))}))
        if not isinstance(missing, list) or not all(isinstance(m, str) for m in missing):
            raise MesError("Cloudflare's answer about which files it has was not readable, so mes stopped.",
                           hint="Try again later.", code="provider_bad_answer")
        by_hash = {h: p for p, h in hashes.items()}
        bucket: list[dict[str, Any]] = []
        size = 0
        for h in missing:
            if h not in contents:
                continue
            value = base64.b64encode(contents[h]).decode("ascii")
            bucket.append({"key": h, "value": value, "metadata": {"contentType": content_type(by_hash[h])},
                           "base64": True})
            size += len(value)
            if size >= self.upload_bucket_bytes:
                http.request("POST", "/pages/assets/upload", token=jwt, json_body=bucket)
                bucket, size = [], 0
        if bucket:
            http.request("POST", "/pages/assets/upload", token=jwt, json_body=bucket)
        http.request("POST", "/pages/assets/upsert-hashes", token=jwt,
                     json_body={"hashes": sorted(set(hashes.values()))})
        manifest = {"/" + p: h for p, h in sorted(hashes.items())}
        boundary = "mes-" + secrets.token_hex(16)
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"manifest\"\r\n\r\n"
                f"{json.dumps(manifest)}\r\n--{boundary}--\r\n").encode("utf-8")
        dep = _result(http.request("POST", f"/accounts/{acc}/pages/projects/{site}/deployments", token=token,
                                   body=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}))
        dep_id = dep.get("id") if isinstance(dep, dict) and isinstance(dep.get("id"), str) \
            and _ID_RE.match(dep["id"]) else None
        dep_host = _host(dep.get("url")) if isinstance(dep, dict) else None
        if mode == "people":
            hosts = [sub, *custom] + ([dep_host] if dep_host else [])
            if dep_host and dep_host != sub and not dep_host.endswith("." + sub) and dep_host not in custom:
                self._undo(acc, site, dep_id, token, http)
                raise Refused(f"Cloudflare published this version at {dep_host}, which the sign-in rule does not "
                              "cover, so mes deleted it again. Nothing stays online from this upload.",
                              hint="Look at the project in your Cloudflare account, then run mes expose again.",
                              code="access_not_covering")
            self._check_protected(acc, site, dep_id, hosts, token, http)
        removed, kept = _delete_each(http, token, [
            f"/accounts/{acc}/pages/projects/{site}/deployments/{d}?force=true" for d in earlier if d != dep_id])
        out = {"url": f"https://{sub}", "hosts": [sub, *custom],
               "deployment_url": f"https://{dep_host}" if dep_host else None,
               "uploaded": len([h for h in missing if h in contents]), "files": len(files),
               "removed_earlier": removed, "earlier_not_removed": kept}
        if access:
            out["access"] = access
        return out

    def _undo(self, acc, site, dep_id, token, http):
        if dep_id:
            try:
                http.request("DELETE", f"/accounts/{acc}/pages/projects/{site}/deployments/{dep_id}?force=true",
                             token=token, ok_missing=True)
            except MesError:
                pass

    def _check_protected(self, acc, site, dep_id, hosts, token, http):
        """Open every address like a stranger; each must answer with Cloudflare's sign-in page."""
        pending = list(dict.fromkeys(hosts))
        last: dict[str, int] = {}
        for attempt in range(CHECK_TRIES):
            still = []
            for h in pending:
                status, location = self.fetch(f"https://{h}/")
                http.log.append(f"GET https://{h}/ (as a stranger)")
                last[h] = status
                if not access_answer(status, location):
                    still.append(h)
            pending = still
            if not pending:
                return
            if attempt + 1 < CHECK_TRIES:
                self.wait(CHECK_WAIT_SEC)
        self._undo(acc, site, dep_id, token, http)
        raise Refused(
            "The site did not ask for a sign-in when mes opened it like a stranger ("
            + ", ".join(f"{h} answered {last.get(h) or 'nothing'}" for h in pending)
            + "), so mes deleted the new version again instead of saying it is restricted.",
            hint="Check the Access application for the site in your Cloudflare account (Zero Trust > Access), then "
            "run mes expose again.", code="access_not_working")

    def remove(self, site, token, http):
        acc = _account_id()
        proj = http.request("GET", f"/accounts/{acc}/pages/projects/{site}", token=token, ok_missing=True)
        hosts: list[str] = []
        if proj is not None:
            try:
                sub, custom = self._project_hosts(_result(proj))
                hosts = [sub, *custom]
            except MesError:
                hosts = []
            ids = self._deployments(acc, site, token, http)
            # The newest version cannot be deleted on its own; deleting the project removes it.
            removed, kept = _delete_each(http, token, [
                f"/accounts/{acc}/pages/projects/{site}/deployments/{d}?force=true" for d in ids[1:]])
            http.request("DELETE", f"/accounts/{acc}/pages/projects/{site}", token=token, ok_missing=True)
        else:
            removed, kept = 0, []
        apps = _result(http.request("GET", f"/accounts/{acc}/access/apps", token=token))
        rules = 0
        for a in apps if isinstance(apps, list) else []:
            if isinstance(a, dict) and isinstance(a.get("id"), str) and _ID_RE.match(a["id"]) and (
                    a.get("name") == f"mes {site}" or a.get("domain") in hosts):
                http.request("DELETE", f"/accounts/{acc}/access/apps/{a['id']}", token=token, ok_missing=True)
                rules += 1
        return {"removed": proj is not None, "versions_removed": removed + (1 if proj is not None else 0),
                "not_removed": kept, "sign_in_rules_removed": rules, "hosts": hosts}

    def _protect(self, acc, site, hosts, people, token, http):
        domain = hosts[0]
        domains = [domain, f"*.{domain}", *hosts[1:]]
        apps = _result(http.request("GET", f"/accounts/{acc}/access/apps", token=token))
        app_id = None
        for a in apps if isinstance(apps, list) else []:
            if not isinstance(a, dict):
                continue
            theirs = {a.get("domain"), *(a.get("self_hosted_domains") or [])}
            if a.get("name") == f"mes {site}" or theirs & set(domains):
                app_id = a.get("id")
                break
        body = {"name": f"mes {site}", "domain": domain, "self_hosted_domains": domains, "type": "self_hosted",
                "session_duration": "24h"}
        if app_id:
            http.request("PUT", f"/accounts/{acc}/access/apps/{app_id}", token=token, json_body=body)
        else:
            created = _result(http.request("POST", f"/accounts/{acc}/access/apps", token=token, json_body=body))
            app_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(app_id, str) or not _ID_RE.match(app_id):
            raise MesError("Cloudflare did not confirm the sign-in rule for the site, so nothing was uploaded.",
                           hint="Check that the token may edit Access applications (mes guide share).",
                           code="provider_bad_answer")
        base = f"/accounts/{acc}/access/apps/{app_id}/policies"
        # The whole set of rules is replaced: every rule already there (which could let others in) goes
        # first, then the one rule that lets only these people in.
        for p in _result(http.request("GET", base, token=token)) or []:
            if isinstance(p, dict) and isinstance(p.get("id"), str) and _ID_RE.match(p["id"]):
                http.request("DELETE", f"{base}/{p['id']}", token=token, ok_missing=True)
        policy = {"name": "mes: people allowed", "decision": "allow", "precedence": 1,
                  "include": [{"email": {"email": e}} for e in people]}
        made = _result(http.request("POST", base, token=token, json_body=policy))
        ours = made.get("id") if isinstance(made, dict) else None
        now = _result(http.request("GET", base, token=token))
        ids = [p.get("id") for p in now if isinstance(p, dict)] if isinstance(now, list) else None
        if not ours or ids != [ours]:
            raise MesError("Cloudflare did not confirm that the site's only sign-in rule is the list of people, so "
                           "nothing was uploaded.", hint="Look at the Access application in your Cloudflare account "
                           "(Zero Trust > Access), then run mes expose again.", code="access_not_confirmed")
        return {"domains": domains, "people": list(people)}


class Netlify(Adapter):
    name = "netlify"
    title = "Netlify"
    token_env = "NETLIFY_AUTH_TOKEN"
    token_help = "User settings > Applications > Personal access tokens > New access token"
    token_url = "https://app.netlify.com/user/applications#personal-access-tokens"
    default_base = "https://api.netlify.com"

    def site_url(self, site: str) -> str:
        return f"https://{site}.netlify.app"

    def plan(self, site, files, mode, people):
        return [
            _step("Find the site", "GET", f"/api/v1/sites?name={site}", "nothing"),
            _step("Create the site if it does not exist", "POST", "/api/v1/sites", f"the name {site}"),
            _step("List the site's earlier versions (deleted once the new one is live)", "GET",
                  "/api/v1/sites/{site_id}/deploys", "nothing"),
            _step("Upload and publish all files as one zip", "POST", "/api/v1/sites/{site_id}/deploys",
                  f"{len(files)} files, {sum(f.size for f in files)} bytes before compression"),
            _step("Delete the earlier versions", "DELETE", "/api/v1/sites/{site_id}/deploys/{id}", "nothing"),
        ]

    def remove_plan(self, site):
        return [_step("Find the site", "GET", f"/api/v1/sites?name={site}", "nothing"),
                _step("Delete the site with every version", "DELETE", "/api/v1/sites/{site_id}", "nothing")]

    def _find(self, site, token, http):
        found = http.request("GET", "/api/v1/sites?" + urllib.parse.urlencode({"name": site, "filter": "all"}),
                             token=token)
        for s in found if isinstance(found, list) else []:
            if isinstance(s, dict) and s.get("name") == site and isinstance(s.get("id"), str) and _ID_RE.match(s["id"]):
                return s
        return None

    def upload(self, site, files_dir, files, mode, people, token, http):
        found = self._find(site, token, http)
        if found is None:
            found = http.request("POST", "/api/v1/sites", token=token, json_body={"name": site})
        site_id = found.get("id") if isinstance(found, dict) else None
        if not isinstance(site_id, str) or not _ID_RE.match(site_id):
            raise MesError("Netlify did not confirm the site, so nothing was uploaded.", hint="Try again later.",
                           code="provider_bad_answer")
        listed = http.request("GET", f"/api/v1/sites/{site_id}/deploys", token=token, ok_missing=True)
        earlier = [d["id"] for d in listed if isinstance(d, dict) and isinstance(d.get("id"), str)
                   and _ID_RE.match(d["id"])] if isinstance(listed, list) else []
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.writestr(f.path, f.read())
        dep = http.request("POST", f"/api/v1/sites/{site_id}/deploys", token=token, body=buf.getvalue(),
                           headers={"Content-Type": "application/zip"})
        host = _host(dep.get("ssl_url")) if isinstance(dep, dict) else None
        host = host or _host(found.get("ssl_url"))
        if not host:
            raise _no_address("Netlify")
        dep_id = dep.get("id") if isinstance(dep, dict) else None
        removed, kept = _delete_each(http, token, [f"/api/v1/sites/{site_id}/deploys/{d}" for d in earlier
                                                   if d != dep_id])
        return {"url": f"https://{host}", "files": len(files), "removed_earlier": removed, "earlier_not_removed": kept}

    def remove(self, site, token, http):
        found = self._find(site, token, http)
        if found is None:
            return {"removed": False, "versions_removed": 0, "not_removed": []}
        http.request("DELETE", f"/api/v1/sites/{found['id']}", token=token, ok_missing=True)
        return {"removed": True, "not_removed": []}


class Vercel(Adapter):
    name = "vercel"
    title = "Vercel"
    token_env = "VERCEL_TOKEN"
    token_help = "Account Settings > Tokens > Create"
    token_url = "https://vercel.com/account/tokens"
    default_base = "https://api.vercel.com"
    site_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?\Z")

    def site_url(self, site: str) -> str:
        return f"https://{site}.vercel.app"

    def _team(self, joiner: str = "?") -> str:
        team = os.environ.get("VERCEL_TEAM_ID")
        if team and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", team):
            return joiner + urllib.parse.urlencode({"teamId": team})
        return ""

    def plan(self, site, files, mode, people):
        return [
            _step("List the site's earlier versions (deleted once the new one is live)", "GET",
                  f"/v6/deployments?app={site}", "nothing"),
            _step("Upload each file", "POST", "/v2/files", f"{len(files)} files, {sum(f.size for f in files)} bytes"),
            _step("Publish this version of the site", "POST", "/v13/deployments",
                  f"the name {site} and the list of file paths and fingerprints"),
            _step("Delete the earlier versions", "DELETE", "/v13/deployments/{id}", "nothing"),
        ]

    def remove_plan(self, site):
        return [_step("Delete the project with every version", "DELETE", f"/v9/projects/{site}", "nothing")]

    def upload(self, site, files_dir, files, mode, people, token, http):
        team = self._team()
        listed = http.request("GET", "/v6/deployments?" + urllib.parse.urlencode({"app": site, "limit": 100})
                              + self._team("&"), token=token, ok_missing=True)
        deps = listed.get("deployments") if isinstance(listed, dict) else None
        earlier = [d["uid"] for d in deps if isinstance(d, dict) and isinstance(d.get("uid"), str)
                   and _ID_RE.match(d["uid"])] if isinstance(deps, list) else []
        uploaded = []
        for f in files:
            data = f.read()
            sha = hashlib.sha1(data).hexdigest()  # noqa: S324 - Vercel names files by SHA-1
            http.request("POST", "/v2/files" + team, token=token, body=data,
                         headers={"Content-Type": "application/octet-stream", "x-vercel-digest": sha,
                                  "x-vercel-size": str(len(data))})
            uploaded.append({"file": f.path, "sha": sha, "size": len(data)})
        dep = http.request("POST", "/v13/deployments" + team, token=token,
                           json_body={"name": site, "files": uploaded, "target": "production",
                                      "projectSettings": {"framework": None}})
        host = None
        if isinstance(dep, dict):
            alias = dep.get("alias")
            host = _host(alias[0]) if isinstance(alias, list) and alias else None
            host = host or _host(dep.get("url"))
        if not host:
            raise _no_address("Vercel")
        dep_id = dep.get("id") if isinstance(dep, dict) else None
        removed, kept = _delete_each(http, token, [f"/v13/deployments/{d}{team}" for d in earlier if d != dep_id])
        return {"url": f"https://{host}", "files": len(files), "removed_earlier": removed, "earlier_not_removed": kept}

    def remove(self, site, token, http):
        gone = http.request("DELETE", f"/v9/projects/{site}" + self._team(), token=token, ok_missing=True)
        return {"removed": gone is not None, "not_removed": []}


class NotYet(Adapter):
    available = False

    def __init__(self, name: str, title: str) -> None:
        super().__init__()
        self.name = name
        self.title = title

    def refuse(self) -> NotAvailableYet:
        return NotAvailableYet(
            f"Sharing through {self.title} is not available yet in this version of mes. Nothing was shared.",
            hint="Use --to cloudflare-pages (recommended; it can also limit a dashboard to people you name), "
            "--to netlify or --to vercel.",
        )


ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (CloudflarePages(), Netlify(), Vercel(), NotYet("github-pages", "GitHub Pages"),
                        NotYet("render", "Render"))
}
