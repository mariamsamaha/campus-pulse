#!/usr/bin/env python3
"""
create_rule_chains.py
=====================
Campus Pulse Phase 2 — Nour's Infrastructure Work
Deploys the full ThingsBoard Rule Chain for telemetry routing,
threshold alarm (high/low temp), and offline device detection.

Telemetry schema (matches engine.py / room.py):
  temperature   : float (°C)        — alert >30 CRITICAL, <15 WARNING
  humidity      : float (%)
  occupancy     : bool / int
  light_level   : float (lux)
  hvac_mode     : str
  active        : bool

Run:
  python3 infrastructure/thingsboard/scripts/create_rule_chains.py

Output:
  infrastructure/thingsboard/exports/rule_chain_export.json
"""

import json
import os
import sys
import requests

# ── Config ────────────────────────────────────────────────────────────────────
TB_URL      = os.getenv("TB_URL",      "http://localhost:9090")
TB_USER     = os.getenv("TB_USER",     "tenant@thingsboard.org")
TB_PASSWORD = os.getenv("TB_PASSWORD", "tenant")
EXPORT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "exports", "rule_chain_export.json"
)
CHAIN_NAME  = "Campus Pulse Main Logic"
# ──────────────────────────────────────────────────────────────────────────────


def login() -> str:
    r = requests.post(f"{TB_URL}/api/auth/login",
                      json={"username": TB_USER, "password": TB_PASSWORD},
                      timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def headers(token: str) -> dict:
    return {"X-Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def delete_existing_chain(token: str, name: str) -> None:
    """Remove any non-root chain with the given name so we start fresh."""
    r = requests.get(f"{TB_URL}/api/ruleChains?pageSize=50&page=0",
                     headers=headers(token), timeout=10)
    for chain in r.json().get("data", []):
        if chain["name"] == name and not chain.get("root", False):
            cid = chain["id"]["id"]
            requests.delete(f"{TB_URL}/api/ruleChain/{cid}",
                            headers=headers(token), timeout=10)
            print(f"  Deleted old chain: {name} ({cid})")


def create_rule_chain(token: str) -> str:
    """Create the rule chain shell and return its ID."""
    payload = {
        "name": CHAIN_NAME,
        "type": "CORE",
        "debugMode": True,
        "root": False,
    }
    r = requests.post(f"{TB_URL}/api/ruleChain",
                      headers=headers(token), json=payload, timeout=10)
    r.raise_for_status()
    chain_id = r.json()["id"]["id"]
    print(f"  Created rule chain: {CHAIN_NAME} ({chain_id})")
    return chain_id


def deploy_metadata(token: str, chain_id: str) -> None:
    """Deploy all rule nodes and their wiring to the chain."""

    # ── Node definitions ──────────────────────────────────────────────────────
    # Each node needs an (x, y) for the visual editor, plus its config.
    # Connections reference nodes by their 0-based index in this list.
    #
    # Node index map:
    #  0 = Message Type Switch   — entry point, routes by msg type
    #  1 = Save Timeseries       — persists telemetry to DB
    #  2 = High Temp Filter      — JS: temperature > 30
    #  3 = Create High Temp Alarm (CRITICAL)
    #  4 = Clear High Temp Alarm — when temp is back in range
    #  5 = Low Temp Filter       — JS: temperature < 15
    #  6 = Create Low Temp Alarm (WARNING)
    #  7 = Clear Low Temp Alarm
    #  8 = Offline / Inactivity Alarm (MAJOR)
    #  9 = Clear Inactive Alarm  — on device ACTIVITY event

    nodes = [
        # 0 — Message Type Switch (entry)
        {
            "type": "org.thingsboard.rule.engine.filter.TbMsgTypeSwitchNode",
            "name": "Message Type Switch",
            "debugMode": True,
            "configuration": {"version": 0},
            "additionalInfo": {
                "description": "Routes messages by type: POST_TELEMETRY, ACTIVITY, INACTIVITY",
                "layoutX": 200, "layoutY": 300,
            },
        },
        # 1 — Save Timeseries
        {
            "type": "org.thingsboard.rule.engine.telemetry.TbMsgTimeseriesNode",
            "name": "Save Telemetry",
            "debugMode": True,
            "configuration": {"defaultTTL": 0, "skipLatestPersistence": False},
            "additionalInfo": {
                "description": "Persists all received telemetry to the time-series DB",
                "layoutX": 500, "layoutY": 150,
            },
        },
        # 2 — High Temperature JS Filter
        {
            "type": "org.thingsboard.rule.engine.filter.TbJsFilterNode",
            "name": "High Temp Filter (>30°C)",
            "debugMode": True,
            "configuration": {
                "jsScript": (
                    "return typeof msg.temperature !== 'undefined' "
                    "&& msg.temperature > 30;"
                )
            },
            "additionalInfo": {
                "description": "Passes message if temperature exceeds 30 °C",
                "layoutX": 800, "layoutY": 50,
            },
        },
        # 3 — Create High Temp Alarm (CRITICAL)
        {
            "type": "org.thingsboard.rule.engine.action.TbCreateAlarmNode",
            "name": "Create High Temp Alarm",
            "debugMode": True,
            "configuration": {
                "alarmType": "High Temperature",
                "severity": "CRITICAL",
                "propagate": True,
                "dynamicSeverity": False,
                "alarmDetailsBuildJs": (
                    "var details = {};\n"
                    "if (metadata.prevAlarmDetails) {\n"
                    "  details = JSON.parse(metadata.prevAlarmDetails);\n"
                    "}\n"
                    "details.temperature = msg.temperature;\n"
                    "details.threshold = 30;\n"
                    "return JSON.stringify(details);"
                ),
            },
            "additionalInfo": {
                "description": "Creates a CRITICAL alarm when temperature > 30 °C",
                "layoutX": 1100, "layoutY": 50,
            },
        },
        # 4 — Clear High Temp Alarm (temp returned to normal)
        {
            "type": "org.thingsboard.rule.engine.action.TbClearAlarmNode",
            "name": "Clear High Temp Alarm",
            "debugMode": True,
            "configuration": {
                "alarmType": "High Temperature",
                "alarmDetailsBuildJs": "return JSON.stringify({cleared: true});",
            },
            "additionalInfo": {
                "description": "Clears the High Temperature alarm when temp drops back to normal",
                "layoutX": 1100, "layoutY": 150,
            },
        },
        # 5 — Low Temperature JS Filter
        {
            "type": "org.thingsboard.rule.engine.filter.TbJsFilterNode",
            "name": "Low Temp Filter (<15°C)",
            "debugMode": True,
            "configuration": {
                "jsScript": (
                    "return typeof msg.temperature !== 'undefined' "
                    "&& msg.temperature < 15;"
                )
            },
            "additionalInfo": {
                "description": "Passes message if temperature drops below 15 °C",
                "layoutX": 800, "layoutY": 300,
            },
        },
        # 6 — Create Low Temp Alarm (WARNING)
        {
            "type": "org.thingsboard.rule.engine.action.TbCreateAlarmNode",
            "name": "Create Low Temp Alarm",
            "debugMode": True,
            "configuration": {
                "alarmType": "Low Temperature",
                "severity": "WARNING",
                "propagate": True,
                "dynamicSeverity": False,
                "alarmDetailsBuildJs": (
                    "var details = {};\n"
                    "if (metadata.prevAlarmDetails) {\n"
                    "  details = JSON.parse(metadata.prevAlarmDetails);\n"
                    "}\n"
                    "details.temperature = msg.temperature;\n"
                    "details.threshold = 15;\n"
                    "return JSON.stringify(details);"
                ),
            },
            "additionalInfo": {
                "description": "Creates a WARNING alarm when temperature < 15 °C",
                "layoutX": 1100, "layoutY": 300,
            },
        },
        # 7 — Clear Low Temp Alarm
        {
            "type": "org.thingsboard.rule.engine.action.TbClearAlarmNode",
            "name": "Clear Low Temp Alarm",
            "debugMode": True,
            "configuration": {
                "alarmType": "Low Temperature",
                "alarmDetailsBuildJs": "return JSON.stringify({cleared: true});",
            },
            "additionalInfo": {
                "description": "Clears the Low Temperature alarm when temp returns to normal",
                "layoutX": 1100, "layoutY": 400,
            },
        },
        # 8 — Create Offline / Inactivity Alarm (MAJOR)
        {
            "type": "org.thingsboard.rule.engine.action.TbCreateAlarmNode",
            "name": "Device Offline Alarm",
            "debugMode": True,
            "configuration": {
                "alarmType": "Device Offline",
                "severity": "MAJOR",
                "propagate": True,
                "dynamicSeverity": False,
                "alarmDetailsBuildJs": (
                    "return JSON.stringify({event: 'INACTIVITY', "
                    "deviceName: metadata.deviceName});"
                ),
            },
            "additionalInfo": {
                "description": "Creates a MAJOR alarm when a device sends an INACTIVITY event",
                "layoutX": 500, "layoutY": 500,
            },
        },
        # 9 — Clear Offline Alarm (device came back online)
        {
            "type": "org.thingsboard.rule.engine.action.TbClearAlarmNode",
            "name": "Clear Offline Alarm",
            "debugMode": True,
            "configuration": {
                "alarmType": "Device Offline",
                "alarmDetailsBuildJs": (
                    "return JSON.stringify({event: 'ACTIVITY', cleared: true});"
                ),
            },
            "additionalInfo": {
                "description": "Clears the Device Offline alarm when activity resumes",
                "layoutX": 500, "layoutY": 620,
            },
        },
    ]

    # ── Wire connections ───────────────────────────────────────────────────────
    #
    # Connection types must match the branch label the *source* node emits.
    #
    # MsgTypeSwitch outputs:
    #   "Post telemetry" → Save Timeseries
    #   "Inactivity Event" → Device Offline Alarm
    #   "Activity Event"   → Clear Offline Alarm
    #
    # Save Timeseries "Success" fans out to both temp filters.
    #
    # High Temp Filter:
    #   "True"  → Create High Temp Alarm
    #   "False" → Clear High Temp Alarm
    #
    # Low Temp Filter:
    #   "True"  → Create Low Temp Alarm
    #   "False" → Clear Low Temp Alarm

    connections = [
        # 0 (Switch) → 1 (Save Telemetry) on "Post telemetry"
        {"fromIndex": 0, "toIndex": 1, "type": "Post telemetry"},
        # 0 (Switch) → 8 (Offline Alarm) on "Inactivity Event"
        {"fromIndex": 0, "toIndex": 8, "type": "Inactivity Event"},
        # 0 (Switch) → 9 (Clear Offline) on "Activity Event"
        {"fromIndex": 0, "toIndex": 9, "type": "Activity Event"},
        # 1 (Save Telemetry) → 2 (High Temp Filter)
        {"fromIndex": 1, "toIndex": 2, "type": "Success"},
        # 1 (Save Telemetry) → 5 (Low Temp Filter)
        {"fromIndex": 1, "toIndex": 5, "type": "Success"},
        # 2 (High Temp Filter True) → 3 (Create High Temp Alarm)
        {"fromIndex": 2, "toIndex": 3, "type": "True"},
        # 2 (High Temp Filter False) → 4 (Clear High Temp Alarm)
        {"fromIndex": 2, "toIndex": 4, "type": "False"},
        # 5 (Low Temp Filter True) → 6 (Create Low Temp Alarm)
        {"fromIndex": 5, "toIndex": 6, "type": "True"},
        # 5 (Low Temp Filter False) → 7 (Clear Low Temp Alarm)
        {"fromIndex": 5, "toIndex": 7, "type": "False"},
    ]

    metadata = {
        "ruleChainId": {"entityType": "RULE_CHAIN", "id": chain_id},
        "firstNodeIndex": 0,
        "nodes": nodes,
        "connections": connections,
    }

    r = requests.post(
        f"{TB_URL}/api/ruleChain/metadata",
        headers=headers(token),
        json=metadata,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"  ERROR deploying metadata: {r.status_code} — {r.text[:500]}")
        sys.exit(1)
    print(f"  Deployed {len(nodes)} nodes and {len(connections)} connections.")


def export_chain(token: str, chain_id: str) -> dict:
    """Compose a portable export artifact from the two CE REST endpoints.
    TB v4 CE does not expose /api/ruleChain/{id}/export (that is PE-only).
    We mimic the same structure ThingsBoard uses internally.
    """
    r_chain = requests.get(f"{TB_URL}/api/ruleChain/{chain_id}",
                           headers=headers(token), timeout=10)
    r_chain.raise_for_status()

    r_meta = requests.get(f"{TB_URL}/api/ruleChain/{chain_id}/metadata",
                          headers=headers(token), timeout=10)
    r_meta.raise_for_status()

    return {
        "ruleChain": r_chain.json(),
        "metadata":  r_meta.json(),
    }


def save_export(data: dict) -> None:
    os.makedirs(os.path.dirname(EXPORT_PATH), exist_ok=True)
    with open(EXPORT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Export saved → {EXPORT_PATH}")


def main():
    print("=== Campus Pulse: Deploy Rule Chains ===")
    print(f"Target: {TB_URL}")

    print("\n[1/4] Authenticating...")
    token = login()
    print("  OK")

    print(f"\n[2/4] Removing stale '{CHAIN_NAME}' chain if exists...")
    delete_existing_chain(token, CHAIN_NAME)

    print(f"\n[3/4] Creating rule chain + deploying all nodes/connections...")
    chain_id = create_rule_chain(token)
    deploy_metadata(token, chain_id)

    print("\n[4/4] Exporting and saving JSON artifact...")
    export_data = export_chain(token, chain_id)
    save_export(export_data)

    print("\n✅ Rule chain deployment complete.")
    print(f"   Chain ID : {chain_id}")
    print(f"   Export   : {EXPORT_PATH}")
    print("\nNode summary:")
    print("  0  Message Type Switch      → routes by msg type")
    print("  1  Save Telemetry           → persists to time-series DB")
    print("  2  High Temp Filter >30°C   → JS boolean filter")
    print("  3  Create High Temp Alarm   → CRITICAL")
    print("  4  Clear High Temp Alarm    → when temp normal again")
    print("  5  Low Temp Filter <15°C    → JS boolean filter")
    print("  6  Create Low Temp Alarm    → WARNING")
    print("  7  Clear Low Temp Alarm     → when temp normal again")
    print("  8  Device Offline Alarm     → MAJOR, on INACTIVITY")
    print("  9  Clear Offline Alarm      → on ACTIVITY event")


if __name__ == "__main__":
    main()
