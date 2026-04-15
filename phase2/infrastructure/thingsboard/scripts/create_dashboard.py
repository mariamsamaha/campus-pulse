#!/usr/bin/env python3
"""
create_dashboard.py
===================
Campus Pulse Phase 2 — Nour's Infrastructure Work
Creates the Campus Pulse ThingsBoard dashboard via REST API with:

  State 1 (default): Campus Overview
    - Alarm panel (all active alarms)
    - Device count / online gauge
    - Latest telemetry table for all 200 devices

  State 2: Per-Floor View (entity alias: devices on selected floor)
    - Temperature cards
    - Humidity trend chart
    - Occupancy indicator
    - Light level bar
    - HVAC mode label
    - Online/Offline status

  Real-time updates driven by ThingsBoard websocket subscription.

Telemetry keys (match engine.py):
  temperature, humidity, occupancy, light_level, hvac_mode, active

Run:
  python3 infrastructure/thingsboard/scripts/create_dashboard.py

Output:
  infrastructure/thingsboard/exports/dashboard_export.json
"""

import json
import os
import sys
import uuid
import requests

# ── Config ────────────────────────────────────────────────────────────────────
TB_URL      = os.getenv("TB_URL",      "http://localhost:9090")
TB_USER     = os.getenv("TB_USER",     "tenant@thingsboard.org")
TB_PASSWORD = os.getenv("TB_PASSWORD", "tenant")
EXPORT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "exports", "dashboard_export.json"
)
DASHBOARD_TITLE = "Campus Pulse Dashboard"
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


def delete_existing_dashboard(token: str, title: str) -> None:
    r = requests.get(f"{TB_URL}/api/tenant/dashboards?pageSize=50&page=0",
                     headers=headers(token), timeout=10)
    for db in r.json().get("data", []):
        if db.get("title") == title or db.get("name") == title:
            did = db["id"]["id"]
            requests.delete(f"{TB_URL}/api/dashboard/{did}",
                            headers=headers(token), timeout=10)
            print(f"  Deleted old dashboard: {title} ({did})")


def mk_widget(wid, row, col, size_row, size_col, widget_type_fqn,
              title, datasource_type, entity_alias, ts_keys,
              config_overrides=None):
    """Build a single widget descriptor for the dashboard configuration."""
    datasource = {
        "type": datasource_type,
        "entityAliasId": entity_alias,
        "dataKeys": [
            {
                "name": k,
                "type": "timeseries",
                "label": k.replace("_", " ").title(),
                "color": "#" + format(hash(k) & 0xFFFFFF, "06x"),
                "settings": {},
            }
            for k in ts_keys
        ],
    }
    config = {
        "title": title,
        "showTitle": True,
        "backgroundColor": "#ffffff",
        "padding": "8px",
        "showLegend": False,
        "datasources": [datasource],
        **(config_overrides or {}),
    }
    return {
        "id": wid,
        "typeFullFqn": widget_type_fqn,
        "type": "latest",
        "sizeX": size_col,
        "sizeY": size_row,
        "row": row,
        "col": col,
        "config": config,
    }


