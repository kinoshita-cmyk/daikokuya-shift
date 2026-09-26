"""GitHub Actions entrypoint. Uses only private backups, never local requests."""
from __future__ import annotations

import argparse
import os
from datetime import datetime

from .paid_leave_sync import (
    AttendanceSender, GitHubSyncRepository, JST, SyncError, month_key, sync_month,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default="", help="YYYY-MM; empty uses the current JST month")
    parser.add_argument("--manual", action="store_true", help="Allow recovery of an earlier month, never overwrite a receipt")
    args = parser.parse_args(argv)
    if os.environ.get("PAID_LEAVE_SYNC_ENABLED", "").lower() != "true":
        print("有給連携は未有効化です。接続先の受入テスト後に有効化してください。", flush=True)
        return 0
    try:
        now = datetime.now(JST)
        month = month_key(args.month or now.strftime("%Y-%m"))
        if not args.manual:
            start = month_key(os.environ.get("PAID_LEAVE_SYNC_START_MONTH", ""))
            if month < start:
                print(f"{month}: 自動連携の開始月前のため送信しません。", flush=True)
                return 0
        repo = GitHubSyncRepository(
            os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_BACKUP_REPO", ""),
            os.environ.get("GITHUB_BACKUP_BRANCH", "main"),
        )
        sender = AttendanceSender(
            os.environ.get("ATTENDANCE_PAID_LEAVE_SYNC_URL", ""),
            os.environ.get("ATTENDANCE_PAID_LEAVE_SYNC_TOKEN", ""),
        )
        result = sync_month(repo, sender, month, now, manual=args.manual)
        # Names, counts per employee, URLs and credentials never go to Actions logs.
        print(f"{month}: {result.status}: {result.message}", flush=True)
        return 1 if result.status == "failed" else 0
    except SyncError as exc:
        print(f"有給連携は完了していません: {exc}", flush=True)
        return 1
    except Exception:
        print("有給連携に予期しないエラーが発生しました。連携状態を再確認してください。", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
