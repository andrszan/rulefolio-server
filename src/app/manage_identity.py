import argparse
import json
from uuid import UUID

from app.core.database import SessionLocal
from app.identity.service import diagnose_outbox, provision_account


def main() -> None:
    parser = argparse.ArgumentParser(description="好玩实验室受控身份运维命令")
    commands = parser.add_subparsers(dest="command", required=True)

    provision = commands.add_parser("provision", help="开通待激活账户")
    provision.add_argument("--email", required=True)
    provision.add_argument("--operator", required=True)
    provision.add_argument("--reason", required=True)

    diagnose = commands.add_parser("outbox", help="读取指定邮件的受控诊断结果")
    diagnose.add_argument("--id", required=True, type=UUID)
    diagnose.add_argument("--operator", required=True)
    diagnose.add_argument("--reason", required=True)

    args = parser.parse_args()
    with SessionLocal() as session:
        if args.command == "provision":
            account_id = provision_account(
                session, args.email, args.operator, args.reason
            )
            print(json.dumps({"accountId": str(account_id)}, ensure_ascii=False))
            return
        print(
            json.dumps(
                diagnose_outbox(session, args.id, args.operator, args.reason),
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
