from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from statistics import mean, median, stdev
from typing import Dict, List, Optional
logger = logging.getLogger("latency_tracker")

@dataclass
class RTTSample:
    node_id:    str
    command:    str
    t0_ms:      int         
    t1_ms:      Optional[int] = None   
    t2_ms:      Optional[int] = None  
    rtt_ms:     Optional[float] = None

    def complete(self) -> bool:
        return self.t2_ms is not None

    def compute_rtt(self) -> None:
        if self.t2_ms and self.t0_ms:
            self.rtt_ms = self.t2_ms - self.t0_ms

@dataclass
class LatencyReport:
    samples: List[RTTSample] = field(default_factory=list)

    def completed(self) -> List[RTTSample]:
        return [s for s in self.samples if s.complete()]

    def summary(self) -> dict:
        rtts = [s.rtt_ms for s in self.completed() if s.rtt_ms is not None]
        if not rtts:
            return {"n": 0, "msg": "No completed samples yet"}
        return {
            "n":           len(rtts),
            "min_ms":      round(min(rtts), 2),
            "max_ms":      round(max(rtts), 2),
            "mean_ms":     round(mean(rtts), 2),
            "median_ms":   round(median(rtts), 2),
            "stdev_ms":    round(stdev(rtts), 2) if len(rtts) > 1 else 0.0,
            "under_500ms": sum(1 for r in rtts if r < 500),
            "pct_ok":      round(100 * sum(1 for r in rtts if r < 500) / len(rtts), 1),
            "target_met":  all(r < 500 for r in rtts),
        }

    def print_table(self) -> None:
        done = self.completed()
        if not done:
            print("  [RTT] No completed round-trips yet.")
            return

        print(f"\n{'─'*70}")
        print(f"  {'Node ID':<30} {'Command':<20} {'RTT (ms)':>10}  {'OK?':>5}")
        print(f"{'─'*70}")
        for s in done[-20:]: 
            ok = "✓" if (s.rtt_ms or 9999) < 500 else "✗"
            rtt_str = f"{s.rtt_ms:.1f}" if s.rtt_ms else "N/A"
            print(f"  {s.node_id:<30} {s.command:<20} {rtt_str:>10}  {ok:>5}")

        summ = self.summary()
        print(f"{'─'*70}")
        print(
            f"  SUMMARY: n={summ['n']}  "
            f"min={summ['min_ms']}ms  "
            f"mean={summ['mean_ms']}ms  "
            f"max={summ['max_ms']}ms  "
            f"<500ms={summ['under_500ms']}/{summ['n']} "
            f"({summ['pct_ok']}%)  "
            f"TARGET={'MET ✓' if summ['target_met'] else 'MISSED ✗'}"
        )
        print(f"{'─'*70}\n")

class LatencyTracker:
    def __init__(self):
        self.report   = LatencyReport()
        self._pending: Dict[str, RTTSample] = {}  

    def record_command_sent(self, node_id: str, command: str) -> int:
        """Call immediately before publishing a command. Returns t0_ms."""
        t0 = int(time.time() * 1000)
        sample = RTTSample(node_id=node_id, command=command, t0_ms=t0)
        self._pending[node_id] = sample
        logger.debug("[RTT] Command sent to %s at t0=%d ms", node_id, t0)
        return t0

    def on_ack(self, node_id: str, ts_ms: int) -> None:
        """Call when a node ACK arrives (from /ack or /cmd-response topic)."""
        sample = self._pending.get(node_id)
        if sample and sample.t1_ms is None:
            sample.t1_ms = ts_ms
            logger.debug("[RTT] ACK from %s at t1=%d ms (leg1=%.1f ms)",
                         node_id, ts_ms, ts_ms - sample.t0_ms)

    def on_telemetry(self, node_id: str, ts_ms: int, hvac_mode: str) -> None:

        sample = self._pending.get(node_id)
        if sample is None:
            return
        sample.t2_ms = ts_ms
        sample.compute_rtt()
        self.report.samples.append(sample)
        del self._pending[node_id]
        logger.info(
            "[RTT] %s command='%s' RTT=%.1f ms %s",
            node_id, sample.command, sample.rtt_ms or 0,
            "✓" if (sample.rtt_ms or 9999) < 500 else "✗ SLOW",
        )

    def summary(self) -> dict:
        return self.report.summary()

async def _standalone_listener(broker_host: str = "localhost", port: int = 1883) -> None:

    try:
        from gmqtt import Client as MQTTClient
    except ImportError:
        print("ERROR: gmqtt not installed. Run:  pip install gmqtt")
        return

    tracker = LatencyTracker()

    client = MQTTClient("campus-latency-observer")

    def on_connect(cli, flags, rc, props):
        cli.subscribe("campus/+/+/+/ack",       qos=1)
        cli.subscribe("campus/+/+/+/telemetry", qos=1)
        print(f"[Latency Observer] Connected to {broker_host}:{port}")
        print("  Listening for ack and telemetry messages …\n")

    def on_message(cli, topic, payload_bytes, qos, props):
        try:
            data = json.loads(payload_bytes.decode())
        except Exception:
            return

        parts   = topic.split("/")
        node_id = f"{parts[1]}-{parts[2]}-{parts[3]}" if len(parts) >= 4 else "unknown"

        if topic.endswith("/ack"):
            ts_ms = data.get("ts_ms") or int(data.get("timestamp", 0) * 1000)
            tracker.on_ack(node_id, ts_ms)

        elif topic.endswith("/telemetry"):
            ts_ms    = data.get("metadata", {}).get("ts_ms", int(time.time() * 1000))
            hvac     = data.get("actuators", {}).get("hvac_mode", "")
            tracker.on_telemetry(node_id, ts_ms, hvac)

    client.on_connect = on_connect
    client.on_message = on_message

    await client.connect(broker_host, port)
    try:
        while True:
            await asyncio.sleep(10)
            tracker.report.print_table()
    except asyncio.CancelledError:
        pass
    finally:
        await client.disconnect()


if __name__ == "__main__":
    import sys
    broker = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    print("Campus Pulse Phase 2 — Latency Tracker")
    print(f"Broker: {broker}:1883")
    print("Press Ctrl+C to stop and print final report.\n")
    asyncio.run(_standalone_listener(broker))
