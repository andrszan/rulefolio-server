import argparse
import json

from app.core.database import SessionLocal
from app.project_baseline import (
    BaselineOperationFailed,
    initialize,
    maintenance_lock,
    reset,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="好玩实验室项目基线维护命令")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize_command = commands.add_parser(
        "initialize", help="从空项目范围创建交付基线"
    )
    initialize_command.add_argument("--operator", required=True)
    initialize_command.add_argument("--reason", required=True)

    reset_command = commands.add_parser("reset", help="清空项目范围并恢复交付基线")
    reset_command.add_argument("--operator", required=True)
    reset_command.add_argument("--reason", required=True)
    reset_command.add_argument("--confirm-reset")

    args = parser.parse_args()
    try:
        with maintenance_lock(), SessionLocal() as session:
            result = (
                initialize(session, args.operator, args.reason)
                if args.command == "initialize"
                else reset(session, args.operator, args.reason, args.confirm_reset)
            )
    except BaselineOperationFailed as error:
        print(
            json.dumps(
                {"status": "failed", "phase": error.phase, "counts": error.counts},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            json.dumps({"status": "failed", "phase": "unexpected"}, ensure_ascii=False)
        )
        raise SystemExit(1) from None

    print(json.dumps(result.as_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
