import asyncio

from app.config import get_settings
from app.main import Runtime


async def main() -> None:
    settings = get_settings()
    if settings.process_role not in {"run-worker", "scheduler-worker"}:
        raise RuntimeError(
            "run_worker.py requires ASSISTANT_PROCESS_ROLE=run-worker "
            "or scheduler-worker"
        )
    runtime = Runtime(settings)
    await runtime.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runtime.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
