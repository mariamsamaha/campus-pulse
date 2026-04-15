from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import List

import psutil

logger = logging.getLogger("performance_monitor")


@dataclass
class PerfSample:
    timestamp:  float
    cpu_pct:    float
    mem_mb:     float
    mem_pct:    float


@dataclass
class PerfReport:
    samples: List[PerfSample] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

    def add(self, s: PerfSample) -> None:
        self.samples.append(s)

    def summary(self) -> dict:
        if not self.samples:
            return {}
        cpus = [s.cpu_pct for s in self.samples]
        mems = [s.mem_mb  for s in self.samples]
        uptime = time.time() - self.start_time
        return {
            "uptime_s":        round(uptime, 1),
            "sample_count":    len(self.samples),
            "cpu_mean_pct":    round(mean(cpus), 2),
            "cpu_max_pct":     round(max(cpus), 2),
            "cpu_stdev":       round(stdev(cpus), 2) if len(cpus) > 1 else 0.0,
            "mem_mean_mb":     round(mean(mems), 1),
            "mem_max_mb":      round(max(mems), 1),
            "mem_mean_pct":    round(mean(s.mem_pct for s in self.samples), 2),
        }

    def print_table(self) -> None:
        summ = self.summary()
        if not summ:
            print("  [Perf] No samples yet.")
            return
        print("\n" + "─" * 55)
        print("  PERFORMANCE MONITOR REPORT")
        print("─" * 55)
        print(f"  Uptime:          {summ['uptime_s']} s")
        print(f"  Samples:         {summ['sample_count']}")
        print(f"  CPU mean:        {summ['cpu_mean_pct']} %")
        print(f"  CPU max:         {summ['cpu_max_pct']} %")
        print(f"  Memory mean:     {summ['mem_mean_mb']} MB")
        print(f"  Memory max:      {summ['mem_max_mb']} MB")
        print(f"  Memory mean %:   {summ['mem_mean_pct']} %")
        print("─" * 55 + "\n")

    def to_json(self, path: str) -> None:
        """Save full sample log as JSON (for report appendix)."""
        data = {
            "summary": self.summary(),
            "samples": [
                {
                    "t":       round(s.timestamp - self.start_time, 1),
                    "cpu_pct": s.cpu_pct,
                    "mem_mb":  s.mem_mb,
                }
                for s in self.samples
            ],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("[PerfMonitor] Report saved → %s", path)


class PerformanceMonitor:
    def __init__(self, sample_interval_s: float = 10.0):
        self.interval = sample_interval_s
        self.report   = PerfReport()
        self._proc    = psutil.Process()

    async def run(self) -> None:
        logger.info(
            "[PerfMonitor] Started — sampling every %.0f s", self.interval
        )
        while True:
            cpu  = self._proc.cpu_percent(interval=None)
            mem  = self._proc.memory_info().rss / (1024 * 1024)   # MB
            mem_pct = self._proc.memory_percent()
            sample = PerfSample(
                timestamp=time.time(),
                cpu_pct=cpu,
                mem_mb=mem,
                mem_pct=mem_pct,
            )
            self.report.add(sample)
            logger.info(
                "[PerfMonitor] CPU=%.1f%%  MEM=%.1f MB (%.1f%%)",
                cpu, mem, mem_pct,
            )
            await asyncio.sleep(self.interval)