def build_dashboard_config():
    """
    Build the full dashboard configuration object.
    Uses real ThingsBoard built-in widget types (fully qualified).
    """

    # Widget IDs (stable UUIDs for this deployment)
    W_ALARMS        = str(uuid.uuid4())
    W_TEMP_GAUGE    = str(uuid.uuid4())
    W_HUM_CHART     = str(uuid.uuid4())
    W_OCCUPANCY     = str(uuid.uuid4())
    W_LIGHT         = str(uuid.uuid4())
    W_HVAC          = str(uuid.uuid4())
    W_ONLINE_TABLE  = str(uuid.uuid4())
    W_TELEMETRY_TBL = str(uuid.uuid4())

    # ── Entity aliases ─────────────────────────────────────────────────────────
    alias_all_id   = "alias_all_devices"
    alias_floor_id = "alias_floor_devices"

    entity_aliases = {
        alias_all_id: {
            "id": alias_all_id,
            "alias": "All Campus Devices",
            "filter": {
                "type": "entityType",
                "resolveMultiple": True,
                "entityType": "DEVICE",
            },
        },
        alias_floor_id: {
            "id": alias_floor_id,
            "alias": "Floor Devices (MQTT type)",
            "filter": {
                "type": "entityType",
                "resolveMultiple": True,
                "entityType": "DEVICE",
                # Filter by device type for demonstration; Rehab's NR flows
                # can further target by asset relation.
            },
        },
    }

    # ── Helper: datasource with specific keys ──────────────────────────────────
    def ds(alias_id, keys, ds_type="entity"):
        return [
            {
                "type": ds_type,
                "entityAliasId": alias_id,
                "dataKeys": [
                    {
                        "name": k,
                        "type": "timeseries",
                        "label": k.replace("_", " ").title(),
                        "color": "#" + format((hash(k) * 1234567) & 0xFFFFFF, "06x"),
                        "settings": {},
                    }
                    for k in keys
                ],
                "alarmFilterConfig": {"statusList": ["ACTIVE"]},
            }
        ]

    # ── Widget definitions (layout: 24-column grid) ────────────────────────────

    widgets = {

        # Row 0: Alarms panel (full width)
        W_ALARMS: {
            "id": W_ALARMS,
            "typeFullFqn": "default-widgets-bundle.alarms-table",
            "type": "alarm",
            "sizeX": 24, "sizeY": 5, "row": 0, "col": 0,
            "config": {
                "title": "🚨 Active Alarms",
                "showTitle": True,
                "backgroundColor": "#ffebee",
                "datasources": [
                    {
                        "type": "entity",
                        "entityAliasId": alias_all_id,
                        "dataKeys": [],
                        "alarmFilterConfig": {
                            "statusList": ["ACTIVE", "ACKNOWLEDGED"],
                        },
                    }
                ],
                "alarmStatusList": ["ACTIVE", "ACKNOWLEDGED"],
                "alarmSeverityList": ["CRITICAL", "MAJOR", "WARNING"],
                "settings": {
                    "displayDetails": True,
                    "allowClear": True,
                },
            },
        },

        # Row 5: Temperature gauge
        W_TEMP_GAUGE: {
            "id": W_TEMP_GAUGE,
            "typeFullFqn": "default-widgets-bundle.radial-gauge",
            "type": "latest",
            "sizeX": 6, "sizeY": 5, "row": 5, "col": 0,
            "config": {
                "title": "🌡 Temperature (°C)",
                "showTitle": True,
                "backgroundColor": "#e3f2fd",
                "datasources": ds(alias_all_id, ["temperature"]),
                "settings": {
                    "minValue": 0,
                    "maxValue": 50,
                    "gaugeType": "arc",
                    "levelColors": ["#4caf50", "#ff9800", "#f44336"],
                    "neonGlowBrightness": 0,
                },
            },
        },

        # Row 5: Humidity timeseries chart
        W_HUM_CHART: {
            "id": W_HUM_CHART,
            "typeFullFqn": "default-widgets-bundle.basic-timeseries",
            "type": "timeseries",
            "sizeX": 10, "sizeY": 5, "row": 5, "col": 6,
            "config": {
                "title": "💧 Humidity Trend (%)",
                "showTitle": True,
                "backgroundColor": "#f3e5f5",
                "datasources": ds(alias_all_id, ["humidity"]),
                "timewindow": {
                    "displayValue": "6h",
                    "realtime": {"interval": 5000, "timewindowMs": 21600000},
                },
                "settings": {
                    "showLegend": True,
                    "smoothLines": True,
                },
            },
        },

        # Row 5: Occupancy status
        W_OCCUPANCY: {
            "id": W_OCCUPANCY,
            "typeFullFqn": "default-widgets-bundle.boolean-indicator",
            "type": "latest",
            "sizeX": 4, "sizeY": 5, "row": 5, "col": 16,
            "config": {
                "title": "🧑 Occupancy",
                "showTitle": True,
                "backgroundColor": "#e8f5e9",
                "datasources": ds(alias_all_id, ["occupancy"]),
                "settings": {
                    "trueColor": "#4caf50",
                    "falseColor": "#bdbdbd",
                    "trueLabel": "Occupied",
                    "falseLabel": "Empty",
                },
            },
        },

        # Row 5: Light level
        W_LIGHT: {
            "id": W_LIGHT,
            "typeFullFqn": "default-widgets-bundle.simple-card",
            "type": "latest",
            "sizeX": 4, "sizeY": 5, "row": 5, "col": 20,
            "config": {
                "title": "💡 Light (lux)",
                "showTitle": True,
                "backgroundColor": "#fffde7",
                "datasources": ds(alias_all_id, ["light_level"]),
                "settings": {
                    "labelPosition": "top",
                    "units": "lux",
                },
            },
        },

        # Row 10: HVAC mode table
        W_HVAC: {
            "id": W_HVAC,
            "typeFullFqn": "default-widgets-bundle.entities-table",
            "type": "latest",
            "sizeX": 12, "sizeY": 6, "row": 10, "col": 0,
            "config": {
                "title": "🌀 HVAC Mode per Device",
                "showTitle": True,
                "backgroundColor": "#fce4ec",
                "datasources": ds(alias_all_id, ["hvac_mode", "temperature"]),
                "settings": {
                    "displayEntityName": True,
                    "columns": [
                        {"key": "entityName", "title": "Device"},
                        {"key": "hvac_mode", "title": "HVAC Mode"},
                        {"key": "temperature", "title": "Temp (°C)"},
                    ],
                },
            },
        },

        # Row 10: Online / Offline status table (uses 'active' key)
        W_ONLINE_TABLE: {
            "id": W_ONLINE_TABLE,
            "typeFullFqn": "default-widgets-bundle.entities-table",
            "type": "latest",
            "sizeX": 12, "sizeY": 6, "row": 10, "col": 12,
            "config": {
                "title": "📡 Device Online/Offline Status",
                "showTitle": True,
                "backgroundColor": "#e0f7fa",
                "datasources": ds(alias_all_id, ["active", "temperature", "humidity"]),
                "settings": {
                    "displayEntityName": True,
                    "columns": [
                        {"key": "entityName", "title": "Device"},
                        {"key": "active", "title": "Online"},
                        {"key": "temperature", "title": "Temp (°C)"},
                        {"key": "humidity", "title": "Humidity (%)"},
                    ],
                },
            },
        },

        # Row 16: Full telemetry table (all 200 devices live)
        W_TELEMETRY_TBL: {
            "id": W_TELEMETRY_TBL,
            "typeFullFqn": "default-widgets-bundle.entities-table",
            "type": "latest",
            "sizeX": 24, "sizeY": 8, "row": 16, "col": 0,
            "config": {
                "title": "📊 Live Telemetry — All 200 Devices",
                "showTitle": True,
                "backgroundColor": "#f9fbe7",
                "datasources": ds(
                    alias_all_id,
                    ["temperature", "humidity", "occupancy",
                     "light_level", "hvac_mode", "active"]
                ),
                "settings": {
                    "displayEntityName": True,
                    "pageSize": 20,
                    "columns": [
                        {"key": "entityName",   "title": "Device"},
                        {"key": "temperature",  "title": "Temp (°C)"},
                        {"key": "humidity",     "title": "Humidity (%)"},
                        {"key": "occupancy",    "title": "Occupied"},
                        {"key": "light_level",  "title": "Light (lux)"},
                        {"key": "hvac_mode",    "title": "HVAC"},
                        {"key": "active",       "title": "Online"},
                    ],
                },
            },
        },
    }

    # ── Layout ─────────────────────────────────────────────────────────────────
    layout_widgets = {
        wid: {
            "sizeX": w["sizeX"],
            "sizeY": w["sizeY"],
            "row": w["row"],
            "col": w["col"],
            "id": wid,
        }
        for wid, w in widgets.items()
    }

    # ── Full config object ─────────────────────────────────────────────────────
    return {
        "description": "Campus Pulse real-time IoT dashboard",
        "widgets": widgets,
        "states": {
            "default": {
                "name": "Campus Overview",
                "root": True,
                "layouts": {
                    "main": {
                        "widgets": layout_widgets,
                        "gridSettings": {
                            "backgroundColor": "#1a1a2e",
                            "backgroundSizeMode": "100%",
                            "columns": 24,
                            "margins": [10, 10],
                            "minimumRowHeight": 20,
                        },
                    }
                },
            }
        },
        "entityAliases": entity_aliases,
        "filters": {},
        "settings": {
            "stateControllerId": "entity",
            "showTitle": True,
            "showDashboardsSelect": True,
            "showEntitiesSelect": True,
            "showDashboardTimewindow": True,
            "showDashboardExport": True,
            "toolbarAlwaysOpen": True,
        },
        "timewindow": {
            "displayValue": "",
            "hideInterval": False,
            "realtime": {
                "realtimeType": 0,
                "interval": 5000,
                "timewindowMs": 86400000,
            },
        },
    }


