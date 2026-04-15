import asyncio
import logging
import signal
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import time
from world_engine import build_mqtt_fleet, build_coap_fleet
from dedup import DedupHandler
from async_orchestrator import AsyncOrchestrator
from performance_monitor import PerformanceMonitor

logger = logging.getLogger("main_phase2")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _handle(sig_name: str) -> None:
        logger.info("Received %s — cancelling all tasks …", sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle, sig.name)
        except NotImplementedError:
            pass


async def run() -> None:
    sim_start  = time.time()
    dedup      = DedupHandler()
    perf_mon   = PerformanceMonitor(sample_interval_s=10.0)

    mqtt_nodes = build_mqtt_fleet(sim_start=sim_start, dedup=dedup)
    coap_nodes = build_coap_fleet(sim_start=sim_start, dedup=dedup)

    orchestrator = AsyncOrchestrator(mqtt_nodes, coap_nodes)

    perf_task = asyncio.create_task(perf_mon.run(), name="perf_monitor")

    try:
        await asyncio.gather(
            orchestrator.run(),
            perf_task,
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        logger.info("Phase 2 engine shutdown complete.")
    finally:
        perf_mon.report.print_table()
        perf_mon.report.to_json(str(_HERE / "perf_log.json"))


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
        loop.run_until_complete(run())
    except asyncio.CancelledError:
        logger.info("Phase 2 engine exited cleanly.")
    except Exception as exc:
        logger.critical("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        loop.close()