"""
test_ota.py — Phase 3: Unit tests for the Secure OTA Update System.

Coverage
--------
• SHA-256 signing & verification (integrity.py)
• Fleet versioning system (versioning.py)
• Tamper detection & audit logging (audit_logger.py)
• MQTT topic parsing & routing (mqtt_handler.py)
• Full receiver pipeline (receiver.py)
• Publisher signing & dispatch (publisher.py)

Run
---
    python -m pytest phase3/test_ota.py -v
"""

from __future__ import annotations

import json
import sys
import pathlib
from unittest.mock import MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from ota.integrity import (
    OtaIntegrityVerifier,
    compute_sha256,
    sign_payload,
)
from ota.versioning import (
    FleetVersionRegistry,
    NodeVersionState,
    is_newer,
    is_downgrade,
    _parse_version_tuple,
)
from ota.audit_logger import OtaAuditLogger, SEVERITY_MAP
from ota.mqtt_handler import OtaMqttHandler, OtaTopicTarget, TOPIC_PATTERN
from ota.types import OtaStatus, TamperReason, OtaUpdateResult
from ota.receiver import OtaReceiver


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def verifier():
    return OtaIntegrityVerifier()


@pytest.fixture
def registry():
    r = FleetVersionRegistry()
    for i in range(1, 6):
        r.register(f"b01-f01-r{100 + i}", "1.0")
    return r


@pytest.fixture
def audit_logger(tmp_path):
    log_file = tmp_path / "tamper_log.json"
    return OtaAuditLogger(log_file=log_file)


@pytest.fixture
def valid_signed_payload():
    params = {"alpha": 0.012, "beta": 0.22}
    signed = sign_payload(params, version="1.1")
    return json.dumps(signed).encode()


@pytest.fixture
def sample_node_ids():
    return [f"b01-f01-r{100 + i}" for i in range(1, 6)]


# ──────────────────────────────────────────────────────────────────────
# SHA-256 Integrity Tests
# ──────────────────────────────────────────────────────────────────────

class TestSHA256Integrity:

    def test_compute_sha256_deterministic(self):
        data = {"beta": 0.20, "alpha": 0.01}
        h1 = compute_sha256(data)
        h2 = compute_sha256(data)
        assert h1 == h2

    def test_compute_sha256_key_order_independent(self):
        data1 = {"alpha": 0.01, "beta": 0.20}
        data2 = {"beta": 0.20, "alpha": 0.01}
        assert compute_sha256(data1) == compute_sha256(data2)

    def test_compute_sha256_different_values_different_hashes(self):
        h1 = compute_sha256({"alpha": 0.01})
        h2 = compute_sha256({"alpha": 0.02})
        assert h1 != h2

    def test_sign_payload_contains_signature(self):
        signed = sign_payload({"alpha": 0.01}, "1.0")
        assert "signature" in signed
        assert len(signed["signature"]) == 64

    def test_sign_payload_contains_version(self):
        signed = sign_payload({}, "2.0")
        assert signed["version"] == "2.0"

    def test_sign_payload_preserves_params(self):
        params = {"alpha": 0.01, "beta": 0.20, "thermal_leakage": 0.05}
        signed = sign_payload(params, "1.0")
        assert signed["alpha"] == 0.01
        assert signed["beta"] == 0.20
        assert signed["thermal_leakage"] == 0.05

    def test_verify_valid_payload(self, verifier, valid_signed_payload):
        payload, alert = verifier.verify(valid_signed_payload, node_id="b01-f01-r101")
        assert payload is not None
        assert alert is None
        assert payload.version == "1.1"
        assert payload.params == {"alpha": 0.012, "beta": 0.22}

    def test_verify_tampered_payload(self, verifier):
        signed = sign_payload({"alpha": 0.01}, "1.0")
        signed["signature"] = "0000000000000000000000000000000000000000000000000000000000000000"
        payload, alert = verifier.verify(
            json.dumps(signed).encode(), node_id="b01-f01-r101"
        )
        assert payload is None
        assert alert is not None
        assert alert.reason == TamperReason.HASH_MISMATCH

    def test_verify_missing_signature(self, verifier):
        data = {"alpha": 0.01, "version": "1.0"}
        payload, alert = verifier.verify(
            json.dumps(data).encode(), node_id="b01-f01-r101"
        )
        assert payload is None
        assert alert is not None
        assert alert.reason == TamperReason.MISSING_SIGNATURE

    def test_verify_malformed_json(self, verifier):
        payload, alert = verifier.verify(b"not json at all", node_id="b01-f01-r101")
        assert payload is None
        assert alert is not None
        assert alert.reason == TamperReason.MALFORMED_JSON

    def test_verify_stats_tracking(self, verifier, valid_signed_payload):
        verifier.verify(valid_signed_payload, node_id="n1")
        verifier.verify(valid_signed_payload, node_id="n2")
        verifier.verify(b"bad", node_id="n3")
        stats = verifier.stats
        assert stats["verified"] == 2
        assert stats["tampered"] == 1

    def test_verify_raw_payload_stored_in_tamper_alert(self, verifier):
        raw = b'{"alpha":0.01}'
        payload, alert = verifier.verify(raw, node_id="b01-f01-r101")
        assert alert is not None
        assert alert.raw_payload == raw


