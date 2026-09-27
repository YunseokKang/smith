"""Bootstrap CLI. No network, LLM or email operations are implemented."""
import argparse
from pathlib import Path
import tomllib
from smith.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smith private banker scaffold")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config", help="Validate configuration without external side effects")
    check.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        # Do not echo file contents: a local config may contain private fields.
        print("Configuration invalid or unreadable. Check the documented example.")
        return 2
    reports = config["reports"]
    print("Configuration valid. Financial access: read_only.")
    print(f"Report schedule: {', '.join(reports['weekdays'])} {reports['time']} ({config['app']['timezone']})")
    print("Scaffold only: no schedule registered and no email sent.")
    return 0