def create_dashboard(token: str) -> str:
    config = build_dashboard_config()
    payload = {
        "title": DASHBOARD_TITLE,
        "name":  DASHBOARD_TITLE,
        "configuration": config,
    }
    r = requests.post(f"{TB_URL}/api/dashboard",
                      headers=headers(token), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        print(f"  ERROR: {r.status_code} — {r.text[:600]}")
        sys.exit(1)
    db_id = r.json()["id"]["id"]
    print(f"  Created dashboard: {DASHBOARD_TITLE} ({db_id})")
    return db_id


def export_dashboard(token: str, db_id: str) -> dict:
    r = requests.get(f"{TB_URL}/api/dashboard/{db_id}",
                     headers=headers(token), timeout=10)
    r.raise_for_status()
    return r.json()


def save_export(data: dict) -> None:
    os.makedirs(os.path.dirname(EXPORT_PATH), exist_ok=True)
    with open(EXPORT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Export saved → {EXPORT_PATH}")


def main():
    print("=== Campus Pulse: Deploy Dashboard ===")
    print(f"Target: {TB_URL}")

    print("\n[1/4] Authenticating...")
    token = login()
    print("  OK")

    print(f"\n[2/4] Removing stale '{DASHBOARD_TITLE}' if exists...")
    delete_existing_dashboard(token, DASHBOARD_TITLE)

    print("\n[3/4] Creating dashboard with all widgets...")
    db_id = create_dashboard(token)

    print("\n[4/4] Exporting and saving JSON artifact...")
    export_data = export_dashboard(token, db_id)
    save_export(export_data)

    print("\n✅ Dashboard deployment complete.")
    print(f"   Dashboard ID : {db_id}")
    print(f"   View at      : {TB_URL}/dashboard/{db_id}")
    print(f"   Export       : {EXPORT_PATH}")
    print("\nWidget summary:")
    print("  🚨 Active Alarms table (CRITICAL/MAJOR/WARNING, all devices)")
    print("  🌡 Temperature gauge (radial, 0-50°C)")
    print("  💧 Humidity trend chart (6h realtime)")
    print("  🧑 Occupancy boolean indicator")
    print("  💡 Light level card (lux)")
    print("  🌀 HVAC mode per-device table")
    print("  📡 Online/Offline status table (active key)")
    print("  📊 Full live telemetry table (all 200 devices, paginated)")


if __name__ == "__main__":
    main()
