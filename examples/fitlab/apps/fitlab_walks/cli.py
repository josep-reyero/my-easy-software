"""fitlab-walks: walks you type in, kept as rows of the project's shared tables (an example app)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated

from mes import App, UsageError

app = App("fitlab-walks")

LOG = "walks.jsonl"  # in this app's own folder: <data folder>/walks/


def _walks(ctx) -> list[dict]:
    path = ctx.app_data_dir / LOG
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@app.command("add", kind="write")
def add(ctx, day: Annotated[dt.date, "the day of the walk, YYYY-MM-DD"],
        km: Annotated[float, "how far, in kilometres"],
        minutes: Annotated[float, "how long, in minutes"],
        weather: Annotated[str, "the weather, in a word or two"] = ""):
    """Save one walk. The dashboards show it after the next rebuild."""
    if km <= 0 or minutes <= 0:
        raise UsageError(
            f"A walk needs a distance and a time above zero, but this one had {km} km in {minutes} minutes, "
            "so nothing was saved.",
            hint="For example: fitlab walks add 2026-01-05 4.2 50 --weather sunny",
        )
    walk = {"day": day.isoformat(), "km": km, "minutes": minutes, "weather": weather.strip()[:40]}
    with (ctx.app_data_dir / LOG).open("a", encoding="utf-8") as f:
        f.write(json.dumps(walk) + "\n")
    return {"saved": True, **walk}


@app.command("list", kind="read")
def list_walks(ctx):
    """The saved walks, newest first."""
    return sorted(_walks(ctx), key=lambda w: w["day"], reverse=True)


@app.command("rebuild", kind="write")
def rebuild(ctx):
    """Rebuild this app's rows of the shared tables, and its walks_log table, from the saved walks."""
    walks = _walks(ctx)
    activities = [{"id": f"walk-{n}", "date": w["day"], "type": "walking",
                   "distance_m": w["km"] * 1000, "duration_s": w["minutes"] * 60}
                  for n, w in enumerate(walks, 1)]
    days: dict[str, dict] = {}
    for w in walks:
        d = days.setdefault(w["day"], {"date": w["day"], "walk_km": 0.0, "weather": None})
        d["walk_km"] = round(d["walk_km"] + w["km"], 3)
        d["weather"] = w["weather"] or d["weather"]
    with ctx.db() as db:  # one transaction: readers see the old rows or the new ones, never half
        db.replace_rows("activities", activities)
        db.replace_rows("days", list(days.values()))
        db.conn.execute('CREATE TABLE IF NOT EXISTS "walks_log" '
                        "(line INTEGER PRIMARY KEY, date TEXT, km REAL, minutes REAL, weather TEXT)")
        db.conn.execute('DELETE FROM "walks_log"')
        db.conn.executemany('INSERT INTO "walks_log" VALUES (?, ?, ?, ?, ?)',
                            [(n, w["day"], w["km"], w["minutes"], w["weather"] or None)
                             for n, w in enumerate(walks, 1)])
    return {"walks": len(walks), "days": len(days)}


def main() -> None:
    app.main()


if __name__ == "__main__":
    main()
