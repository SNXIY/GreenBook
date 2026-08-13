from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

from app.core.config import get_settings
from app.creator.infrastructure.database import CreatorDatabase
from app.creator.worker.composition import open_creator_worker_runtime
from app.creator.worker.operations import replay_dead_outbox

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MindFlow Creator runtime worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="consume Creator Outbox commands")
    subparsers.add_parser(
        "healthcheck",
        help="verify the worker heartbeat file is fresh",
    )
    replay = subparsers.add_parser(
        "replay-dead",
        help="create an auditable replay command from one DEAD message",
    )
    replay.add_argument("--message-id", required=True)
    replay.add_argument("--operator", required=True)
    replay.add_argument("--reason", required=True)
    return parser


async def run_worker() -> int:
    settings = get_settings()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    async with open_creator_worker_runtime(settings) as runtime:
        logger.info(
            "Creator worker started worker_id=%s",
            runtime.worker.worker_id,
        )
        await runtime.worker.run_forever(stop_event)
        logger.info(
            "Creator worker stopped worker_id=%s",
            runtime.worker.worker_id,
        )
    return 0


def healthcheck() -> int:
    settings = get_settings()
    path = Path(settings.creator_worker_health_file)
    try:
        age_seconds = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 1
    return int(age_seconds > settings.creator_worker_health_max_age_seconds)


async def replay_dead_message(
    *,
    message_id: str,
    operator_id: str,
    reason: str,
) -> int:
    settings = get_settings()
    database = CreatorDatabase.from_settings(settings)
    try:
        result = await replay_dead_outbox(
            uow_factory=database.uow_factory,
            message_id=message_id,
            operator_id=operator_id,
            reason=reason,
        )
    finally:
        await database.dispose()
    print(
        json.dumps(
            {
                "source_message_id": result.source_message_id,
                "replay_message_id": result.replay_message_id,
                "replayed": result.replayed,
            },
            sort_keys=True,
        )
    )
    return 0


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def stop() -> None:
        stop_event.set()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop)
        except NotImplementedError:
            signal.signal(signal_number, lambda *_: stop())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format=("%(asctime)s %(levelname)s %(name)s " "%(message)s"),
    )
    args = build_parser().parse_args(argv)
    if args.command == "healthcheck":
        return healthcheck()
    if args.command == "replay-dead":
        return asyncio.run(
            replay_dead_message(
                message_id=args.message_id,
                operator_id=args.operator,
                reason=args.reason,
            )
        )
    try:
        return asyncio.run(run_worker())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
