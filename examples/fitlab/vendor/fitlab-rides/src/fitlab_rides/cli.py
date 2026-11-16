"""fitlab-rides: rides you type in, kept as rows of the project's shared tables (an example app)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated

from mes import App, UsageError

app = App("fitlab-rides")

LOG = "rides.jsonl"  # in this app's own folder: <data folder>/rides/


def _rides(ctx) -> list[dict]:
    path = ctx.app_data_dir / LOG
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@app.command("add", kind="write")
def add(ctx, day: Annotated[dt.date, "the day of the ride, YYYY-MM-DD"],
        km: Annotated[float, "how far, in kilometres"],
        minutes: Annotated[float, "how long, in minutes"],
        climb: Annotated[float, "metres climbed"] = 0.0,
        weather: Annotated[str, "the weather, in a word or two"] = ""):
    """Save one ride. The dashboards show it after the next rebuild."""
    if km <= 0 or minutes <= 0 or climb < 0:
        raise UsageError(
            f"A ride needs a distance and a time above zero and a climb of zero or more, but this one had "
            f"{km} km in {minutes} minutes with {climb} m of climbing, so nothing was saved.",
            hint="For example: fitlab rides add 2026-01-05 21.5 60 --climb 240 --weather cloudy",
        )
    ride = {"day": day.isoformat(), "km": km, "minutes": minutes, "climb": climb, "weather": weather.strip()[:40]}
    with (ctx.app_data_dir / LOG).open("a", encoding="utf-8") as f:
        f.write(json.dumps(ride) + "\n")
    return {"saved": True, **ride}


@app.command("list", kind="read")
def list_rides(ctx):
    """The saved rides, newest first."""
    return sorted(_rides(ctx), key=lambda r: r["day"], reverse=True)


@app.command("rebuild", kind="write")
def rebuild(ctx):
    """Rebuild this app's rows of the shared tables from the saved rides."""
    rides = _rides(ctx)
    activities = [{"id": f"ride-{n}", "date": r["day"], "type": "cycling", "distance_m": r["km"] * 1000,
                   "duration_s": r["minutes"] * 60, "ascent_m": r["climb"]}
                  for n, r in enumerate(rides, 1)]
    days: dict[str, dict] = {}
    for r in rides:
        d = days.setdefault(r["day"], {"date": r["day"], "ride_km": 0.0, "weather": None})
        d["ride_km"] = round(d["ride_km"] + r["km"], 3)
        d["weather"] = r["weather"] or d["weather"]
    with ctx.db() as db:
        db.replace_rows("activities", activities)
        db.replace_rows("days", list(days.values()))
    return {"rides": len(rides), "days": len(days)}


def main() -> None:
    app.main()


if __name__ == "__main__":
    main()