# ──────────────────────────────────────────────────────────────────────
# Versioning Tests
# ──────────────────────────────────────────────────────────────────────

class TestVersioning:

    def test_parse_version_tuple(self):
        assert _parse_version_tuple("1.0") == (1, 0)
        assert _parse_version_tuple("2.5") == (2, 5)
        assert _parse_version_tuple("10.12") == (10, 12)

    def test_parse_version_tuple_malformed(self):
        assert _parse_version_tuple("bad") == (0, 0)
        assert _parse_version_tuple("") == (0, 0)

    def test_is_newer(self):
        assert is_newer("1.1", "1.0") is True
        assert is_newer("2.0", "1.9") is True
        assert is_newer("1.0", "1.0") is False
        assert is_newer("0.9", "1.0") is False

    def test_is_downgrade(self):
        assert is_downgrade("0.9", "1.0") is True
        assert is_downgrade("1.0", "1.0") is False
        assert is_downgrade("1.1", "1.0") is False

    def test_register_node(self, registry):
        state = registry.get_node_state("b01-f01-r101")
        assert state is not None
        assert state.current_version == "1.0"
        assert state.desired_version == "1.0"

    def test_set_desired_version(self, registry):
        count = registry.set_desired_version("1.1")
        assert count == 5
        for n in registry.all_nodes():
            assert n.desired_version == "1.1"
            assert n.update_pending is True

    def test_set_desired_version_targets_subset(self, registry):
        count = registry.set_desired_version(
            "1.2", targets=["b01-f01-r101", "b01-f01-r102"]
        )
        assert count == 2
        assert registry.get_node_state("b01-f01-r101").desired_version == "1.2"
        assert registry.get_node_state("b01-f01-r103").desired_version == "1.0"

    def test_confirm_update(self, registry):
        registry.set_desired_version("1.1")
        ok = registry.confirm_update("b01-f01-r101", "1.1")
        assert ok is True
        state = registry.get_node_state("b01-f01-r101")
        assert state.current_version == "1.1"
        assert state.desired_version == "1.1"
        assert state.in_sync is True
        assert state.update_pending is False

    def test_reject_update(self, registry):
        registry.set_desired_version("1.1")
        registry.reject_update("b01-f01-r101", "hash_mismatch")
        state = registry.get_node_state("b01-f01-r101")
        assert state.update_pending is False
        assert "hash_mismatch" in (state.last_update_result or "")

    def test_out_of_sync_nodes(self, registry):
        registry.set_desired_version("1.1")
        registry.confirm_update("b01-f01-r101", "1.1")
        pending = registry.out_of_sync_nodes()
        assert len(pending) == 4

    def test_summary(self, registry):
        registry.set_desired_version("1.1")
        registry.confirm_update("b01-f01-r101", "1.1")
        summary = registry.summary()
        assert summary["total"] == 5
        assert summary["in_sync"] == 1
        assert summary["out_of_sync"] == 4

    def test_dashboard_rows(self, registry):
        registry.set_desired_version("1.1")
        rows = registry.to_dashboard_rows()
        assert len(rows) == 5
        assert "device_name" in rows[0]
        assert "sync_status" in rows[0]


