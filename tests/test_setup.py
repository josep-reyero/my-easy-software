"""Tests for mes setup / mes doctor (mes.setup, mes.checks, mes.messages).

Every external command goes through a fake runner, so no test touches the real
machine: nothing is installed, no sudo, and ``tailscale up`` is never really run.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

from mes import checks, cli, messages, protocol, setup
from mes.checks import CommandResult
from mes.errors import ConfigError, ExitCode, UsageError

DESIGN = Path(__file__).resolve().parents[1] / "docs" / "design.md"

UBUNTU = {"ID": "ubuntu", "ID_LIKE": "debian", "VERSION_CODENAME": "noble", "PRETTY_NAME": "Ubuntu 24.04 LTS"}
DEBIAN = {"ID": "debian", "VERSION_CODENAME": "bookworm", "PRETTY_NAME": "Debian GNU/Linux 12"}
ARCH = {"ID": "arch", "PRETTY_NAME": "Arch Linux"}
MINT = {"ID": "linuxmint", "ID_LIKE": "ubuntu debian", "UBUNTU_CODENAME": "jammy", "VERSION_CODENAME": "virginia"}
FEDORA_40 = {"ID": "fedora", "VERSION_ID": "40"}
FEDORA_42 = {"ID": "fedora", "VERSION_ID": "42"}
OTHER_LINUX = {"ID": "alpine", "PRETTY_NAME": "Alpine Linux"}

SIGN_IN_URL = "https://login.tailscale.com/a/abc123"


def status(backend: str, **extra) -> str:
    data = {"BackendState": backend, "Self": {"DNSName": "my-server.example-net.ts.net."},
            "CurrentTailnet": {"Name": "example-net"}}
    data.update(extra)
    return json.dumps(data)


class Fake:
    """A fake machine: which programs exist and what each command prints."""

    def __init__(self, *, system="Linux", os_release=None, which=None, exists=(), commands=None,
                 python=(3, 13, 1), user="someone"):
        self.system = system
        self.os_release = dict(UBUNTU if os_release is None and system == "Linux" else (os_release or {}))
        self.paths = dict(which or {})
        self.files = set(exists)
        self.commands = dict(commands or {})
        self.python = python
        self.user = user
        self.calls: list[tuple[str, ...]] = []

    @staticmethod
    def _key(argv):
        return (os.path.basename(argv[0]).lower(),) + tuple(argv[1:])

    def run(self, argv, timeout=10.0):
        argv = tuple(str(a) for a in argv)
        self.calls.append(argv)
        key = self._key(argv)
        for prefix, res in self.commands.items():
            if key[: len(prefix)] == prefix:
                if callable(res):
                    res = res(argv)
                return CommandResult(argv, *res)
        return CommandResult(argv, 127, "", f"{argv[0]}: not found")

    def probe(self) -> checks.Probe:
        return checks.Probe(
            run=self.run,
            which=lambda name: self.paths.get(name),
            exists=lambda p: p in self.files,
            system=self.system,
            os_release=self.os_release,
            python=self.python,
            user=self.user,
        )

    def ran(self, *words) -> bool:
        return any(self._key(c)[: len(words)] == words for c in self.calls)


def linux(**over) -> Fake:
    which = {"uv": "/usr/bin/uv", "systemctl": "/usr/bin/systemctl", "tailscale": "/usr/bin/tailscale"}
    commands = {
        ("uv", "--version"): (0, "uv 0.6.6 (abc 2025-03-12)\n", ""),
        ("systemctl", "--user", "is-system-running"): (0, "running\n", ""),
        ("loginctl", "show-user"): (0, "yes\n", ""),
        ("tailscale", "status", "--json"): (0, status("Running"), ""),
    }
    which.update(over.pop("which", {}))
    commands.update(over.pop("commands", {}))
    for name in over.pop("missing", ()):
        which.pop(name, None)
    return Fake(system="Linux", which=which, commands=commands, **over)


def mac(**over) -> Fake:
    which = {"uv": "/opt/homebrew/bin/uv", "brew": "/opt/homebrew/bin/brew", "tailscale": "/usr/local/bin/tailscale"}
    commands = {
        ("uv", "--version"): (0, "uv 0.6.6\n", ""),
        ("tailscale", "status", "--json"): (0, status("Running"), ""),
    }
    exists = set(over.pop("exists", {checks.MAC_APP, checks.MAC_APP_CLI}))
    which.update(over.pop("which", {}))
    commands.update(over.pop("commands", {}))
    for name in over.pop("missing", ()):
        which.pop(name, None)
    return Fake(system="Darwin", which=which, commands=commands, exists=exists, **over)


def by_id(result) -> dict:
    return {i["id"]: i for i in result["items"]}


def items_for(fake: Fake, mode=None) -> dict:
    items, _ = checks.run_checks(fake.probe(), mode=mode)
    return {i["id"]: i for i in items}


NO_TAILSCALE = {"missing": ("tailscale",)}


# --------------------------------------------------------------------------- wording


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(">", " ").replace("**", "")).strip()


def test_offer_is_word_for_word_from_design():
    design = _plain(DESIGN.read_text(encoding="utf-8"))
    assert _plain(messages.TAILSCALE_OFFER) in design
    assert messages.TAILSCALE_OFFER.startswith("Keeping your apps private. Your apps should only open")
    assert messages.TAILSCALE_OFFER.endswith("Install it now?")


def test_every_check_text_is_present():
    for check, texts in messages.T.items():
        assert texts["title"].strip()
        assert all(v.strip() for v in texts.values()), check


# --------------------------------------------------------------------------- runner


def test_run_command_ok_missing_and_timeout():
    ok = checks.run_command([sys.executable, "-c", "print('hi')"])
    assert ok.ok and ok.stdout.strip() == "hi"
    missing = checks.run_command(["mes-test-no-such-program-xyz"])
    assert missing.returncode == 127 and not missing.ok
    slow = checks.run_command([sys.executable, "-c", "import time; print('x', flush=True); time.sleep(5)"], timeout=0.5)
    assert slow.returncode == 124


def test_read_os_release(tmp_path):
    f = tmp_path / "os-release"
    f.write_text('# comment\nID=ubuntu\nPRETTY_NAME="Ubuntu 24.04 LTS"\nVERSION_CODENAME=noble\nbroken line\n')
    assert checks.read_os_release(str(f)) == {"ID": "ubuntu", "PRETTY_NAME": "Ubuntu 24.04 LTS", "VERSION_CODENAME": "noble"}
    assert checks.read_os_release(str(tmp_path / "missing")) == {}


def test_real_probe_has_defaults():
    p = checks.Probe()
    assert p.system and p.python[:2] >= (3, 12)


# --------------------------------------------------------------------------- platform / python / uv


@pytest.mark.parametrize(
    "system,rel,kind,distro,codename",
    [
        ("Darwin", {}, "macos", None, None),
        ("Linux", UBUNTU, "linux", "ubuntu", "noble"),
        ("Linux", DEBIAN, "linux", "debian", "bookworm"),
        ("Linux", ARCH, "linux", "arch", None),
        ("Linux", {"ID": "manjaro", "ID_LIKE": "arch"}, "linux", "arch", None),
        ("Linux", MINT, "linux", "ubuntu", "jammy"),
        ("Linux", FEDORA_42, "linux", "fedora", None),
        ("Linux", OTHER_LINUX, "linux", None, None),
        ("Windows", {}, "other", None, None),
    ],
)
def test_detect_platform(system, rel, kind, distro, codename):
    plat = checks.detect_platform(Fake(system=system, os_release=rel).probe())
    assert (plat.kind, plat.distro, plat.codename) == (kind, distro, codename)


def test_platform_items():
    assert items_for(mac())["platform"]["status"] == "done"
    assert "Linux server" in items_for(mac())["platform"]["why"]
    lx = items_for(linux())["platform"]
    assert lx["status"] == "done" and lx["detail"] == "Ubuntu 24.04 LTS"


def test_unsupported_platform_blocks_and_skips_the_rest():
    fake = Fake(system="Windows", which={"uv": "uv"}, commands={("uv", "--version"): (0, "uv 0.6.6", "")})
    items = items_for(fake)
    assert items["platform"]["status"] == "blocked"
    assert items["systemd"]["status"] == "skipped"
    assert all(items[i]["status"] == "skipped" for i in ("tailscale-installed", "tailscale-running", "tailscale-signed-in"))
    assert not fake.ran("tailscale")


def test_python_too_old():
    it = items_for(linux(python=(3, 11, 9)))["python"]
    assert it["status"] == "todo" and it["fix"] == "uv python install 3.13"
    assert "3.11.9" in it["detail"]
    assert items_for(linux())["python"]["status"] == "done"


def test_uv_found_reports_version():
    it = items_for(linux())["uv"]
    assert it["status"] == "done" and it["detail"] == "uv 0.6.6"


@pytest.mark.parametrize(
    "fake,fix",
    [
        (mac(missing=("uv",)), "brew install uv"),
        (mac(missing=("uv", "brew")), "curl -LsSf https://astral.sh/uv/install.sh | sh"),
        (linux(missing=("uv",), os_release=ARCH), "sudo pacman -S uv"),
        (linux(missing=("uv",)), "curl -LsSf https://astral.sh/uv/install.sh | sh"),
    ],
)
def test_uv_missing_gives_official_install(fake, fix):
    it = items_for(fake)["uv"]
    assert it["status"] == "todo" and it["fix"] == fix and not it["needs_human"]


def test_uv_broken():
    fake = linux(commands={("uv", "--version"): (1, "", "segfault")})
    it = items_for(fake)["uv"]
    assert it["status"] == "todo" and "damaged" in it["why"] and it["detail"] == "segfault"


# --------------------------------------------------------------------------- systemd


def test_systemd_on_mac_is_skipped_and_fine():
    items = items_for(mac())
    assert items["systemd"]["status"] == "skipped"
    assert "Linux server" in items["systemd"]["why"] and "that is fine" in items["systemd"]["why"]
    assert "systemd-linger" not in items


def test_systemd_running_with_linger():
    items = items_for(linux())
    assert items["systemd"]["status"] == "done" and items["systemd-linger"]["status"] == "done"


def test_systemd_degraded_still_works():
    items = items_for(linux(commands={("systemctl", "--user", "is-system-running"): (1, "degraded\n", "")}))
    assert items["systemd"]["status"] == "done" and "degraded" in items["systemd"]["detail"]


def test_systemd_no_user_session():
    fake = linux(commands={("systemctl", "--user", "is-system-running"): (1, "", "Failed to connect to bus: No medium found")})
    it = items_for(fake)["systemd"]
    assert it["status"] == "todo" and it["fix"] == 'sudo loginctl enable-linger "$USER"'
    assert "Failed to connect to bus" in it["detail"]


def test_systemd_linger_off():
    fake = linux(commands={("loginctl", "show-user"): (0, "no\n", "")})
    it = items_for(fake)["systemd-linger"]
    assert it["status"] == "todo" and "log out" in it["why"] and "enable-linger" in it["fix"]
    assert fake.ran("loginctl", "show-user", "someone", "--property=Linger", "--value")


def test_no_systemd_at_all():
    items = items_for(linux(missing=("systemctl",)))
    assert items["systemd"]["status"] == "blocked" and items["systemd-linger"]["status"] == "blocked"


# --------------------------------------------------------------------------- Tailscale checks


@pytest.mark.parametrize(
    "fake,expected",
    [
        (mac(**NO_TAILSCALE, exists=()), "brew install --cask tailscale"),
        (linux(**NO_TAILSCALE, os_release=ARCH), "sudo pacman -S tailscale && sudo systemctl enable --now tailscaled"),
        (linux(**NO_TAILSCALE, os_release=OTHER_LINUX), "curl -fsSL https://tailscale.com/install.sh | sh"),
    ],
)
def test_install_command_exact(fake, expected):
    it = items_for(fake)["tailscale-installed"]
    assert it["status"] == "todo" and it["fix"] == expected and not it["needs_human"]


def test_install_command_debian_and_ubuntu_use_official_repo():
    for rel, distro, codename in ((UBUNTU, "ubuntu", "noble"), (DEBIAN, "debian", "bookworm"), (MINT, "ubuntu", "jammy")):
        fix = items_for(linux(**NO_TAILSCALE, os_release=rel))["tailscale-installed"]["fix"]
        assert f"https://pkgs.tailscale.com/stable/{distro}/{codename}.noarmor.gpg" in fix
        assert f"https://pkgs.tailscale.com/stable/{distro}/{codename}.tailscale-keyring.list" in fix
        assert fix.endswith("sudo apt-get update && sudo apt-get install -y tailscale")


def test_install_command_fedora_old_and_new_dnf():
    old = items_for(linux(**NO_TAILSCALE, os_release=FEDORA_40))["tailscale-installed"]["fix"]
    new = items_for(linux(**NO_TAILSCALE, os_release=FEDORA_42))["tailscale-installed"]["fix"]
    repo = "https://pkgs.tailscale.com/stable/fedora/tailscale.repo"
    assert old.startswith(f"sudo dnf config-manager --add-repo {repo}")
    assert new.startswith(f"sudo dnf config-manager addrepo --from-repofile={repo}")
    for fix in (old, new):
        assert "sudo dnf install -y tailscale" in fix and fix.endswith("sudo systemctl enable --now tailscaled")


def test_install_on_mac_without_homebrew_needs_the_person():
    it = items_for(mac(missing=("tailscale", "brew"), exists=()))["tailscale-installed"]
    assert it["status"] == "todo" and it["fix"] is None and it["needs_human"]
    assert "tailscale.com/download/mac" in it["detail"]


def test_not_installed_blocks_the_next_items():
    items = items_for(linux(**NO_TAILSCALE))
    assert items["tailscale-running"]["status"] == "blocked"
    assert items["tailscale-signed-in"]["status"] == "blocked"


def test_mac_app_without_cli_on_path_uses_the_app():
    fake = mac(missing=("tailscale",))
    items = items_for(fake)
    assert items["tailscale-installed"]["status"] == "done"
    assert checks.MAC_APP_CLI in items["tailscale-installed"]["detail"]
    assert (checks.MAC_APP_CLI, "status", "--json") in fake.calls


def test_mac_app_alone_without_cli_counts_as_installed():
    fake = mac(missing=("tailscale",), exists={checks.MAC_APP})
    items = items_for(fake)
    assert items["tailscale-installed"]["status"] == "done"
    assert items["tailscale-running"]["status"] == "todo"
    assert items["tailscale-running"]["fix"] == "open -a Tailscale"


def test_daemon_not_running_linux():
    fake = linux(commands={("tailscale", "status", "--json"): (1, "", "failed to connect to local tailscaled")})
    it = items_for(fake)["tailscale-running"]
    assert it["status"] == "todo" and it["fix"] == "sudo systemctl enable --now tailscaled"
    assert "failed to connect" in it["detail"]
    assert items_for(fake)["tailscale-signed-in"]["status"] == "blocked"


def test_daemon_not_running_mac_mentions_allow():
    fake = mac(commands={("tailscale", "status", "--json"): (1, "", "The Tailscale GUI is not running")})
    it = items_for(fake)["tailscale-running"]
    assert it["fix"] == "open -a Tailscale" and "Allow" in it["why"]


def test_mac_cli_only_start_command():
    fake = mac(exists=(), commands={("tailscale", "status", "--json"): (1, "", "no daemon")})
    assert items_for(fake)["tailscale-running"]["fix"] == "sudo tailscaled install-system-daemon"


def test_status_nostate_means_not_running():
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NoState"), "")})
    assert items_for(fake)["tailscale-running"]["status"] == "todo"


def test_stopped_means_signed_in_but_switched_off():
    items = items_for(linux(commands={("tailscale", "status", "--json"): (0, status("Stopped"), "")}))
    assert items["tailscale-running"]["status"] == "todo"
    assert items["tailscale-running"]["fix"] == "sudo tailscale up"
    assert items["tailscale-signed-in"]["status"] == "done"
    mac_items = items_for(mac(commands={("tailscale", "status", "--json"): (0, status("Stopped"), "")}))
    assert mac_items["tailscale-running"]["fix"] == "tailscale up"


def test_starting_asks_to_wait():
    it = items_for(linux(commands={("tailscale", "status", "--json"): (0, status("Starting"), "")}))["tailscale-running"]
    assert it["status"] == "todo" and it["fix"] is None and "moment" in it["why"]


def test_running_and_signed_in():
    items = items_for(linux())
    assert items["tailscale-running"]["status"] == "done"
    signed = items["tailscale-signed-in"]
    assert signed["status"] == "done"
    # Only the device's own name: the network's name can identify the owner.
    assert signed["detail"] == "this computer's private name: my-server"
    assert "example-net" not in signed["detail"] and ".ts.net" not in signed["detail"]


def test_needs_login_needs_the_person():
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsLogin", AuthURL=SIGN_IN_URL), "")})
    it = items_for(fake)["tailscale-signed-in"]
    assert it["status"] == "todo" and it["needs_human"] and it["detail"] == SIGN_IN_URL


def test_needs_machine_auth():
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsMachineAuth"), "")})
    it = items_for(fake)["tailscale-signed-in"]
    assert it["needs_human"] and it["detail"] == messages.APPROVE_URL


def test_garbage_status_output():
    fake = linux(commands={("tailscale", "status", "--json"): (0, "not json", "")})
    it = items_for(fake)["tailscale-running"]
    assert it["status"] == "todo" and it["detail"] == "not json"


def test_local_only_skips_unfinished_tailscale_items_and_explains():
    items = items_for(linux(**NO_TAILSCALE), mode="local-only")
    first = items["tailscale-installed"]
    assert first["status"] == "skipped"
    assert "What works:" in first["why"] and "What does not work:" in first["why"]
    assert "phone" in first["why"]
    assert items["tailscale-running"]["status"] == "skipped"
    assert items["tailscale-signed-in"]["why"] == messages.LOCAL_ONLY_NOT_NEEDED


def test_local_only_keeps_done_items_done():
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsLogin"), "")})
    items = items_for(fake, mode="local-only")
    assert items["tailscale-installed"]["status"] == "done"
    assert items["tailscale-running"]["status"] == "done"
    assert items["tailscale-signed-in"]["status"] == "skipped"
    assert "What works:" in items["tailscale-signed-in"]["why"]


# --------------------------------------------------------------------------- config check


def test_config_missing_broken_and_ok(mes_home):
    cfg = mes_home["config"]
    assert items_for(linux())["config"]["status"] == "todo"
    assert items_for(linux())["config"]["fix"] == "mes setup"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[network\n")
    broken = items_for(linux())["config"]
    assert broken["status"] == "todo" and broken["fix"] is None and str(cfg) in broken["detail"]
    cfg.write_text('[network]\nmode = "local-only"\n')
    ok = items_for(linux())["config"]
    assert ok["status"] == "done" and "local-only" in ok["detail"]


def test_unknown_mode_in_config_is_ignored(mes_home):
    mes_home["config"].parent.mkdir(parents=True)
    mes_home["config"].write_text('[network]\nmode = "banana"\n')
    assert checks.network_mode(tomllib.loads(mes_home["config"].read_text())) is None


# --------------------------------------------------------------------------- doctor


def test_doctor_all_good_linux():
    fake = linux()
    setup._ensure_config(checks.config_state(), "tailscale")
    result = setup.run_doctor(probe=fake.probe())
    assert result["ok"] is True
    assert protocol.exit_code_for(result) == 0


def test_doctor_never_changes_anything(mes_home):
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsLogin"), "")})
    result = setup.run_doctor(probe=fake.probe())
    assert not mes_home["config"].exists()
    assert not fake.ran("tailscale", "up")
    assert result["ok"] is False
    assert protocol.exit_code_for(result) == ExitCode.NEEDS_HUMAN


def test_doctor_missing_tailscale_is_a_fix_not_a_question():
    result = setup.run_doctor(probe=linux(**NO_TAILSCALE).probe())
    assert protocol.is_checklist(result)
    it = by_id(result)["tailscale-installed"]
    assert it["status"] == "todo" and it["fix"]
    assert protocol.exit_code_for(result) == ExitCode.ERROR


# --------------------------------------------------------------------------- setup


def test_setup_asks_about_tailscale_with_exact_wording(mes_home):
    result = setup.run_setup({}, probe=linux(**NO_TAILSCALE).probe())
    assert result["status"] == "needs_input" and result["id"] == "tailscale"
    assert result["question"] == messages.TAILSCALE_OFFER
    assert [o["value"] for o in result["options"]] == ["install", "local-only"]
    assert result["recommended"] == "install"
    assert result["rerun"] == "mes setup --answer tailscale=<value>"
    assert protocol.exit_code_for(result) == 6
    assert not mes_home["config"].exists()  # nothing saved before the person decides


def test_setup_question_rerun_keeps_json():
    result = setup.run_setup({}, probe=mac(**NO_TAILSCALE, exists=()).probe(), json_mode=True)
    assert result["rerun"] == "mes --json setup --answer tailscale=<value>"


def test_setup_local_only_is_saved_and_reported(mes_home):
    fake = linux(**NO_TAILSCALE)
    result = setup.run_setup({"tailscale": "local-only"}, probe=fake.probe())
    assert protocol.is_checklist(result) and result["ok"] is True
    assert by_id(result)["tailscale-installed"]["status"] == "skipped"
    assert "What does not work:" in by_id(result)["tailscale-installed"]["why"]
    assert by_id(result)["config"]["status"] == "done"
    assert tomllib.loads(mes_home["config"].read_text())["network"]["mode"] == "local-only"
    # A later run remembers the choice and does not ask again.
    again = setup.run_setup({}, probe=fake.probe())
    assert protocol.is_checklist(again) and again["ok"] is True
    # Doctor reports the same.
    assert by_id(setup.run_doctor(probe=fake.probe()))["tailscale-installed"]["status"] == "skipped"


def test_setup_install_returns_the_command_and_never_runs_it(mes_home):
    fake = linux(**NO_TAILSCALE, os_release=ARCH)
    result = setup.run_setup({"tailscale": "install"}, probe=fake.probe())
    assert protocol.is_checklist(result) and result["ok"] is False
    it = by_id(result)["tailscale-installed"]
    assert it["fix"] == "sudo pacman -S tailscale && sudo systemctl enable --now tailscaled"
    assert protocol.exit_code_for(result) == ExitCode.ERROR
    assert tomllib.loads(mes_home["config"].read_text())["network"]["mode"] == "tailscale"
    # Rerun without the answer (install not done yet): no second question.
    again = setup.run_setup({}, probe=fake.probe())
    assert protocol.is_checklist(again)
    for call in fake.calls:
        assert call[0] not in ("sudo", "pacman", "brew", "curl", "apt-get", "dnf")


def test_setup_continues_to_start_then_sign_in(mes_home):
    # After the install, the daemon is not running yet: setup gives the start command.
    fake = linux(commands={("tailscale", "status", "--json"): (1, "", "failed to connect to local tailscaled")})
    first = setup.run_setup({"tailscale": "install"}, probe=fake.probe())
    assert by_id(first)["tailscale-running"]["fix"] == "sudo systemctl enable --now tailscaled"
    # Then it is running but signed out: setup asks Tailscale for the link.
    def up(argv):
        return (1, "\nTo authenticate, visit:\n\n\t" + SIGN_IN_URL + "\n\n", "timeout waiting for Running")
    fake2 = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsLogin"), ""), ("tailscale", "up"): up})
    second = setup.run_setup({}, probe=fake2.probe())
    assert second["status"] == "needs_human" and second["id"] == "tailscale-sign-in"
    assert second["action"] == "create or sign in to your Tailscale account"
    assert second["url"] == SIGN_IN_URL
    assert second["rerun"] == "mes setup"
    assert fake2.ran("tailscale", "up", "--timeout=15s")
    assert protocol.exit_code_for(second) == 6


def test_setup_uses_auth_url_from_status_without_running_up():
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsLogin", AuthURL=SIGN_IN_URL), "")})
    result = setup.run_setup({}, probe=fake.probe())
    assert result["url"] == SIGN_IN_URL
    assert not fake.ran("tailscale", "up")


def test_setup_sign_in_link_found_in_status_after_up():
    calls = {"n": 0}

    def st(argv):
        calls["n"] += 1
        return (0, status("NeedsLogin", AuthURL=SIGN_IN_URL if calls["n"] > 1 else ""), "")

    fake = linux(commands={("tailscale", "status", "--json"): st, ("tailscale", "up"): (124, "", "timed out")})
    result = setup.run_setup({}, probe=fake.probe())
    assert result["url"] == SIGN_IN_URL


def test_setup_sign_in_without_link_linux_and_mac():
    denied = (1, "", "Access denied: prefs write access denied")
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsLogin"), ""), ("tailscale", "up"): denied})
    result = setup.run_setup({}, probe=fake.probe())
    assert result["status"] == "needs_human" and result["url"] is None
    assert "sudo tailscale up" in result["why"]
    fake_mac = mac(commands={("tailscale", "status", "--json"): (0, status("NeedsLogin"), ""), ("tailscale", "up"): denied})
    result_mac = setup.run_setup({}, probe=fake_mac.probe())
    assert "menu bar" in result_mac["why"]


def test_setup_machine_needs_approval():
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsMachineAuth"), "")})
    result = setup.run_setup({}, probe=fake.probe())
    assert result["status"] == "needs_human" and result["id"] == "tailscale-approve"
    assert result["url"] == messages.APPROVE_URL


def test_setup_local_only_does_not_ask_to_sign_in():
    fake = linux(commands={("tailscale", "status", "--json"): (0, status("NeedsLogin"), "")})
    result = setup.run_setup({"tailscale": "local-only"}, probe=fake.probe())
    assert protocol.is_checklist(result) and result["ok"] is True
    assert not fake.ran("tailscale", "up")


def test_setup_all_ready_creates_config(mes_home):
    result = setup.run_setup({}, probe=mac().probe())
    assert result["ok"] is True and protocol.exit_code_for(result) == 0
    text = mes_home["config"].read_text()
    assert text.startswith("# Settings for mes")
    assert tomllib.loads(text) == {"network": {"mode": "tailscale"}}
    assert (mes_home["config"].stat().st_mode & 0o777) == 0o600


def test_setup_unsupported_platform_does_not_ask(mes_home):
    fake = Fake(system="Windows", which={"uv": "uv"}, commands={("uv", "--version"): (0, "uv 0.6.6", "")})
    result = setup.run_setup({}, probe=fake.probe())
    assert protocol.is_checklist(result) and by_id(result)["platform"]["status"] == "blocked"
    assert mes_home["config"].exists() and tomllib.loads(mes_home["config"].read_text()) == {}


def test_setup_broken_config_stops(mes_home):
    mes_home["config"].parent.mkdir(parents=True)
    mes_home["config"].write_text("this is = = not toml")
    with pytest.raises(ConfigError) as e:
        setup.run_setup({}, probe=linux().probe())
    assert "damaged" in e.value.message and e.value.hint
    assert mes_home["config"].read_text() == "this is = = not toml"


def test_setup_switch_from_local_only_to_install(mes_home):
    fake = linux(**NO_TAILSCALE)
    setup.run_setup({"tailscale": "local-only"}, probe=fake.probe())
    result = setup.run_setup({"tailscale": "install"}, probe=fake.probe())
    assert by_id(result)["tailscale-installed"]["status"] == "todo"
    assert tomllib.loads(mes_home["config"].read_text())["network"]["mode"] == "tailscale"


# --------------------------------------------------------------------------- answers


def test_parse_answers():
    assert setup.parse_answers(["tailscale=install"]) == {"tailscale": "install"}
    assert setup.parse_answers([" tailscale = local-only "]) == {"tailscale": "local-only"}
    assert setup.parse_answers([]) == {}
    for bad, word in (("tailscale", "form"), ("color=blue", "does not ask"), ("tailscale=maybe", "not one of")):
        with pytest.raises(UsageError) as e:
            setup.parse_answers([bad])
        assert word in e.value.message and e.value.hint


# --------------------------------------------------------------------------- TOML writer


def test_toml_values_and_round_trip():
    assert setup.toml_value(True) == "true" and setup.toml_value(False) == "false"
    assert setup.toml_value(7) == "7"
    tricky = 'a "quote" \\ back\nnew\ttab \x01 ünï'
    text = setup.toml_table("t", {"s": tricky, "b": True, "n": 3})
    assert tomllib.loads(text) == {"t": {"s": tricky, "b": True, "n": 3}}
    with pytest.raises(ValueError):
        setup.toml_value(1.5)
    with pytest.raises(ValueError):
        setup.toml_table("bad name", {})
    with pytest.raises(ValueError):
        setup.toml_table("t", {"bad key": "x"})


def test_set_config_table_keeps_other_settings_and_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "# my notes\n"
        'top = "kept"\n\n'
        "[network]\n"
        'mode = "local-only"\n'
        'extra = "kept too"\n\n'
        "[apps.myapp]\n"
        "days = [1, 2, 3]\n"
    )
    setup.set_config_table("network", {"mode": "tailscale", "extra": "kept too"}, p)
    text = p.read_text()
    assert "# my notes" in text
    assert tomllib.loads(text) == {
        "top": "kept",
        "network": {"mode": "tailscale", "extra": "kept too"},
        "apps": {"myapp": {"days": [1, 2, 3]}},
    }
    assert text.count("[network]") == 1


def test_ensure_config_merges_existing_network_keys(mes_home):
    p = mes_home["config"]
    p.parent.mkdir(parents=True)
    p.write_text('[network]\nmode = "local-only"\nnote = "hello"\n')
    setup._ensure_config(checks.config_state(), "tailscale")
    assert tomllib.loads(p.read_text())["network"] == {"mode": "tailscale", "note": "hello"}


def test_set_config_table_refuses_unsafe_updates(tmp_path):
    p = tmp_path / "config.toml"
    original = 'network.mode = "local-only"\n'
    p.write_text(original)
    with pytest.raises(ConfigError):
        setup.set_config_table("network", {"mode": "tailscale"}, p)
    assert p.read_text() == original
    with pytest.raises(ConfigError):
        setup.set_config_table("network", {"mode": ["not", "flat"]}, p)
    p.write_text("[[network]]\nmode = 'x'\n")
    with pytest.raises(ConfigError):
        setup.set_config_table("network", {"mode": "tailscale"}, p)
    assert not list(tmp_path.glob(".*.tmp"))


# --------------------------------------------------------------------------- through the mes command


@pytest.fixture
def use_fake(monkeypatch):
    def install(fake: Fake) -> Fake:
        probe = fake.probe()  # built with the real Probe class before it is replaced
        monkeypatch.setattr(checks, "Probe", lambda: probe)
        return fake

    return install


def test_cli_setup_question_json(use_fake, capsys):
    use_fake(linux(**NO_TAILSCALE))
    code = cli.run(["setup", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 6 and out["status"] == "needs_input"
    assert out["rerun"] == "mes --json setup --answer tailscale=<value>"


def test_cli_setup_answer_and_text_output(use_fake, capsys):
    use_fake(linux(**NO_TAILSCALE))
    code = cli.run(["setup", "--answer", "tailscale=local-only"])
    out = capsys.readouterr().out
    assert code == 0
    assert "[skipped] Private network (Tailscale) is installed: You chose to keep your apps on this computer only" in out
    assert "What does not work: Your phone and other devices cannot open your apps" in out
    assert "Everything is ready." not in out and "except for the steps marked [skipped]" in out


def test_cli_setup_bad_answer_is_usage_error(use_fake, capsys):
    use_fake(linux())
    code = cli.run(["--json", "setup", "--answer", "tailscale=maybe"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["error"]["code"] == "usage"


def test_cli_doctor_json(use_fake, capsys):
    use_fake(linux(**NO_TAILSCALE))
    code = cli.run(["doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False
    assert [i["id"] for i in out["items"]] == [
        "platform", "python", "uv", "systemd", "systemd-linger",
        "tailscale-installed", "tailscale-running", "tailscale-signed-in", "config",
    ]


def test_cli_lists_setup_and_doctor(capsys):
    assert cli.run(["--json"]) == 0
    names = {c["name"] for c in json.loads(capsys.readouterr().out)["commands"]}
    assert {"setup", "doctor"} <= names



def test_setup_needs_admin_doctor_needs_read(use_fake, capsys, monkeypatch):
    use_fake(linux(**NO_TAILSCALE))
    monkeypatch.setenv("MES_ALLOW", "read")
    assert cli.run(["--json", "setup", "--answer", "tailscale=local-only"]) == 4
    capsys.readouterr()
    from mes import paths
    assert not paths.config_path().exists()
    assert cli.run(["--json", "doctor"]) in (0, 1, 6)
