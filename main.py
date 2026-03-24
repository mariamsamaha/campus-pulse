"""
main.py — Entry point for Campus Pulse IoT simulation.

Delegates to engine.run_engine() after setting up top-level exception handling
and a clean shutdown hook so the SQLite DB is always flushed on exit.
"""

import asyncio
import logging
import signal
import sys

from engine import run_engine

logger = logging.getLogger("main")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGINT / SIGTERM to initiate a clean shutdown."""
    def _handle_signal(sig_name: str) -> None:
        logger.info("Received %s — cancelling all tasks …", sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig.name)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)

    try:
        loop.run_until_complete(run_engine())
    except asyncio.CancelledError:
        logger.info("Engine cancelled — shutdown complete.")
    except Exception as exc:
        logger.critical("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        loop.close()
