"""CLI entrypoint for CPAPriorityKeeper."""
import argparse

from .maintainer import CPAPriorityKeeper
from .settings import SettingsError, load_settings


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="CPAPriorityKeeper",
        description="Adjust CPA credential priority/disabled state from live probes + usage history.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and print changes without applying them.")
    parser.add_argument("--daemon", action="store_true", default=True,
                        help="Run forever (default).")
    parser.add_argument("--once", dest="daemon", action="store_false",
                        help="Run a single round, then exit.")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        settings = load_settings()
    except SettingsError as exc:
        parser.exit(status=2, message=f"Configuration error: {exc}\n")

    keeper = CPAPriorityKeeper(settings=settings, dry_run=args.dry_run)
    if args.daemon:
        keeper.run_forever(interval_seconds=settings.interval_seconds)
        return 0
    keeper.run_once()
    return 0