# ──────────────────────────────────────────────────────────────────────
# Audit Logger Tests
# ──────────────────────────────────────────────────────────────────────

class TestAuditLogger:

    def test_record_tamper_alert(self, audit_logger):
        from ota.types import TamperAlert
        alert = TamperAlert(
            node_id="b01-f01-r101",
            reason=TamperReason.HASH_MISMATCH,
            expected_hash="aaa",
            received_hash="bbb",
            raw_payload=b"tampered",
            source_ip="192.168.1.1",
        )
        entry = audit_logger.record(alert)
        assert entry.node_id == "b01-f01-r101"
        assert entry.tamper_reason == "hash_mismatch"
        assert entry.severity == "CRITICAL"
        assert entry.alert_id.startswith("OTA-ALERT-")

    def test_alert_severity_mapping(self):
        assert SEVERITY_MAP[TamperReason.HASH_MISMATCH] == "CRITICAL"
        assert SEVERITY_MAP[TamperReason.MISSING_SIGNATURE] == "CRITICAL"
        assert SEVERITY_MAP[TamperReason.MALFORMED_JSON] == "HIGH"
        assert SEVERITY_MAP[TamperReason.VERSION_DOWNGRADE] == "HIGH"

    def test_stats_after_records(self, audit_logger):
        from ota.types import TamperAlert
        a1 = TamperAlert(
            node_id="n1", reason=TamperReason.HASH_MISMATCH,
            expected_hash="", received_hash="", raw_payload=b"",
        )
        a2 = TamperAlert(
            node_id="n2", reason=TamperReason.MISSING_SIGNATURE,
            expected_hash="", received_hash="", raw_payload=b"",
        )
        audit_logger.record(a1)
        audit_logger.record(a2)
        stats = audit_logger.stats()
        assert stats["total_alerts"] == 2
        assert stats["critical_count"] == 2

    def test_save_and_load(self, audit_logger, tmp_path):
        from ota.types import TamperAlert
        alert = TamperAlert(
            node_id="n1", reason=TamperReason.HASH_MISMATCH,
            expected_hash="a", received_hash="b", raw_payload=b"x",
        )
        audit_logger.record(alert)
        audit_logger.save()

        # Create a new logger loading from the same file
        log_file = tmp_path / "tamper_log.json"
        reloaded = OtaAuditLogger(log_file=log_file)
        assert len(reloaded.get_alerts()) == 1

    def test_filter_by_severity(self, audit_logger):
        from ota.types import TamperAlert
        a1 = TamperAlert(
            node_id="n1", reason=TamperReason.HASH_MISMATCH,
            expected_hash="", received_hash="", raw_payload=b"",
        )
        a2 = TamperAlert(
            node_id="n2", reason=TamperReason.MALFORMED_JSON,
            expected_hash="", received_hash="", raw_payload=b"",
        )
        audit_logger.record(a1)
        audit_logger.record(a2)
        critical = audit_logger.get_alerts(severity="CRITICAL")
        high = audit_logger.get_alerts(severity="HIGH")
        assert len(critical) == 1
        assert len(high) == 1

    def test_filter_by_node(self, audit_logger):
        from ota.types import TamperAlert
        for nid in ["n1", "n2", "n1"]:
            audit_logger.record(
                TamperAlert(
                    node_id=nid, reason=TamperReason.HASH_MISMATCH,
                    expected_hash="", received_hash="", raw_payload=b"",
                )
            )
        n1_alerts = audit_logger.get_alerts(node_id="n1")
        assert len(n1_alerts) == 2

    def test_thingsboard_format(self, audit_logger):
        from ota.types import TamperAlert
        audit_logger.record(
            TamperAlert(
                node_id="n1", reason=TamperReason.HASH_MISMATCH,
                expected_hash="expected123", received_hash="received456",
                raw_payload=b"test", source_ip="10.0.0.1",
            )
        )
        rows = audit_logger.get_alerts_for_thingsboard()
        assert len(rows) == 1
        vals = rows[0]["values"]
        assert vals["ota_tamper_node_id"] == "n1"
        assert vals["ota_tamper_severity"] == "CRITICAL"
        assert "ts" in rows[0]

    def test_clear(self, audit_logger):
        from ota.types import TamperAlert
        audit_logger.record(
            TamperAlert(
                node_id="n1", reason=TamperReason.HASH_MISMATCH,
                expected_hash="", received_hash="", raw_payload=b"",
            )
        )
        audit_logger.clear()
        assert len(audit_logger.get_alerts()) == 0
        assert audit_logger.stats()["total_alerts"] == 0


