from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from statistics import mean
from typing import Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("async_orchestrator")


#Event Loop Latency Monitor

@dataclass
class LoopLatencyStats:
    samples: List[float] = field(default_factory=list)
    violations: int = 0          # samples > 200ms
    target_ms: float = 200.0

    def record(self, latency_ms: float) -> None:
        self.samples.append(latency_ms)
        if latency_ms > self.target_ms:
            self.violations += 1
            logger.warning(
                "[LoopMonitor] ⚠ Event loop latency %.1f ms — OVER TARGET (>%.0f ms)",
                latency_ms, self.target_ms,
            )
        # Keep only last 1000 samples 
        if len(self.samples) > 1000:
            self.samples = self.samples[-1000:]

    def summary(self) -> dict:
        if not self.samples:
            return {"n": 0, "msg": "No samples yet"}
        return {
            "n":             len(self.samples),
            "mean_ms":       round(mean(self.samples), 2),
            "max_ms":        round(max(self.samples), 2),
            "min_ms":        round(min(self.samples), 2),
            "violations":    self.violations,
            "target_ms":     self.target_ms,
            "target_met":    self.violations == 0,
        }


async def monitor_event_loop_latency(
    stats: LoopLatencyStats,
    interval_s: float = 1.0,
) -> None:
    """
    Probe event loop latency every `interval_s` seconds.

    Strategy: record time before sleep, then measure actual wake-up delay.
    If the loop is blocked (e.g. synchronous call), the measured delay
    will exceed `interval_s`, revealing the blockage.
    """
    logger.info("[LoopMonitor] Started — sampling every %.1f s", interval_s)
    while True:
        t_before = asyncio.get_event_loop().time()
        await asyncio.sleep(interval_s)
        t_after = asyncio.get_event_loop().time()

        # Actual elapsed minus expected = loop overhead (latency)
        latency_ms = (t_after - t_before - interval_s) * 1000
        latency_ms = max(0.0, latency_ms)   # clamp negatives (clock jitter)
        stats.record(latency_ms)


#Task Watchdog

@dataclass
class TaskWatchdog:
    """
    Tracks all node tasks. If a task dies unexpectedly, restarts it.
    """
    _tasks:   Dict[str, asyncio.Task] = field(default_factory=dict)
    _makers:  Dict[str, Callable[[], Coroutine]] = field(default_factory=dict)
    restarts: int = 0

    def register(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine],
    ) -> asyncio.Task:
        """Create, register, and return a task."""
        task = asyncio.create_task(coro_factory(), name=name)
        self._tasks[name] = task
        self._makers[name] = coro_factory
        return task

    async def run_watchdog_loop(self, check_interval_s: float = 10.0) -> None:
        """
        Every `check_interval_s`, scan for dead tasks and restart them.
        Dead = done AND not cancelled (i.e. crashed with an exception).
        """
        logger.info("[Watchdog] Started — checking every %.0f s", check_interval_s)
        while True:
            await asyncio.sleep(check_interval_s)
            dead = [
                name
                for name, t in self._tasks.items()
                if t.done() and not t.cancelled()
            ]
            for name in dead:
                exc = self._tasks[name].exception()
                logger.error(
                    "[Watchdog] Task '%s' died with %s — restarting …",
                    name, exc,
                )
                new_task = asyncio.create_task(
                    self._makers[name](), name=name
                )
                self._tasks[name] = new_task
                self.restarts += 1
                logger.info("[Watchdog] Task '%s' restarted (total restarts: %d)", name, self.restarts)

    def all_tasks(self) -> List[asyncio.Task]:
        return list(self._tasks.values())

    def stats(self) -> dict:
        alive     = sum(1 for t in self._tasks.values() if not t.done())
        dead      = sum(1 for t in self._tasks.values() if t.done() and not t.cancelled())
        cancelled = sum(1 for t in self._tasks.values() if t.cancelled())
        return {
            "total":     len(self._tasks),
            "alive":     alive,
            "dead":      dead,
            "cancelled": cancelled,
            "restarts":  self.restarts,
        }


