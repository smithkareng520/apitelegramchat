"""CLI entry point for explicit Telegram webhook registration."""

from __future__ import annotations

import argparse
import asyncio

from apitelegramchat.app import set_webhook
from apitelegramchat.config import validate_runtime_config


def main() -> None:
    parser = argparse.ArgumentParser(description="注册 Telegram webhook")
    parser.add_argument(
        "--drop-pending-updates",
        action="store_true",
        help="明确丢弃 Telegram 中尚未投递的历史 update",
    )
    args = parser.parse_args()
    validate_runtime_config(strict=True)
    asyncio.run(set_webhook(drop_pending_updates=args.drop_pending_updates))


if __name__ == "__main__":
    main()
