from __future__ import annotations
import sys
import time
from dedup import DedupHandler, _TTLCache

def _section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print('─'*60)

def test_mqtt_dup():
    _section("1. MQTT DUP Flag : same packet_id from same client")
    d = DedupHandler()
    client = "campus-mqtt-b01-f01-r101"
    pid = 42

    result1 = d.is_mqtt_duplicate(client, pid)
    assert result1 is False, "First delivery must NOT be a duplicate"
    print(f"  [PASS] First  delivery  → is_duplicate={result1}  (PROCESS ✓)")

    result2 = d.is_mqtt_duplicate(client, pid)
    assert result2 is True,  "Second delivery with same packet_id must be a DUP"
    print(f"  [PASS] Retransmit (DUP) → is_duplicate={result2}  (DROP ✓)")


def test_mqtt_different_pid():
    _section("2. MQTT — different packet_id → both processed")
    d = DedupHandler()
    client = "campus-mqtt-b01-f01-r102"

    r1 = d.is_mqtt_duplicate(client, 10)
    r2 = d.is_mqtt_duplicate(client, 11)
    assert r1 is False and r2 is False
    print(f"  [PASS] pid=10 → {r1}  pid=11 → {r2}  (both PROCESS ✓)")


def test_mqtt_qos0_no_dup():
    _section("3. MQTT QoS 0 — no packet_id, cannot be a DUP")
    d = DedupHandler()
    r1 = d.is_mqtt_duplicate("campus-mqtt-b01-f01-r103", None)
    r2 = d.is_mqtt_duplicate("campus-mqtt-b01-f01-r103", None)
    assert r1 is False and r2 is False, "QoS 0 never has DUPs"
    print(f"  [PASS] QoS 0 (packet_id=None) → always False  (PROCESS ✓)")


def test_mqtt_ttl_expiry():
    _section("4. MQTT TTL expiry — same packet_id is fresh after TTL")
    cache = _TTLCache(ttl=0.1)
    cache.register("client:42")
    assert cache.seen("client:42"), "Should be seen immediately"
    time.sleep(0.15)
    assert not cache.seen("client:42"), "Should expire after TTL"
    print("  [PASS] Packet ID expired after TTL — re-registered as fresh  (✓)")


def test_coap_duplicate():
    _section("5. CoAP CON retransmit — same content_hash → DROP")
    d = DedupHandler()
    node = "b01-f01-r111"
    h    = "abc123def456"

    r1 = d.is_coap_duplicate(node, h)
    assert r1 is False
    print(f"  [PASS] First CON  delivery  → is_duplicate={r1}  (PROCESS ✓)")

    r2 = d.is_coap_duplicate(node, h)
    assert r2 is True
    print(f"  [PASS] Retransmit (same hash) → is_duplicate={r2}  (DROP ✓)")


def test_coap_different_hash():
    _section("6. CoAP — different content_hash → both processed")
    d = DedupHandler()
    node = "b01-f01-r112"

    r1 = d.is_coap_duplicate(node, "hash_A")
    r2 = d.is_coap_duplicate(node, "hash_B")
    assert r1 is False and r2 is False
    print(f"  [PASS] hash_A → {r1}  hash_B → {r2}  (both PROCESS ✓)")

def test_cross_node_isolation():
    _section("7. Cross-node isolation — same packet_id on different clients")
    d = DedupHandler()
    r1 = d.is_mqtt_duplicate("campus-mqtt-b01-f01-r101", 99)
    r2 = d.is_mqtt_duplicate("campus-mqtt-b01-f01-r102", 99)
    assert r1 is False and r2 is False, "Different clients must be isolated"
    print(f"  [PASS] client-r101 pid=99 → {r1}  client-r102 pid=99 → {r2}  (isolated ✓)")

def test_stats():
    _section("8. Dedup statistics snapshot")
    d = DedupHandler()

    for pid in range(5):
        d.is_mqtt_duplicate("campus-mqtt-b01-f01-r101", pid)
    d.is_mqtt_duplicate("campus-mqtt-b01-f01-r101", 0)   # DUP
    d.is_mqtt_duplicate("campus-mqtt-b01-f01-r101", 1)   # DUP

    for h in ["h1", "h2", "h3"]:
        d.is_coap_duplicate("b01-f01-r111", h)
    d.is_coap_duplicate("b01-f01-r111", "h1")  

    stats = d.stats()
    print(f"  mqtt_ok={stats['mqtt_ok']}  mqtt_dup={stats['mqtt_dup']}")
    print(f"  coap_ok={stats['coap_ok']}  coap_dup={stats['coap_dup']}")
    print(f"  total={stats['total_messages']}  dup_rate={stats['dup_rate_pct']}%")
    assert stats["mqtt_dup"] == 2
    assert stats["coap_dup"] == 1
    assert stats["dup_rate_pct"] > 0
    print("  [PASS] Stats correct ✓")

def test_content_hash_uniqueness():
    _section("9. Telemetry content_hash — different readings → different hashes")
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from telemetry_schema import build_telemetry

    base = dict(sensor_id="b01-f01-r101", building="b01", floor=1, room=101,
                protocol="MQTT", humidity=55.0, occupancy=True, light_level=450,
                hvac_mode="ON", lighting_dimmer=45)

    p1 = build_telemetry(temperature=22.0, **base)
    p2 = build_telemetry(temperature=23.0, **base)
    p3 = build_telemetry(temperature=22.0, **base)  

    assert p1["content_hash"] != p2["content_hash"], "Different temps must differ"
    assert p1["content_hash"] == p3["content_hash"], "Same readings must match"
    print(f"  [PASS] T=22.0 → hash={p1['content_hash']}")
    print(f"  [PASS] T=23.0 → hash={p2['content_hash']}  (different ✓)")
    print(f"  [PASS] T=22.0 (again) → hash={p3['content_hash']}  (same as first ✓)")


if __name__ == "__main__":
    print("=" * 60)
    print("  Campus Pulse Phase 2 — DUP Deduplication Test Suite")
    print("=" * 60)

    tests = [
        test_mqtt_dup,
        test_mqtt_different_pid,
        test_mqtt_qos0_no_dup,
        test_mqtt_ttl_expiry,
        test_coap_duplicate,
        test_coap_different_hash,
        test_cross_node_isolation,
        test_stats,
        test_content_hash_uniqueness,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed / {failed} failed")
    print(f"{'='*60}\n")
    sys.exit(0 if failed == 0 else 1)
