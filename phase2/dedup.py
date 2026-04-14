from __future__ import annotations
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
logger = logging.getLogger("dedup")

DEDUP_TTL_S: float = 30.0
MAX_CACHE_SIZE: int = 5_000

@dataclass
class _Entry:
    seen_at: float = field(default_factory=time.monotonic)

class _TTLCache:
    def __init__(self, ttl: float = DEDUP_TTL_S, max_size: int = MAX_CACHE_SIZE):
        self._ttl = ttl
        self._max = max_size
        self._store: OrderedDict[str, float] = OrderedDict()

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, ts in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]

    def seen(self, key: str) -> bool:
        """Return True if key was registered recently (duplicate)."""
        self._evict_expired()
        return key in self._store

    def register(self, key: str) -> None:
        if len(self._store) >= self._max:
            self._store.popitem(last=False)  
        self._store[key] = time.monotonic()
        self._store.move_to_end(key)           

class DedupHandler:

    def __init__(self):
        self._mqtt_cache  = _TTLCache()   
        self._coap_cache  = _TTLCache()   
        self._stats = {"mqtt_dup": 0, "coap_dup": 0, "mqtt_ok": 0, "coap_ok": 0}

    def is_mqtt_duplicate(self, client_id: str, packet_id: int | None) -> bool:
        if packet_id is None:
            self._stats["mqtt_ok"] += 1
            return False

        key = f"{client_id}:{packet_id}"
        if self._mqtt_cache.seen(key):
            self._stats["mqtt_dup"] += 1
            logger.debug(
                "[DEDUP-MQTT] DUP detected — client=%s pkt_id=%d — DROPPED",
                client_id, packet_id,
            )
            return True

        self._mqtt_cache.register(key)
        self._stats["mqtt_ok"] += 1
        return False

    def mark_mqtt_processed(self, client_id: str, packet_id: int) -> None:
        key = f"{client_id}:{packet_id}"
        self._mqtt_cache.register(key)

    def is_coap_duplicate(self, node_id: str, content_hash: str) -> bool:

        key = f"{node_id}:{content_hash}"
        if self._coap_cache.seen(key):
            self._stats["coap_dup"] += 1
            logger.debug(
                "[DEDUP-CoAP] Duplicate CON detected — node=%s hash=%s — DROPPED",
                node_id, content_hash,
            )
            return True

        self._coap_cache.register(key)
        self._stats["coap_ok"] += 1
        return False
    def stats(self) -> dict:
        """Return a snapshot of dedup statistics for monitoring."""
        total = sum(self._stats.values())
        return {
            **self._stats,
            "total_messages": total,
            "dup_rate_pct": round(
                100.0 * (self._stats["mqtt_dup"] + self._stats["coap_dup"]) / max(total, 1), 2
            ),
        }

    def reset_stats(self) -> None:
        self._stats = {"mqtt_dup": 0, "coap_dup": 0, "mqtt_ok": 0, "coap_ok": 0}