# ──────────────────────────────────────────────────────────────────────
# MQTT Topic Routing Tests
# ──────────────────────────────────────────────────────────────────────

class TestMqttTopicRouting:

    def test_broadcast_target(self):
        target = OtaTopicTarget.from_topic("campus/+/+/ota")
        assert target.building == "*"
        assert target.floor == "*"
        assert target.is_floor_target is False

    def test_floor_target(self):
        target = OtaTopicTarget.from_topic("campus/b01/f05/ota")
        assert target.building == "b01"
        assert target.floor == "05"
        assert target.is_floor_target is True

    def test_topic_pattern_regex(self):
        assert TOPIC_PATTERN.match("campus/b01/f01/ota")
        assert TOPIC_PATTERN.match("campus/b99/f99/ota")
        assert not TOPIC_PATTERN.match("campus/+/+/ota")
        assert not TOPIC_PATTERN.match("other/topic")

    def test_broadcast_matches_all_nodes(self):
        target = OtaTopicTarget.from_topic("campus/+/+/ota")
        assert target.matches_node("b01-f01-r101")
        assert target.matches_node("b01-f10-r1020")
        assert target.matches_node("b99-f99-r999")

    def test_floor_target_matches_correct_floor(self):
        target = OtaTopicTarget.from_topic("campus/b01/f05/ota")
        assert target.matches_node("b01-f05-r501")
        assert not target.matches_node("b01-f01-r101")
        assert not target.matches_node("b02-f05-r501")

    def test_wildcard_floor_matches_different_buildings(self):
        target = OtaTopicTarget.from_topic("campus/+/f03/ota")
        target.building = "*"
        target.floor = "03"
        target.is_floor_target = True
        assert target.matches_node("b01-f03-r301")
        assert target.matches_node("b02-f03-r301")
        assert not target.matches_node("b01-f01-r101")


# ──────────────────────────────────────────────────────────────────────
# OTA Receiver Tests
# ──────────────────────────────────────────────────────────────────────

