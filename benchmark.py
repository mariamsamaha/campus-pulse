"""
benchmark.py — Campus Pulse IoT Simulation Benchmark
=====================================================

Validates the system against the required benchmarks:
  • 200 rooms simulated concurrently
  • 5-second publish interval
  • Event-loop latency < 200 ms  (p95 and p99 reported)
  • Continuous operation for 30 minutes
  • Live CPU and memory usage reporting (sampled every 30 s)

Run:
    python benchmark.py              # full 30-minute run
    python benchmark.py --duration 60  # quick smoke-test (60 seconds)
    python benchmark.py --duration 300 --rooms 200  # custom

No real MQTT broker required — publishes are intercepted by a mock so the
benchmark measures pure simulation + persistence performance, not network I/O.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import sqlite3
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

# ─── graceful import of psutil ────────────────────────────────────────────────
try:
    import psutil
    _HAVE_PSUTIL = True
except ImportError:
    _HAVE_PSUTIL = False

# ─── project imports ──────────────────────────────────────────────────────────
from config import cfg, AppConfig, SimulationConfig, ThermalConfig, DatabaseConfig, FaultsConfig, MQTTConfig
from db import RoomDatabase
from faults import FaultEngine, FaultState
from room import Room

# ─────────────────────────────────────────────────────────────────────────────
# Benchmark configuration
# ─────────────────────────────────────────────────────────────────────────────

LATENCY_WARN_MS   = 200.0   # p95 must be below this (spec requirement)
LATENCY_BUDGET_MS = 200.0   # same threshold used for pass/fail
SAMPLE_INTERVAL_S = 30      # how often to print live CPU/memory stats
LATENCY_WINDOW    = 2000    # keep last N latency samples for percentile calc

logging.basicConfig(
    level=logging.WARNING,          # suppress per-room noise during benchmark
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("benchmark")

# ─────────────────────────────────────────────────────────────────────────────
# Metrics collector (shared across all coroutines)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Metrics:
    start_time:       float = field(default_factory=time.time)
    tick_count:       int   = 0        # total ticks across all rooms
    publish_count:    int   = 0        # mock-publishes succeeded
    suppressed_count: int   = 0        # node_dropout suppressions
    fault_count:      int   = 0        # total fault activations
    error_count:      int   = 0        # tick exceptions
    db_flush_count:   int   = 0        # DB sync cycles
    db_rows_written:  int   = 0        # cumulative rows persisted

    latencies_ms: Deque[float] = field(
        default_factory=lambda: deque(maxlen=LATENCY_WINDOW)
    )

    # per-sample snapshots for final chart
    cpu_samples:    List[float] = field(default_factory=list)
    mem_mb_samples: List[float] = field(default_factory=list)
    sample_times:   List[float] = field(default_factory=list)

    def record_latency(self, ms: float) -> None:
        self.latencies_ms.append(ms)

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_lats = sorted(self.latencies_ms)
        idx = int(math.ceil(p / 100.0 * len(sorted_lats))) - 1
        return sorted_lats[max(0, idx)]

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def ticks_per_second(self) -> float:
        e = self.elapsed
        return self.tick_count / e if e > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Mock MQTT publisher (no broker needed)
# ─────────────────────────────────────────────────────────────────────────────

class MockPublisher:
    """Drop-in for MQTTManager. Counts publishes, adds no I/O latency."""

    def __init__(self, metrics: Metrics) -> None:
        self._m = metrics

    async def publish(self, topic: str, payload: dict, qos: int = 1) -> None:
        self._m.publish_count += 1

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Latency probe (event-loop sentinel)
# ─────────────────────────────────────────────────────────────────────────────

async def latency_probe(metrics: Metrics, stop_event: asyncio.Event) -> None:
    """
    Continuously measures event-loop scheduling latency.

    Schedules a 10 ms sleep and measures actual elapsed time.
    Difference = latency introduced by event-loop backpressure.
    """
    PROBE_INTERVAL = 0.010   # 10 ms nominal
    while not stop_event.is_set():
        t0 = asyncio.get_event_loop().time()
        await asyncio.sleep(PROBE_INTERVAL)
        actual = (asyncio.get_event_loop().time() - t0) * 1000  # ms
        latency = max(0.0, actual - PROBE_INTERVAL * 1000)
        metrics.record_latency(latency)


# ─────────────────────────────────────────────────────────────────────────────
# Per-room benchmark task
# ─────────────────────────────────────────────────────────────────────────────

async def bench_room_task(
    room:        Room,
    fstate:      FaultState,
    fault_eng:   FaultEngine,
    db:          RoomDatabase,
    rooms_ref:   Dict[str, Room],
    publisher:   MockPublisher,
    sim_start:   float,
    tick_interval: float,
    metrics:     Metrics,
    stop_event:  asyncio.Event,
) -> None:
    """Mirrors engine.room_task exactly but uses MockPublisher and records metrics."""

    # Startup jitter (0-1 s for benchmark, not 5 s)
    await asyncio.sleep(random.uniform(0, 1.0))

    tick_count = 0

    while not stop_event.is_set():
        tick_start = asyncio.get_event_loop().time()
        sim_clock  = time.time() - sim_start

        try:
            room.apply_physics(sim_clock)
            room.apply_environmental_correlations()
            room.validate_state()

            hvac_before = room.hvac_mode
            if random.random() < 0.10:
                room.set_occupancy(not room.occupancy)

            fault_eng.maybe_inject_fault(room.id, fstate)
            if fstate.total_activations > metrics.fault_count:
                metrics.fault_count = fstate.total_activations

            payload = room.telemetry_payload()
            if fstate.active:
                from faults import FaultEngine as _FE
                payload["fault"] = fault_eng.fault_summary(room.id, fstate)

            should_publish = await fault_eng.apply_fault(room.id, fstate, payload)

            if should_publish:
                await publisher.publish(f"{room.mqtt_path}/telemetry", payload)
                if tick_count % 5 == 0:
                    await publisher.publish(
                        f"{room.mqtt_path}/heartbeat",
                        room.heartbeat_payload()
                    )
            else:
                metrics.suppressed_count += 1

            db.mark_dirty(room)
            if room.hvac_mode != hvac_before:
                await db.flush_one(room, rooms_ref)

            metrics.tick_count += 1

        except Exception as exc:
            metrics.error_count += 1
            logger.error("[bench] room %s tick %d error: %s", room.id, tick_count, exc)

        tick_count += 1
        elapsed    = asyncio.get_event_loop().time() - tick_start
        sleep_time = max(0.0, tick_interval - elapsed)
        await asyncio.sleep(sleep_time)


# ─────────────────────────────────────────────────────────────────────────────
# Resource monitor
# ─────────────────────────────────────────────────────────────────────────────

async def resource_monitor(
    metrics:    Metrics,
    stop_event: asyncio.Event,
    num_rooms:  int,
) -> None:
    """
    Samples CPU% and RSS memory every SAMPLE_INTERVAL_S seconds.
    Prints a live status line so the terminal stays active.
    """
    proc = psutil.Process(os.getpid()) if _HAVE_PSUTIL else None

    while not stop_event.is_set():
        await asyncio.sleep(SAMPLE_INTERVAL_S)
        if stop_event.is_set():
            break

        elapsed  = metrics.elapsed
        mins, secs = divmod(int(elapsed), 60)

        if proc:
            cpu_pct = proc.cpu_percent(interval=None)
            mem_mb  = proc.memory_info().rss / (1024 * 1024)
            metrics.cpu_samples.append(cpu_pct)
            metrics.mem_mb_samples.append(mem_mb)
            metrics.sample_times.append(elapsed)
            resource_str = f"CPU={cpu_pct:5.1f}%  MEM={mem_mb:6.1f} MB"
        else:
            resource_str = "CPU=n/a  MEM=n/a  (install psutil for live stats)"

        p95 = metrics.percentile(95)
        p99 = metrics.percentile(99)
        tps = metrics.ticks_per_second

        flag = "🟢" if p95 < LATENCY_WARN_MS else "🔴"
        print(
            f"  [{mins:02d}:{secs:02d}] {resource_str} | "
            f"ticks={metrics.tick_count:>7,d}  tps={tps:5.1f}  "
            f"pub={metrics.publish_count:>7,d}  fault={metrics.fault_count:>4,d}  "
            f"p95_lat={p95:5.1f}ms {flag}  p99={p99:5.1f}ms  "
            f"err={metrics.error_count}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# DB sync monitor (patches db._flush_count / _rows)
# ─────────────────────────────────────────────────────────────────────────────

async def db_sync_monitor(
    db:         RoomDatabase,
    rooms:      Dict[str, Room],
    stop_event: asyncio.Event,
    metrics:    Metrics,
) -> None:
    """Wraps the standard periodic_sync_task and mirrors counts into Metrics."""
    orig_flush = db.flush

    async def instrumented_flush(rooms_d=None):
        n = await orig_flush(rooms_d)
        if n:
            metrics.db_flush_count  += 1
            metrics.db_rows_written += n
        return n

    db.flush = instrumented_flush   # monkey-patch for instrumentation
    await db.periodic_sync_task(rooms, stop_event)


# ─────────────────────────────────────────────────────────────────────────────
# Report printer
# ─────────────────────────────────────────────────────────────────────────────

def _bar(value: float, max_val: float, width: int = 30, char: str = "█") -> str:
    filled = int(round(value / max_val * width)) if max_val > 0 else 0
    filled = min(filled, width)
    return char * filled + "░" * (width - filled)


def print_report(metrics: Metrics, num_rooms: int, duration_s: int, db_path: str) -> bool:
    elapsed = metrics.elapsed
    p50  = metrics.percentile(50)
    p95  = metrics.percentile(95)
    p99  = metrics.percentile(99)
    p_max = max(metrics.latencies_ms) if metrics.latencies_ms else 0.0

    tps           = metrics.ticks_per_second
    avg_pub_rate  = metrics.publish_count / elapsed if elapsed > 0 else 0
    tick_per_room = metrics.tick_count / num_rooms if num_rooms > 0 else 0

    avg_cpu = sum(metrics.cpu_samples) / len(metrics.cpu_samples) if metrics.cpu_samples else None
    max_cpu = max(metrics.cpu_samples)                             if metrics.cpu_samples else None
    avg_mem = sum(metrics.mem_mb_samples) / len(metrics.mem_mb_samples) if metrics.mem_mb_samples else None
    max_mem = max(metrics.mem_mb_samples)                               if metrics.mem_mb_samples else None

    # Verify DB row count
    db_actual_rows = 0
    try:
        con = sqlite3.connect(db_path)
        db_actual_rows = con.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        con.close()
    except Exception:
        pass

    # ── pass / fail ───────────────────────────────────────────────────────
    pass_rooms    = num_rooms == 200
    pass_interval = abs(cfg.simulation.tick_interval - 5.0) < 0.01
    pass_latency  = p95 < LATENCY_BUDGET_MS
    pass_duration = elapsed >= (duration_s * 0.99)   # within 1 % of target
    pass_errors   = metrics.error_count == 0

    overall_pass = all([pass_rooms, pass_interval, pass_latency, pass_duration, pass_errors])

    W = 62
    sep  = "═" * W
    sep2 = "─" * W

    print()
    print(f"╔{sep}╗")
    print(f"║{'CAMPUS PULSE — BENCHMARK REPORT':^{W}}║")
    print(f"╠{sep}╣")

    def row(label: str, value: str, ok: Optional[bool] = None) -> str:
        badge = ""
        if ok is True:  badge = " ✅"
        if ok is False: badge = " ❌"
        return f"║  {label:<28}{value:<28}{badge:<4}║"

    print(row("Run duration",       f"{elapsed/60:.2f} min  ({elapsed:.0f} s)", pass_duration))
    print(row("Rooms simulated",    str(num_rooms),                               pass_rooms))
    print(row("Tick interval",      f"{cfg.simulation.tick_interval:.1f} s",      pass_interval))
    print(row("Total ticks",        f"{metrics.tick_count:,}"))
    print(row("Ticks / room",       f"{tick_per_room:.1f}"))
    print(row("Ticks / sec (all)",  f"{tps:.2f}"))
    print(f"║  {sep2}  ║")

    print(row("MQTT publishes",     f"{metrics.publish_count:,}"))
    print(row("Publish rate",       f"{avg_pub_rate:.1f} msg/s"))
    print(row("Suppressed (dropout)", f"{metrics.suppressed_count:,}"))
    print(f"║  {sep2}  ║")

    # Latency section
    lat_ok = p95 < LATENCY_BUDGET_MS
    print(row("Loop latency p50",   f"{p50:.2f} ms"))
    print(row("Loop latency p95",   f"{p95:.2f} ms  (limit: {LATENCY_BUDGET_MS:.0f} ms)", lat_ok))
    print(row("Loop latency p99",   f"{p99:.2f} ms"))
    print(row("Loop latency max",   f"{p_max:.2f} ms"))

    # Sparkline for latency distribution (buckets: 0-10, 10-50, 50-100, 100-200, >200 ms)
    lats = list(metrics.latencies_ms)
    if lats:
        buckets = [
            sum(1 for l in lats if l < 10),
            sum(1 for l in lats if 10 <= l < 50),
            sum(1 for l in lats if 50 <= l < 100),
            sum(1 for l in lats if 100 <= l < 200),
            sum(1 for l in lats if l >= 200),
        ]
        total   = len(lats)
        labels  = ["<10ms", "10-50", "50-100", "100-200", ">200ms"]
        print(f"║  {'Latency distribution':<{W-4}}  ║")
        for lbl, cnt in zip(labels, buckets):
            pct = cnt / total * 100 if total else 0
            bar = _bar(pct, 100, width=24)
            print(f"║    {lbl:<8} {bar} {pct:5.1f}%  ({cnt:,}){'':>4}║")

    print(f"║  {sep2}  ║")

    # Fault stats
    print(row("Fault activations",  f"{metrics.fault_count:,}"))
    print(row("Tick errors",        str(metrics.error_count), pass_errors))
    print(f"║  {sep2}  ║")

    # CPU / Memory
    if avg_cpu is not None:
        print(row("Avg CPU usage",  f"{avg_cpu:.1f}%"))
        print(row("Peak CPU usage", f"{max_cpu:.1f}%"))
        bar = _bar(max_cpu, 100, width=30)
        print(f"║  CPU peak  [{bar}] {max_cpu:.0f}%{'':>4}║")
    else:
        print(row("CPU / Memory",   "install psutil for stats"))

    if avg_mem is not None:
        print(row("Avg memory",     f"{avg_mem:.1f} MB"))
        print(row("Peak memory",    f"{max_mem:.1f} MB"))
        bar = _bar(max_mem, 512, width=30)
        print(f"║  MEM peak  [{bar}] {max_mem:.0f} MB{'':>2}║")

    print(f"║  {sep2}  ║")

    # DB stats
    print(row("DB sync cycles",     str(metrics.db_flush_count)))
    print(row("DB rows written",    f"{metrics.db_rows_written:,}"))
    print(row("DB rows in file",    str(db_actual_rows)))

    print(f"╠{sep}╣")

    # Final verdict
    verdict = "✅  ALL BENCHMARKS PASSED" if overall_pass else "❌  SOME BENCHMARKS FAILED"
    print(f"║  {'VERDICT:':<10} {verdict:<{W-13}}║")

    checks = [
        (pass_rooms,    f"200 rooms simulated          got={num_rooms}"),
        (pass_interval, f"5 s tick interval            got={cfg.simulation.tick_interval:.1f}s"),
        (pass_latency,  f"p95 latency < 200 ms         got={p95:.1f} ms"),
        (pass_duration, f"Full duration completed      got={elapsed:.0f}s / {duration_s}s"),
        (pass_errors,   f"Zero tick errors             got={metrics.error_count}"),
    ]
    for ok, desc in checks:
        icon = "  ✅" if ok else "  ❌"
        print(f"║{icon}  {desc:<{W-5}}║")

    print(f"╚{sep}╝")
    print()
    return overall_pass


# ─────────────────────────────────────────────────────────────────────────────
# Main benchmark runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_benchmark(num_rooms: int, duration_s: int) -> bool:
    metrics    = Metrics()
    stop_event = asyncio.Event()

    # Override config for benchmark (no code changes — proves config flexibility)
    num_floors      = 10
    rooms_per_floor = num_rooms // num_floors
    assert num_floors * rooms_per_floor == num_rooms, \
        f"num_rooms must be divisible by 10, got {num_rooms}"

    print()
    print("┌─────────────────────────────────────────────────────────────┐")
    print("│         CAMPUS PULSE — BENCHMARK STARTING                   │")
    print(f"│  rooms={num_rooms}  tick={cfg.simulation.tick_interval:.0f}s  "
          f"duration={duration_s//60}m{duration_s%60:02d}s  "
          f"fault_prob={cfg.faults.probability:.2f}          │")
    print("└─────────────────────────────────────────────────────────────┘")
    print()
    print("  Time     CPU      MEM        Ticks      TPS   Publishes  "
          "Faults  p95 lat  p99 lat  Errors")
    print("  " + "─" * 100)

    # ── Temporary DB (isolated from production DB) ────────────────────────
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    db_path = tmp_db.name

    from config import DatabaseConfig
    bench_db_cfg = DatabaseConfig(
        path=db_path,
        sync_interval=cfg.database.sync_interval,
        dirty_threshold=cfg.database.dirty_threshold,
    )
    db = RoomDatabase(bench_db_cfg)
    await db.init()

    # ── Build fleet ───────────────────────────────────────────────────────
    rooms: Dict[str, Room] = {}
    for floor in range(1, num_floors + 1):
        for rnum in range(1, rooms_per_floor + 1):
            room = Room(
                building=cfg.simulation.building_id,
                floor=floor,
                room_num=rnum,
                alpha=cfg.thermal.alpha,
                beta=cfg.thermal.beta,
            )
            rooms[room.id] = room

    print(f"  Fleet built: {len(rooms)} rooms")

    # ── Fault engine + publisher ──────────────────────────────────────────
    fault_engine = FaultEngine(cfg.faults)
    fault_states: Dict[str, FaultState] = {rid: FaultState() for rid in rooms}
    publisher    = MockPublisher(metrics)

    sim_start = time.time()
    metrics.start_time = sim_start

    # ── Warm CPU counter (psutil first call is always 0.0) ─────────────
    if _HAVE_PSUTIL:
        psutil.Process(os.getpid()).cpu_percent(interval=None)

    # ── Assemble all tasks ────────────────────────────────────────────────
    all_tasks = []

    # Room tasks
    for rid, room in rooms.items():
        all_tasks.append(asyncio.create_task(
            bench_room_task(
                room, fault_states[rid], fault_engine,
                db, rooms, publisher, sim_start,
                cfg.simulation.tick_interval,
                metrics, stop_event,
            ),
            name=rid,
        ))

    # Latency probe
    all_tasks.append(asyncio.create_task(
        latency_probe(metrics, stop_event), name="latency_probe"
    ))

    # Resource monitor
    all_tasks.append(asyncio.create_task(
        resource_monitor(metrics, stop_event, num_rooms), name="resource_monitor"
    ))

    # DB sync
    all_tasks.append(asyncio.create_task(
        db_sync_monitor(db, rooms, stop_event, metrics), name="db_sync"
    ))

    # ── Duration timer ────────────────────────────────────────────────────
    async def _timer():
        await asyncio.sleep(duration_s)
        stop_event.set()

    timer_task = asyncio.create_task(_timer(), name="timer")

    # ── Wait for timer then cancel rooms ─────────────────────────────────
    await timer_task
    stop_event.set()

    # Give tasks a moment to notice stop_event before cancelling
    await asyncio.sleep(0.5)
    for t in all_tasks:
        t.cancel()
    await asyncio.gather(*all_tasks, return_exceptions=True)

    # ── Final DB flush ────────────────────────────────────────────────────
    await db.flush(rooms)
    await db.close()

    # ── Print report ──────────────────────────────────────────────────────
    passed = print_report(metrics, num_rooms, duration_s, db_path)

    # Cleanup temp DB
    try:
        os.unlink(db_path)
    except OSError:
        pass

    return passed


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Campus Pulse benchmark — 200 rooms, 5 s tick, 30 min run"
    )
    parser.add_argument(
        "--duration", type=int, default=1800,
        help="Benchmark duration in seconds (default: 1800 = 30 min)",
    )
    parser.add_argument(
        "--rooms", type=int, default=200,
        help="Number of rooms to simulate (must be divisible by 10, default: 200)",
    )
    args = parser.parse_args()

    if not _HAVE_PSUTIL:
        print("⚠️  psutil not installed — CPU/memory stats will not be available.")
        print("   Run:  pip install psutil\n")

    try:
        passed = asyncio.run(run_benchmark(args.rooms, args.duration))
        sys.exit(0 if passed else 1)
    except KeyboardInterrupt:
        print("\n[benchmark] Interrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
