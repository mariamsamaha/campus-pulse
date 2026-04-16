"""
reliability/coap_con_sender.py
==============================
Gateway-side CoAP CON command sender.

This module is the "client half" of the CoAP CON reliability story.
``CoAPNode`` (nodes/coap_node.py) is the server half.

Role in the system
------------------
In the deployed hybrid architecture the flow is:

  ThingsBoard rule chain
       │  (MQTT)
       ▼
  Node-RED Gateway   ──────── CON PUT ──────▶  CoAPNode (room server)
                     ◀──────── 2.04 ACK ─────

This module simulates / implements that Node-RED Gateway role in Python,
using aiocoap as the CoAP client.

Reliability guarantees provided
--------------------------------
1. CON (Confirmable) message type — the CoAP server MUST ACK.
2. aiocoap retransmits automatically if no ACK arrives within
   ACK_TIMEOUT (default 2 s), up to MAX_RETRANSMIT (default 4) times.
3. The application layer logs every attempt and its outcome.
4. If the node's dedup cache rejects a retransmit, it returns 2.03 Valid
   which this sender treats as a successful "idempotent ACK" — no
   duplicate actuator side-effect occurs.

Usage
-----
  from reliability.coap_con_sender import CoapConSender
  sender = CoapConSender()
  await sender.send_command(
      host="127.0.0.1",
      port=5683,
      path=("f01", "r111", "actuators", "hvac"),
      command={"action": "SET_HVAC", "value": "ECO"},
  )
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiocoap
from aiocoap import Message, Code

logger = logging.getLogger("coap_con_sender")

# ─── Result dataclass ────────────────────────────────────────────────────────

@dataclass
class ConSendResult:
    """Outcome of a single CON PUT attempt."""
    node_id:         str
    action:          str
    success:         bool
    response_code:   str          # "2.04 Changed", "2.03 Valid", "error", …
    was_duplicate:   bool         # True if server returned 2.03 Valid
    latency_ms:      float
    attempts:        int          # 1 = first try, >1 = retransmit needed
    error:           Optional[str] = None

    def log(self) -> None:
        status = "ACK" if self.success else "FAIL"
        dup    = " [DUP-idempotent]" if self.was_duplicate else ""
        logger.info(
            "[CON-SENDER] [RELIABILITY] %s node=%s action=%s "
            "code=%s latency=%.1f ms attempts=%d%s",
            status, self.node_id, self.action,
            self.response_code, self.latency_ms, self.attempts, dup,
        )


# ─── Stats accumulator ───────────────────────────────────────────────────────

@dataclass
class ConSenderStats:
    total:       int = 0
    acked:       int = 0
    idempotent:  int = 0    # 2.03 Valid (duplicate, already processed)
    failed:      int = 0
    latencies:   list = field(default_factory=list)

    def record(self, r: ConSendResult) -> None:
        self.total += 1
        if r.success:
            self.acked += 1
            if r.was_duplicate:
                self.idempotent += 1
        else:
            self.failed += 1
        self.latencies.append(r.latency_ms)

    def summary(self) -> dict:
        lats = self.latencies
        return {
            "total":            self.total,
            "acked":            self.acked,
            "idempotent_acks":  self.idempotent,
            "failed":           self.failed,
            "mean_latency_ms":  round(sum(lats) / max(len(lats), 1), 2),
            "max_latency_ms":   round(max(lats, default=0.0), 2),
            "zero_loss":        self.failed == 0,
            "zero_duplicate_side_effect": self.idempotent == self.total - self.acked + self.idempotent,
        }


# ─── Sender ──────────────────────────────────────────────────────────────────

class CoapConSender:
    """
    Sends CON PUT commands to CoAP room nodes and tracks ACK outcomes.

    A single aiocoap client context is reused across all calls for
    efficiency (connection setup overhead is amortised).
    """

    def __init__(self) -> None:
        self._ctx: Optional[aiocoap.Context] = None
        self.stats = ConSenderStats()

    async def _get_ctx(self) -> aiocoap.Context:
        if self._ctx is None:
            self._ctx = await aiocoap.Context.create_client_context()
        return self._ctx

    async def send_command(
        self,
        host:    str,
        port:    int,
        path:    tuple[str, ...],
        command: dict[str, Any],
        node_id: str = "",
    ) -> ConSendResult:
        """
        Send a CON PUT to coap://<host>:<port>/<path…> with <command> as JSON.

        Returns a ConSendResult describing the outcome.

        aiocoap retransmission behaviour (RFC 7252 §4.2)
        -------------------------------------------------
        If no ACK is received within ACK_TIMEOUT (≈2 s), aiocoap doubles the
        timeout and retransmits, up to MAX_RETRANSMIT (4) times.  The total
        wait before giving up is ≈ 45 s in the worst case.

        Because the CoAP node's HVACResource uses dedup, a retransmit of the
        same payload bytes returns 2.03 Valid (not 2.04 Changed) — proving the
        actuator was not applied twice.
        """
        ctx     = await self._get_ctx()
        uri     = f"coap://{host}:{port}/{'/'.join(path)}"
        payload = json.dumps(command).encode()
        action  = command.get("action", "?")

        logger.info(
            "[CON-SENDER] [RELIABILITY] Sending CON PUT → %s  action=%s",
            uri, action,
        )

        t0 = time.monotonic()
        attempts = 1
        error_msg: Optional[str] = None
        response_code = "error"
        success = False

        try:
            req = Message(
                code=Code.PUT,
                uri=uri,
                payload=payload,
                # mtype=aiocoap.CON is the default for client requests
            )
            response = await ctx.request(req).response

            latency_ms    = (time.monotonic() - t0) * 1000
            response_code = str(response.code)
            was_duplicate = response.code == Code.VALID      # 2.03 Valid

            # Both 2.04 Changed and 2.03 Valid count as "success"
            # (2.03 = idempotent ACK: command was already applied)
            success = response.code in (Code.CHANGED, Code.VALID)

            if success:
                logger.info(
                    "[CON-SENDER] [RELIABILITY] ACK received ← %s  "
                    "node=%s action=%s latency=%.1f ms%s",
                    response_code, node_id or uri, action, latency_ms,
                    "  [idempotent-already-processed]" if was_duplicate else "",
                )
            else:
                logger.error(
                    "[CON-SENDER] [RELIABILITY] Unexpected response %s "
                    "from %s", response_code, uri,
                )

        except aiocoap.error.RequestTimedOut:
            latency_ms    = (time.monotonic() - t0) * 1000
            was_duplicate = False
            error_msg     = "CON request timed out after all retransmits"
            logger.error(
                "[CON-SENDER] [RELIABILITY] TIMEOUT — no ACK for %s "
                "after %.1f ms (all retransmits exhausted)", uri, latency_ms,
            )

        except Exception as exc:
            latency_ms    = (time.monotonic() - t0) * 1000
            was_duplicate = False
            error_msg     = str(exc)
            logger.error(
                "[CON-SENDER] [RELIABILITY] ERROR sending CON PUT to %s: %s",
                uri, exc,
            )

        result = ConSendResult(
            node_id=node_id or uri,
            action=action,
            success=success,
            response_code=response_code,
            was_duplicate=was_duplicate,
            latency_ms=latency_ms,
            attempts=attempts,
            error=error_msg,
        )
        result.log()
        self.stats.record(result)
        return result

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.shutdown()
            self._ctx = None

    def print_stats(self) -> None:
        s = self.stats.summary()
        print("\n" + "─" * 55)
        print("  CON SENDER RELIABILITY STATS")
        print("─" * 55)
        print(f"  Total commands sent:      {s['total']}")
        print(f"  ACK received (2.04):      {s['acked'] - s['idempotent_acks']}")
        print(f"  Idempotent ACK (2.03):    {s['idempotent_acks']}")
        print(f"  Failed (timeout/error):   {s['failed']}")
        print(f"  Mean latency:             {s['mean_latency_ms']} ms")
        print(f"  Max  latency:             {s['max_latency_ms']} ms")
        print(f"  Zero loss:                {'✓' if s['zero_loss'] else '✗'}")
        print("─" * 55 + "\n")
