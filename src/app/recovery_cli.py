import argparse
import json
from pathlib import Path

from app.recovery.service import (
    RecoveryFailed,
    confirm_restricted_access,
    load_manifest,
    locked_recovery_session,
    open_dispatcher,
    recover,
)


def _shared_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)


def _manifest_arguments(parser: argparse.ArgumentParser) -> None:
    _shared_arguments(parser)
    parser.add_argument("--manifest", required=True, type=Path)


def _print(status: str, phase: str, counts: dict[str, int]) -> None:
    print(
        json.dumps(
            {"status": status, "phase": phase, "counts": counts}, ensure_ascii=False
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="在受控部署冻结中收敛并重新开放恢复现场"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    recover_parser = commands.add_parser("recover")
    _manifest_arguments(recover_parser)
    confirm_parser = commands.add_parser("confirm-restricted-access")
    _manifest_arguments(confirm_parser)
    dispatcher_parser = commands.add_parser("open-dispatcher")
    _shared_arguments(dispatcher_parser)
    args = parser.parse_args()

    try:
        with locked_recovery_session() as session:
            if args.command == "recover":
                result = recover(
                    session,
                    load_manifest(args.manifest),
                    args.operator,
                    args.reason,
                )
            elif args.command == "confirm-restricted-access":
                result = confirm_restricted_access(
                    session,
                    load_manifest(args.manifest),
                    args.operator,
                    args.reason,
                )
            else:
                result = open_dispatcher(
                    session, args.recovery_id, args.operator, args.reason
                )
    except RecoveryFailed as error:
        _print("failed", error.phase, error.counts)
        raise SystemExit(1) from None
    except Exception:
        _print("failed", "infrastructure", {})
        raise SystemExit(1) from None
    _print(result.status, result.phase, result.counts)


if __name__ == "__main__":
    main()