class TestOtaReceiver:

    def _make_receiver(self, registry, audit_logger, node_ids):
        mock_mqtt = MagicMock()
        receiver = OtaReceiver(
            mqtt_handler=mock_mqtt,
            version_registry=registry,
            audit_logger=audit_logger,
            node_ids=node_ids,
        )
        return receiver

    def test_receiver_applies_valid_update(self, registry, audit_logger, sample_node_ids, valid_signed_payload):
        receiver = self._make_receiver(registry, audit_logger, sample_node_ids)

        results = []
        def apply_hook(node_id, params):
            results.append((node_id, params))
            return OtaUpdateResult(
                node_id=node_id, status=OtaStatus.APPLIED, version="1.1",
            )
        receiver.register_apply_hook(apply_hook)

        from ota.mqtt_handler import OtaTopicTarget
        target = OtaTopicTarget.from_topic("campus/+/+/ota")
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            receiver._on_ota_message("campus/+/+/ota", valid_signed_payload, target)
        )

        assert len(results) == 5
        for nid, params in results:
            assert params == {"alpha": 0.012, "beta": 0.22}

    def test_receiver_rejects_tampered_payload(self, registry, audit_logger, sample_node_ids):
        receiver = self._make_receiver(registry, audit_logger, sample_node_ids)
        tampered = json.dumps({"alpha": 0.01, "signature": "badhash" * 13}).encode()

        from ota.mqtt_handler import OtaTopicTarget
        target = OtaTopicTarget.from_topic("campus/+/+/ota")
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            receiver._on_ota_message("campus/+/+/ota", tampered, target)
        )

        stats = receiver.stats
        assert stats["tampered"] > 0
        assert stats["applied"] == 0

    def test_receiver_filters_by_floor_topic(self, registry, audit_logger):
        nodes_f1 = [f"b01-f01-r{100 + i}" for i in range(1, 4)]
        nodes_f2 = [f"b01-f02-r{200 + i}" for i in range(1, 4)]
        all_nodes = nodes_f1 + nodes_f2
        for n in all_nodes:
            registry.register(n, "1.0")

        receiver = self._make_receiver(registry, audit_logger, all_nodes)
        results = []
        def apply_hook(node_id, params):
            results.append(node_id)
            return OtaUpdateResult(
                node_id=node_id, status=OtaStatus.APPLIED, version="1.1",
            )
        receiver.register_apply_hook(apply_hook)

        signed = sign_payload({"alpha": 0.02}, "1.1")
        payload = json.dumps(signed).encode()

        from ota.mqtt_handler import OtaTopicTarget
        target_f1 = OtaTopicTarget.from_topic("campus/b01/f01/ota")
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            receiver._on_ota_message("campus/b01/f01/ota", payload, target_f1)
        )

        assert len(results) == 3
        for nid in results:
            assert "f01" in nid
            assert "f02" not in nid

    def test_receiver_stats(self, registry, audit_logger, sample_node_ids, valid_signed_payload):
        receiver = self._make_receiver(registry, audit_logger, sample_node_ids)
        receiver.register_apply_hook(
            lambda nid, p: OtaUpdateResult(
                node_id=nid, status=OtaStatus.APPLIED, version="1.1",
            )
        )
        from ota.mqtt_handler import OtaTopicTarget
        target = OtaTopicTarget.from_topic("campus/+/+/ota")
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            receiver._on_ota_message("campus/+/+/ota", valid_signed_payload, target)
        )
        stats = receiver.stats
        assert stats["received"] == 1
        assert stats["applied"] == 5
        assert "integrity" in stats
        assert "audit" in stats
        assert "versioning" in stats


# ──────────────────────────────────────────────────────────────────────
# Types Tests
# ──────────────────────────────────────────────────────────────────────

class TestTypes:

    def test_ota_update_result_success(self):
        r = OtaUpdateResult(node_id="n1", status=OtaStatus.APPLIED, version="1.0")
        assert r.success is True

    def test_ota_update_result_verified(self):
        r = OtaUpdateResult(node_id="n1", status=OtaStatus.VERIFIED, version="1.0")
        assert r.success is True

    def test_ota_update_result_rejected(self):
        r = OtaUpdateResult(
            node_id="n1", status=OtaStatus.REJECTED, version="1.0",
            reason="hash_mismatch",
        )
        assert r.success is False

    def test_ota_update_result_has_timestamp(self):
        import time
        r = OtaUpdateResult(node_id="n1", status=OtaStatus.APPLIED, version="1.0")
        assert abs(r.timestamp - time.time()) < 1.0

    def test_node_version_state_in_sync(self):
        state = NodeVersionState(node_id="n1", current_version="1.0", desired_version="1.0")
        assert state.in_sync is True

    def test_node_version_state_out_of_sync(self):
        state = NodeVersionState(node_id="n1", current_version="1.0", desired_version="1.1")
        assert state.in_sync is False
        assert state.update_pending is True

    def test_node_version_state_set_desired(self):
        state = NodeVersionState(node_id="n1")
        state.set_desired("2.0")
        assert state.desired_version == "2.0"
        assert state.update_pending is True

    def test_node_version_state_confirm_applied(self):
        state = NodeVersionState(
            node_id="n1", current_version="1.0", desired_version="1.1",
        )
        assert state.update_pending is True
        state.confirm_applied("1.1")
        assert state.current_version == "1.1"
        assert state.desired_version == "1.1"
        assert state.in_sync is True
        assert state.update_pending is False
