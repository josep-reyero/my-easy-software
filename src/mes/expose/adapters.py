"""Hosting adapters for ``mes expose``: one small class per provider.

Each adapter talks to the provider's own HTTP API with the person's own token (standard library
only). It never receives the link-mode key and never sees anything but the exported files.

Interface
---------
``Adapter.name``       the provider name used with ``--to``.
``Adapter.token_env``  the environment variable holding the token.
``Adapter.modes``      the sharing modes it supports (``people``, ``link``, ``public``).
``Adapter.default_base``  the API address (``MES_EXPOSE_API_BASE`` replaces it, for tests).
``Adapter.plan(site, files, mode, people) -> list[dict]``  every step, for ``--dry-run``.
``Adapter.upload(site, files_dir, files, mode, people, token, http) -> dict``  with ``url``.

``files`` is a list of ``ExportFile(path, abs_path, size, sha256)``.

Also here: ``Http`` (a tiny JSON/bytes client that never follows redirects, has timeouts and a
response size limit, and keeps the token out of every message), ``api_base(adapter)``,
``load_token(adapter)``, ``ADAPTERS``.
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


def api_base(adapter: "Adapter") -> str:
    override = os.environ.get("MES_EXPOSE_API_BASE")
    if not override:
        return adapter.default_base
    parsed = urllib.parse.urlsplit(override)
    local = parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS
    if not (parsed.scheme == "https" and parsed.hostname) and not local:
        raise Refused(
            "MES_EXPOSE_API_BASE points to an address that is not https (or this computer), so mes will not send "
            "your token or your data there.",
            hint="Remove MES_EXPOSE_API_BASE; it is only for testing.",
            code="unsafe_api_base",
        )
    return override.rstrip("/")


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

    def site_url(self, site: str) -> str:
        raise NotImplementedError

    def plan(self, site: str, files: list[ExportFile], mode: str, people: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def upload(self, site: str, files_dir: Path, files: list[ExportFile], mode: str, people: list[str],
               token: str, http: Http) -> dict[str, Any]:
        raise NotImplementedError


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
        steps = []
        if mode == "people":
            steps += [
                _step("Find an existing sign-in rule for the site", "GET", f"/accounts/{acc}/access/apps", "nothing"),
                _step("Create or update the Cloudflare Access application (before any file is uploaded)", "POST",
                      f"/accounts/{acc}/access/apps", f"the domains {site}.pages.dev and *.{site}.pages.dev"),
                _step("Allow only these people to sign in", "POST",
                      f"/accounts/{acc}/access/apps/{{app}}/policies", "the email addresses: " + ", ".join(people)),
            ]
        steps += [
            _step("Check that the Pages project exists (create it if not)", "GET",
                  f"/accounts/{acc}/pages/projects/{site}", "nothing"),
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
        return steps

    def upload(self, site, files_dir, files, mode, people, token, http):
        acc = _account_id()
        access = None
        if mode == "people":
            access = self._protect(acc, site, people, token, http)  # before any file is public
        proj = http.request("GET", f"/accounts/{acc}/pages/projects/{site}", token=token, ok_missing=True)
        if proj is None:
            http.request("POST", f"/accounts/{acc}/pages/projects", token=token,
                         json_body={"name": site, "production_branch": "main"})
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
        out = {"url": self.site_url(site),
               "deployment_url": dep.get("url") if isinstance(dep, dict) and _https(dep.get("url")) else None,
               "uploaded": len([h for h in missing if h in contents]), "files": len(files)}
        if access:
            out["access"] = access
        return out

    def _protect(self, acc, site, people, token, http):
        domain = f"{site}.pages.dev"
        domains = [domain, f"*.{domain}"]
        apps = _result(http.request("GET", f"/accounts/{acc}/access/apps", token=token))
        app_id = None
        for a in apps if isinstance(apps, list) else []:
            if isinstance(a, dict) and (a.get("domain") == domain or domain in (a.get("self_hosted_domains") or [])):
                app_id = a.get("id")
                break
        body = {"name": f"mes {site}", "domain": domain, "self_hosted_domains": domains, "type": "self_hosted",
                "session_duration": "24h"}
        if app_id:
            http.request("PUT", f"/accounts/{acc}/access/apps/{app_id}", token=token, json_body=body)
        else:
            created = _result(http.request("POST", f"/accounts/{acc}/access/apps", token=token, json_body=body))
            app_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(app_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", app_id):
            raise MesError("Cloudflare did not confirm the sign-in rule for the site, so nothing was uploaded.",
                           hint="Check that the token may edit Access applications (mes guide share).",
                           code="provider_bad_answer")
        policy = {"name": "mes: people allowed", "decision": "allow", "precedence": 1,
                  "include": [{"email": {"email": e}} for e in people]}
        policies = _result(http.request("GET", f"/accounts/{acc}/access/apps/{app_id}/policies", token=token))
        pol_id = None
        for p in policies if isinstance(policies, list) else []:
            if isinstance(p, dict) and p.get("name") == policy["name"]:
                pol_id = p.get("id")
        if isinstance(pol_id, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", pol_id):
            http.request("PUT", f"/accounts/{acc}/access/apps/{app_id}/policies/{pol_id}", token=token,
                         json_body=policy)
        else:
            http.request("POST", f"/accounts/{acc}/access/apps/{app_id}/policies", token=token, json_body=policy)
        return {"domains": domains, "people": list(people)}


def _https(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("https://") and len(value) < 500 and " " not in value


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
            _step("Upload and publish all files as one zip", "POST", "/api/v1/sites/{site_id}/deploys",
                  f"{len(files)} files, {sum(f.size for f in files)} bytes before compression"),
        ]

    def upload(self, site, files_dir, files, mode, people, token, http):
        found = http.request("GET", "/api/v1/sites?" + urllib.parse.urlencode({"name": site, "filter": "all"}),
                             token=token)
        site_id = None
        for s in found if isinstance(found, list) else []:
            if isinstance(s, dict) and s.get("name") == site:
                site_id = s.get("id")
        if not site_id:
            created = http.request("POST", "/api/v1/sites", token=token, json_body={"name": site})
            site_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(site_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", site_id):
            raise MesError("Netlify did not confirm the site, so nothing was uploaded.", hint="Try again later.",
                           code="provider_bad_answer")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.writestr(f.path, f.read())
        dep = http.request("POST", f"/api/v1/sites/{site_id}/deploys", token=token, body=buf.getvalue(),
                           headers={"Content-Type": "application/zip"})
        url = dep.get("ssl_url") if isinstance(dep, dict) else None
        return {"url": url if _https(url) else self.site_url(site), "files": len(files)}


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

    def _team(self) -> str:
        team = os.environ.get("VERCEL_TEAM_ID")
        if team and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", team):
            return "?" + urllib.parse.urlencode({"teamId": team})
        return ""

    def plan(self, site, files, mode, people):
        return [
            _step("Upload each file", "POST", "/v2/files", f"{len(files)} files, {sum(f.size for f in files)} bytes"),
            _step("Publish this version of the site", "POST", "/v13/deployments",
                  f"the name {site} and the list of file paths and fingerprints"),
        ]

    def upload(self, site, files_dir, files, mode, people, token, http):
        team = self._team()
        listed = []
        for f in files:
            data = f.read()
            sha = hashlib.sha1(data).hexdigest()  # noqa: S324 - Vercel names files by SHA-1
            http.request("POST", "/v2/files" + team, token=token, body=data,
                         headers={"Content-Type": "application/octet-stream", "x-vercel-digest": sha,
                                  "x-vercel-size": str(len(data))})
            listed.append({"file": f.path, "sha": sha, "size": len(data)})
        dep = http.request("POST", "/v13/deployments" + team, token=token,
                           json_body={"name": site, "files": listed, "target": "production",
                                      "projectSettings": {"framework": None}})
        url = None
        if isinstance(dep, dict):
            alias = dep.get("alias")
            first = alias[0] if isinstance(alias, list) and alias and isinstance(alias[0], str) else dep.get("url")
            if isinstance(first, str) and re.fullmatch(r"[a-z0-9.-]{1,253}", first):
                url = "https://" + first
        return {"url": url or self.site_url(site), "files": len(files)}


class NotYet(Adapter):
    available = False

    def __init__(self, name: str, title: str) -> None:
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