#Orchestrator

class AsyncOrchestrator:
    """
    Toplevel orchestrator for all 200 campus nodes.

    Usage:
        orch = AsyncOrchestrator(mqtt_nodes, coap_nodes)
        await orch.run()
    """

    def __init__(self, mqtt_nodes: list, coap_nodes: list):
        self.mqtt_nodes   = mqtt_nodes
        self.coap_nodes   = coap_nodes
        self.loop_stats   = LoopLatencyStats()
        self.watchdog     = TaskWatchdog()
        self._start_time: Optional[float] = None

    async def run(self) -> None:
        self._start_time = time.time()
        logger.info(
            "[Orchestrator] Launching %d MQTT + %d CoAP = %d total tasks",
            len(self.mqtt_nodes), len(self.coap_nodes),
            len(self.mqtt_nodes) + len(self.coap_nodes),
        )

        #Register all node tasks with the watchdog
        for node in self.mqtt_nodes:
            self.watchdog.register(node.node_id, node.run)

        for node in self.coap_nodes:
            self.watchdog.register(node.node_id, node.run)

        utility_tasks = [
            asyncio.create_task(
                monitor_event_loop_latency(self.loop_stats),
                name="loop_latency_monitor",
            ),
            asyncio.create_task(
                self.watchdog.run_watchdog_loop(),
                name="task_watchdog",
            ),
            asyncio.create_task(
                self._periodic_report(),
                name="perf_reporter",
            ),
        ]

        all_tasks = self.watchdog.all_tasks() + utility_tasks

        logger.info(
            "[Orchestrator] Total asyncio tasks launched: %d (%d nodes + %d utilities)",
            len(all_tasks), len(self.watchdog.all_tasks()), len(utility_tasks),
        )

        try:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("[Orchestrator] Shutdown signal received.")
        finally:
            self._print_final_report()

    async def _periodic_report(self, interval_s: float = 60.0) -> None:
        """Print a performance snapshot every minute."""
        while True:
            await asyncio.sleep(interval_s)
            uptime = time.time() - (self._start_time or time.time())
            loop_summ = self.loop_stats.summary()
            watch_summ = self.watchdog.stats()
            logger.info(
                "[PerfReport] uptime=%.0fs | loop: mean=%.1fms max=%.1fms violations=%d | "
                "tasks: alive=%d dead=%d restarts=%d",
                uptime,
                loop_summ.get("mean_ms", 0),
                loop_summ.get("max_ms", 0),
                loop_summ.get("violations", 0),
                watch_summ["alive"],
                watch_summ["dead"],
                watch_summ["restarts"],
            )

    def _print_final_report(self) -> None:
        uptime = time.time() - (self._start_time or time.time())
        loop_summ  = self.loop_stats.summary()
        watch_summ = self.watchdog.stats()

        print("\n" + "═" * 65)
        print("  CAMPUS PULSE — ASYNC ORCHESTRATOR FINAL REPORT")
        print("═" * 65)
        print(f"  Uptime:           {uptime:.1f} s ({uptime/60:.1f} min)")
        print(f"  Total tasks:      {watch_summ['total']}")
        print(f"  Tasks alive:      {watch_summ['alive']}")
        print(f"  Task restarts:    {watch_summ['restarts']}")
        print()
        print(f"  Event loop latency samples:  {loop_summ.get('n', 0)}")
        print(f"  Mean latency:     {loop_summ.get('mean_ms', 'N/A')} ms")
        print(f"  Max latency:      {loop_summ.get('max_ms', 'N/A')} ms")
        print(f"  Violations >200ms:{loop_summ.get('violations', 'N/A')}")
        target_str = "✓ MET" if loop_summ.get("target_met", False) else "✗ MISSED"
        print(f"  Target (<200ms):  {target_str}")
        print("═" * 65 + "\n")