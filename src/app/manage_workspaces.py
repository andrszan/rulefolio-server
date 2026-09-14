import argparse
import json
from uuid import UUID

from app.core.database import SessionLocal
from app.workspaces.service import diagnose_workspace


def main() -> None:
    parser = argparse.ArgumentParser(description="好玩实验室工作空间受控维护命令")
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    with SessionLocal() as session:
        result = diagnose_workspace(
            session, args.workspace_id, args.operator, args.reason
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
