import argparse

from app.core.database import SessionLocal
from app.identity.service import (
    process_next_recovery_request,
    recover_stale_recovery_request_jobs,
)
from app.notifications.dispatcher import dispatch_one, recover_stale_dispatches


def main() -> None:
    parser = argparse.ArgumentParser(description="处理好玩实验室账户恢复与待发送邮件")
    parser.add_argument(
        "--once", action="store_true", help="只处理一项等待中的后台工作"
    )
    args = parser.parse_args()

    with SessionLocal() as session:
        recover_stale_recovery_request_jobs(session)
        recover_stale_dispatches(session)
        if args.once:
            result = process_next_recovery_request(session) or dispatch_one(session)
            print(result or "empty")
            return
        while process_next_recovery_request(session) is not None:
            pass
        while dispatch_one(session) is not None:
            pass


if __name__ == "__main__":
    main()
