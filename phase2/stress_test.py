from __future__ import annotations
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

#Make parent directory importable
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from world_engine import build_mqtt_fleet, build_coap_fleet
from dedup import DedupHandler
from async_orchestrator import AsyncOrchestrator, LoopLatencyStats
from performance_monitor import PerformanceMonitor
from latency_tracker import LatencyTracker

logger = logging.getLogger("stress_test")

TEST_DURATION_S = 30   
REPORT_PATH     = _HERE / "stress_test_results.json"


async def run_stress_test() -> None:
    start = time.time()
    logger.info("=" * 60)
    logger.info("  CAMPUS PULSE PHASE 2 — STRESS TEST")
    logger.info("  Duration: %d minutes", TEST_DURATION_S // 60)
    logger.info("  Nodes: 200 (100 MQTT + 100 CoAP)")
    logger.info("=" * 60)

    dedup      = DedupHandler()
    perf_mon   = PerformanceMonitor(sample_interval_s=10.0)
    loop_stats = LoopLatencyStats()

    mqtt_nodes = build_mqtt_fleet(sim_start=start, dedup=dedup)
    coap_nodes = build_coap_fleet(sim_start=start, dedup=dedup)

    orchestrator = AsyncOrchestrator(mqtt_nodes, coap_nodes)
    orchestrator.loop_stats = loop_stats

    async def _timeout_killer():
        """Cancel everything after TEST_DURATION_S."""
        await asyncio.sleep(TEST_DURATION_S)
        logger.info("[Stress] Time limit reached — stopping all tasks …")
        for task in asyncio.all_tasks():
            if task.get_name() != "timeout_killer":
                task.cancel()

    logger.info("[Stress] Launching all tasks …")

    perf_task    = asyncio.create_task(perf_mon.run(), name="perf_monitor")
    timeout_task = asyncio.create_task(_timeout_killer(), name="timeout_killer")

    try:
        await asyncio.gather(
            orchestrator.run(),
            perf_task,
            timeout_task,
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        pass

    #Collect final stats
    elapsed = time.time() - start
    dedup_stats  = dedup.stats()
    loop_summary = loop_stats.summary()
    perf_summary = perf_mon.report.summary()
    watch_summary = orchestrator.watchdog.stats()

    results = {
        "test_meta": {
            "duration_s":    round(elapsed, 1),
            "target_nodes":  200,
            "mqtt_nodes":    len(mqtt_nodes),
            "coap_nodes":    len(coap_nodes),
        },
        "event_loop_latency": loop_summary,
        "performance":        perf_summary,
        "deduplication":      dedup_stats,
        "task_health":        watch_summary,
        "targets": {
            "loop_latency_under_200ms": loop_summary.get("target_met", False),
            "zero_restarts":            watch_summary["restarts"] == 0,
            "low_dup_rate":             dedup_stats["dup_rate_pct"] < 1.0,
        },
    }

    # Save results
    with open(REPORT_PATH, "w") as f:
        json.dump(results, f, indent=2)

    # Print final summary
    print("\n" + "═" * 65)
    print("  STRESS TEST COMPLETE")
    print("═" * 65)
    print(f"  Duration:            {elapsed/60:.1f} min")
    print(f"  MQTT nodes:          {len(mqtt_nodes)}")
    print(f"  CoAP nodes:          {len(coap_nodes)}")
    print()
    print(f"  Loop latency mean:   {loop_summary.get('mean_ms', 'N/A')} ms")
    print(f"  Loop latency max:    {loop_summary.get('max_ms', 'N/A')} ms")
    print(f"  Loop violations:     {loop_summary.get('violations', 'N/A')} (target: 0)")
    print()
    print(f"  CPU mean:            {perf_summary.get('cpu_mean_pct', 'N/A')} %")
    print(f"  CPU max:             {perf_summary.get('cpu_max_pct', 'N/A')} %")
    print(f"  Memory mean:         {perf_summary.get('mem_mean_mb', 'N/A')} MB")
    print()
    print(f"  DUP rate:            {dedup_stats['dup_rate_pct']} %")
    print(f"  MQTT duplicates:     {dedup_stats['mqtt_dup']} dropped")
    print(f"  CoAP duplicates:     {dedup_stats['coap_dup']} dropped")
    print()
    print(f"  Task restarts:       {watch_summary['restarts']}")
    print()
    print("  TARGETS:")
    print(f"    Loop < 200ms:      {'✓ PASS' if results['targets']['loop_latency_under_200ms'] else '✗ FAIL'}")
    print(f"    Zero restarts:     {'✓ PASS' if results['targets']['zero_restarts'] else '✗ FAIL'}")
    print(f"    Low dup rate:      {'✓ PASS' if results['targets']['low_dup_rate'] else '✗ FAIL'}")
    print()
    print(f"  Full report saved → {REPORT_PATH}")
    print("═" * 65 + "\n")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal(sig_name):
        logger.info("Signal %s received — stopping …", sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig.name)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(run_stress_test())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        print("Stress test runner exited cleanly.")