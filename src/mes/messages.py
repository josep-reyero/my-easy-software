"""Plain-language text for ``mes setup`` and ``mes doctor``.

Every title, explanation and question a person can see during setup lives
here, so the wording can be reviewed in one place. Each text says what is
happening, why it matters to the person and what happens next, without
assuming they are a developer.

Public API
----------
``TAILSCALE_OFFER``
    The "Keeping your apps private" explanation from ``docs/design.md``, word for
    word (``TAILSCALE_OFFER_TITLE`` + ``TAILSCALE_OFFER_BODY``). It is the
    question of the ``needs_input`` that ``mes setup`` returns when Tailscale
    is missing.
``TAILSCALE_OPTIONS``
    ``{value: (label, meaning)}`` for the choices ``install`` (recommended) and
    ``local-only``.
``LOCAL_ONLY_WORKS`` / ``LOCAL_ONLY_DOES_NOT_WORK``
    What will and will not work when the person keeps apps on this computer only.
``SIGN_IN_ACTION``
    The ``needs_human`` action for the Tailscale sign-in step.
``T``
    ``{check_id: {"title": ..., "why_done": ..., ...}}``: titles and explanations
    for each checklist item, used by ``mes.checks``. ``T["tailscale-serve"]["why_todo"]`` has a
    ``{project}`` placeholder.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- Tailscale offer

TAILSCALE_OFFER_TITLE = "Keeping your apps private."
TAILSCALE_OFFER_BODY = (
    "Your apps should only open on your own devices. Tailscale is "
    "what makes that possible: it links your computer, phone and server into a private network "
    "that only you can enter. Without it, nobody else can reach your apps, and neither can your "
    "phone. It is free for personal use and takes about two minutes. I can install it for you; "
    "you only need to create an account. Install it now?"
)
TAILSCALE_OFFER = f"{TAILSCALE_OFFER_TITLE} {TAILSCALE_OFFER_BODY}"

TAILSCALE_OFFER_WHY = (
    "Tailscale is not on this computer yet. It is what lets your phone and your own server reach "
    "your apps while keeping everyone else out. Without it, your apps open only on this computer."
)

LOCAL_ONLY_WORKS = (
    "Your apps, their commands, their jobs run by hand and their data all work on this computer."
)
LOCAL_ONLY_DOES_NOT_WORK = (
    "Your phone and other devices cannot open your apps, and apps cannot be moved to a server of "
    "your own."
)
CHANGE_MIND = "You can change your mind at any time with: mes setup --answer tailscale=install"

TAILSCALE_OPTIONS: dict[str, tuple[str, str]] = {
    "install": (
        "Install Tailscale",
        "Tailscale is installed from an official package source (on a Mac: Homebrew, or the app "
        "from tailscale.com) and started. You only create or sign in "
        "to your free account. Your apps then open on your own computer, phone and server, and "
        "nowhere else.",
    ),
    "local-only": (
        "Not now: keep apps on this computer only",
        "Nothing is installed. " + LOCAL_ONLY_WORKS + " " + LOCAL_ONLY_DOES_NOT_WORK + " "
        + CHANGE_MIND,
    ),
}

LOCAL_ONLY_SKIPPED_WHY = (
    "You chose to keep your apps on this computer only, so the private network is not set up. "
    f"What works: {LOCAL_ONLY_WORKS} What does not work: {LOCAL_ONLY_DOES_NOT_WORK} {CHANGE_MIND}"
)
LOCAL_ONLY_NOT_NEEDED = "Not needed, because you chose to keep your apps on this computer only."

SIGN_IN_ACTION = "create or sign in to your Tailscale account"
SIGN_IN_WHY = (
    "Tailscale is running, but this computer is not signed in to your account yet. Signing in is "
    "what joins this computer to your private network, so your other devices can reach it. Only "
    "you can do this: it needs your own account and password."
)
SIGN_IN_WHY_LINK = " Open the link, sign in (or create a free account), then come back."
SIGN_IN_WHY_NO_LINK_MAC = (
    " Click the Tailscale icon in the menu bar at the top of the screen and choose Log in, then "
    "come back."
)
SIGN_IN_WHY_NO_LINK_LINUX = (
    " To get the sign-in link, the agent can run: sudo tailscale up (it prints a link to open)."
)

APPROVE_ACTION = "approve this computer in your Tailscale admin page"
APPROVE_WHY = (
    "This computer is signed in, but your Tailscale network asks an administrator (you) to "
    "approve new devices before they can join. Until then your other devices cannot reach it."
)
APPROVE_URL = "https://login.tailscale.com/admin/machines"

# --------------------------------------------------------------------------- checklist texts

T: dict[str, dict[str, str]] = {
    "platform": {
        "title": "This computer's system is supported",
        "why_done": "mes works on macOS and Linux.",
        "why_mac": (
            "This is a Mac: good for building and trying apps. Scheduled jobs run only on a Linux "
            "server, and that is fine: on this Mac you run jobs by hand with mes jobs run."
        ),
        "why_other": (
            "mes works only on macOS and Linux, and this computer runs something else, so apps "
            "cannot be set up here. Use a Mac or a Linux computer or server instead."
        ),
    },
    "python": {
        "title": "Python is recent enough",
        "why_done": "Apps are written in Python, and this version can run them.",
        "why_todo": (
            "Apps are written in Python, and this version is too old to run them. uv can add a "
            "newer Python just for mes and its apps, without changing the rest of the computer."
        ),
    },
    "uv": {
        "title": "uv is installed (it installs and runs the apps)",
        "why_done": "uv installs each app with exactly the parts it needs and runs it.",
        "why_todo": (
            "uv is the tool that installs each app with exactly the parts it needs and runs it. "
            "Without it, apps cannot be installed or started."
        ),
        "why_broken": (
            "uv is installed but did not answer when asked for its version, so it may be damaged. "
            "Installing it again from its official source usually fixes this."
        ),
    },
    "systemd": {
        "title": "Jobs can be started automatically",
        "why_done": "systemd, the part of Linux that starts programs on a schedule, is ready.",
        "why_mac": (
            "Scheduled jobs run only on a Linux server, and that is fine: on this Mac you can "
            "still run any job by hand with mes jobs run."
        ),
        "why_missing": (
            "This Linux system does not use systemd, the part of Linux that mes uses to start jobs "
            "on a schedule, so scheduled jobs cannot run here. Jobs run by hand still work."
        ),
        "why_not_running": (
            "systemd, the part of Linux that starts jobs on a schedule, is not running normally on "
            "this computer (for example inside some containers), so scheduled jobs cannot run here. "
            "Jobs run by hand still work."
        ),
    },
    "tailscale-installed": {
        "title": "Private network (Tailscale) is installed",
        "why_done": "Tailscale links your own devices into a private network that only you can enter.",
        "why_todo": (
            "Tailscale is not installed. It links your computer, phone and server into a private "
            "network that only you can enter. Without it, your apps open only on this computer."
        ),
        "why_manual_mac": (
            "Tailscale is not installed, and this Mac has no Homebrew to install it automatically. "
            "Download the Tailscale app from https://tailscale.com/download/mac and open it."
        ),
        "why_unsupported": "Not checked, because mes does not support this system.",
    },
    "tailscale-running": {
        "title": "Private network (Tailscale) is running",
        "why_done": "Tailscale is running on this computer.",
        "why_todo": (
            "Tailscale is installed but not running, so your other devices cannot reach this "
            "computer. Starting it fixes this."
        ),
        "why_todo_mac": (
            "Tailscale is installed but not running, so your other devices cannot reach this "
            "computer. Starting the Tailscale app fixes this. The first time, your Mac asks to "
            "allow a VPN configuration: click Allow (it is what keeps the network private)."
        ),
        "why_stopped": (
            "Tailscale is installed and signed in, but switched off, so your other devices cannot "
            "reach this computer. Connecting it again fixes this."
        ),
        "why_starting": "Tailscale is still starting. Run the check again in a moment.",
        "why_blocked": "This waits until Tailscale is installed (the item above).",
    },
    "tailscale-signed-in": {
        "title": "This computer is signed in to your private network",
        "why_done": "This computer is part of your private network.",
        "why_todo": SIGN_IN_WHY,
        "why_approve": APPROVE_WHY,
        "why_blocked": "This waits until Tailscale is running (the item above).",
    },
    "tailscale-serve": {
        "title": "Serve the dashboard on your private network",
        "why_todo": (
            "The dashboard of {project} runs on the server behind a private local socket, so for now "
            "no device can open it. One Tailscale command on that server makes it open on your own "
            "devices (computer, phone) through your private network, and nowhere else: nobody "
            "outside your private network can reach it. mes never runs these commands itself: you "
            "(or your assistant, after you say yes) run them on the server, where they need sudo."
        ),
        "why_blocked": (
            "This waits until Tailscale is installed, running and signed in on this computer (the "
            "items above)."
        ),
        "why_local_only": (
            "You chose to keep your apps on this computer only, so the dashboard is not served on a "
            "private network. Open it on this computer with: mes dev"
        ),
        "why_unsupported": "Serving a dashboard needs Linux or macOS.",
        "never_funnel": (
            "Never use `tailscale funnel` for this: funnel would put the dashboard on the public "
            "internet for anyone to find."
        ),
    },
    "config": {
        "title": "Settings file for this computer exists",
        "why_done": "mes keeps its settings for this computer in this file.",
        "why_todo": (
            "mes keeps a few settings for this computer (for example whether apps stay on this "
            "computer only). The file does not exist yet; mes setup creates it."
        ),
        "why_broken": (
            "The settings file for this computer is damaged, so mes cannot read its settings. The "
            "agent can repair the line named in the detail, or move the file aside and run mes "
            "setup to start fresh."
        ),
    },
}
