#!/usr/bin/env python3
"""
export_tb_assets.py
===================
Campus Pulse Phase 2 — Nour's Infrastructure Work
Exports all ThingsBoard artifacts to JSON files for repo submission:
  - Rule chain (Campus Pulse Main Logic)
  - Dashboard (Campus Pulse Dashboard)
  - Asset list (hierarchy snapshot)
  - Device list (all 200 nodes)

Run AFTER create_rule_chains.py and create_dashboard.py:
  python3 infrastructure/thingsboard/scripts/export_tb_assets.py

Outputs:
  infrastructure/thingsboard/exports/rule_chain_export.json
  infrastructure/thingsboard/exports/dashboard_export.json
  infrastructure/thingsboard/exports/assets_snapshot.json
  infrastructure/thingsboard/exports/devices_snapshot.json
"""

import json
import os
import sys
import requests

# ── Config ────────────────────────────────────────────────────────────────────
TB_URL      = os.getenv("TB_URL",      "http://localhost:9090")
TB_USER     = os.getenv("TB_USER",     "tenant@thingsboard.org")
TB_PASSWORD = os.getenv("TB_PASSWORD", "tenant")
EXPORT_DIR  = os.path.join(os.path.dirname(__file__), "..", "exports")
# ──────────────────────────────────────────────────────────────────────────────

TARGET_CHAIN_NAME     = "Campus Pulse Main Logic"
TARGET_DASHBOARD_NAME = "Campus Pulse Dashboard"


def login() -> str:
    r = requests.post(f"{TB_URL}/api/auth/login",
                      json={"username": TB_USER, "password": TB_PASSWORD},
                      timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def h(token: str) -> dict:
    return {"X-Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def save(name: str, data: dict) -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, name)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  ✔ {name:35s} ({len(json.dumps(data))//1024 + 1} KB)")


def paginate(token: str, url: str, page_size: int = 100) -> list:
    """Fetch all pages from a TB paginated endpoint."""
    results, page = [], 0
    while True:
        r = requests.get(f"{url}?pageSize={page_size}&page={page}",
                         headers=h(token), timeout=15)
        r.raise_for_status()
        body = r.json()
        results.extend(body.get("data", []))
        if body.get("hasNext", False):
            page += 1
        else:
            break
    return results


def export_rule_chain(token: str) -> None:
    chains = paginate(token, f"{TB_URL}/api/ruleChains")
    target = next((c for c in chains if c["name"] == TARGET_CHAIN_NAME), None)
    if not target:
        print(f"  ⚠ Rule chain '{TARGET_CHAIN_NAME}' not found. Run create_rule_chains.py first.")
        return
    chain_id = target["id"]["id"]
    # CE-compatible: compose from shell + metadata endpoints
    r_chain = requests.get(f"{TB_URL}/api/ruleChain/{chain_id}",
                           headers=h(token), timeout=10)
    r_chain.raise_for_status()
    r_meta = requests.get(f"{TB_URL}/api/ruleChain/{chain_id}/metadata",
                          headers=h(token), timeout=10)
    r_meta.raise_for_status()
    export_obj = {"ruleChain": r_chain.json(), "metadata": r_meta.json()}
    save("rule_chain_export.json", export_obj)


def export_dashboard(token: str) -> None:
    dashboards = paginate(token, f"{TB_URL}/api/tenant/dashboards")
    target = next(
        (d for d in dashboards
         if d.get("title") == TARGET_DASHBOARD_NAME
         or d.get("name") == TARGET_DASHBOARD_NAME),
        None,
    )
    if not target:
        print(f"  ⚠ Dashboard '{TARGET_DASHBOARD_NAME}' not found. Run create_dashboard.py first.")
        return
    db_id = target["id"]["id"]
    r = requests.get(f"{TB_URL}/api/dashboard/{db_id}",
                     headers=h(token), timeout=10)
    r.raise_for_status()
    save("dashboard_export.json", r.json())


def export_assets(token: str) -> None:
    assets = paginate(token, f"{TB_URL}/api/tenant/assets")
    snapshot = [
        {
            "name":    a["name"],
            "type":    a["type"],
            "id":      a["id"]["id"],
        }
        for a in assets
    ]
    snapshot.sort(key=lambda x: (x["type"], x["name"]))
    save("assets_snapshot.json", snapshot)
    print(f"     ({len(snapshot)} assets)")


def export_devices(token: str) -> None:
    devices = paginate(token, f"{TB_URL}/api/tenant/devices")
    snapshot = [
        {
            "name":  d["name"],
            "type":  d["type"],
            "id":    d["id"]["id"],
        }
        for d in devices
    ]
    snapshot.sort(key=lambda x: x["name"])
    save("devices_snapshot.json", snapshot)
    print(f"     ({len(snapshot)} devices)")


def main():
    print("=== Campus Pulse: Export TB Assets ===")
    print(f"Source: {TB_URL}")
    print(f"Output: {os.path.abspath(EXPORT_DIR)}\n")

    token = login()
    print("Authenticated.\n")

    print("Exporting artifacts:")
    export_rule_chain(token)
    export_dashboard(token)
    export_assets(token)
    export_devices(token)

    print("\n✅ All exports complete.")
    print("   Submit the contents of infrastructure/thingsboard/exports/ with your repo.")


if __name__ == "__main__":
    main()
