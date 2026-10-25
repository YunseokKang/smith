"""`smith weather`: today's official weather, warnings, air quality and pollen with the rule-based advice (FR-31 ⓪)."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import credentials, weather
from smith.config import load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")


def add_parser(sub: Any) -> None:
    command = sub.add_parser("weather", help="Today's weather block inputs from official sources (daily mail)")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    command.add_argument("--json", action="store_true", help="print the model payload instead of the summary")


def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    daily = config.get("daily", {})
    if "weather" not in daily:
        print("Missing setup: add [daily.weather] (lat, lon, air_station, air_region, pollen_area_no, warning_keywords).")
        return 2
    key = credentials.load_api_key("datagokr")
    if key is None:
        print("Missing setup: no data.go.kr key stored (smith realestate login).")
        return 2
    location = weather.Location(**{**daily["weather"], "warning_keywords": tuple(daily["weather"]["warning_keywords"])})
    now = local_time(datetime.now(timezone.utc), config["app"]["timezone"])
    report = weather.fetch(key, location, now=now)
    advice = weather.advise(report, airways_sensitive=bool(daily.get("airways_sensitive", False)))
    if args.json:
        print(json.dumps(weather.for_model(report, advice), ensure_ascii=False, indent=2))
        return 0
    print(f"{now:%Y-%m-%d %H:%M} · grid {location.grid} · station {location.air_station}")
    print(f"headline: {advice.headline}{' (severe)' if advice.severe else ''}")
    print(f"items: {', '.join(advice.items) or '없음'} · cautions: {', '.join(advice.cautions) or '없음'}")
    print(f"air: {advice.air_line}")
    for label, forecast in (("today", report.today), ("tomorrow", report.tomorrow)):
        if forecast:
            print(f"{label}: {forecast.tmin}~{forecast.tmax}℃ · pop {forecast.pop_max}% · rain hours {list(forecast.rain_hours)} "
                  f"· {'/'.join(forecast.precip) or '강수 없음'} · {forecast.sky} · wind {forecast.wind_max} m/s · run {forecast.issued}")
    if report.now:
        print(f"now ({report.now.at}): {report.now.temp}℃ · humidity {report.now.humidity}% · {report.now.precip or '강수 없음'}")
    relevant = [w for w in report.warnings if w.relevant]
    print(f"warnings: {len(report.warnings)} nationwide, {len(relevant)} relevant" +
          (": " + "; ".join(f"{w.kind}{w.level} ({w.areas[:40]})" for w in relevant) if relevant else ""))
    for pollen in report.pollen:
        print(f"pollen {pollen.kind}: " + (f"today {pollen.today} tomorrow {pollen.tomorrow}" if pollen.in_season else "out of season"))
    if report.failures:
        print(f"failures: {report.failures}")
    return 0 if not report.failures else 1
