import asyncio
import logging
import signal
import sys

from world_engine import run_world_engine

logger = logging.getLogger("main_phase2")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGINT / SIGTERM for clean shutdown."""
    def _handle(sig_name: str) -> None:
        logger.info("Received %s — cancelling all tasks …", sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle, sig.name)
        except NotImplementedError:
            pass


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)

    try:
        loop.run_until_complete(run_world_engine())
    except asyncio.CancelledError:
        logger.info("Phase 2 engine shutdown complete.")
    except Exception as exc:
        logger.critical("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        loop.close()
