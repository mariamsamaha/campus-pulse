from __future__ import annotations
import csv
import sys
from pathlib import Path

BUILDING_ID = "b01"
NUM_FLOORS  = 10

DEVICES_CSV = Path("tb_devices.csv")
ASSETS_CSV  = Path("tb_assets.csv")

def room_to_ids(floor: int, room_num: int) -> tuple[str, str, str]:
    """Return (node_id, mqtt_topic_or_coap_uri, profile)."""
    floor_id   = f"f{floor:02d}"
    room_id_num = f"r{floor}{room_num:02d}"
    node_id    = f"{BUILDING_ID}-{floor_id}-{room_id_num}"
    is_mqtt    = room_num <= 10

    if is_mqtt:
        endpoint = f"campus/{BUILDING_ID}/{floor_id}/{room_id_num}/telemetry"
        profile  = "MQTT-ThermalSensor"
        protocol = "MQTT"
        lwt      = f"campus/{BUILDING_ID}/{floor_id}/{room_id_num}/status"
        cmd_topic = f"campus/{BUILDING_ID}/{floor_id}/{room_id_num}/cmd"
        port     = ""
    else:
        room_offset = room_num - 11
        port = 5683 + (floor - 1) * 10 + room_offset
        endpoint = f"coap://127.0.0.1:{port}/{floor_id}/{room_id_num}/telemetry"
        profile  = "CoAP-ThermalSensor"
        protocol = "CoAP"
        lwt      = ""
        cmd_topic = f"coap://127.0.0.1:{port}/{floor_id}/{room_id_num}/actuators/hvac"

    return node_id, endpoint, profile, protocol, lwt, cmd_topic, str(port)


def main():
    devices = []
    assets  = []

    assets.append({
        "asset_name": "Campus",
        "asset_type": "Campus",
        "parent": "",
        "label": "Main Campus",
    })

    assets.append({
        "asset_name": BUILDING_ID,
        "asset_type": "Building",
        "parent": "Campus",
        "label": "Building 01",
    })

    for floor in range(1, NUM_FLOORS + 1):
        floor_id = f"f{floor:02d}"
        floor_name = f"{BUILDING_ID}-{floor_id}"

        assets.append({
            "asset_name": floor_name,
            "asset_type": "Floor",
            "parent": BUILDING_ID,
            "label": f"Floor {floor:02d}",
        })

        for room_num in range(1, 21):  # 1-10 MQTT, 11-20 CoAP
            room_id_num = f"r{floor}{room_num:02d}"
            room_name   = f"{BUILDING_ID}-{floor_id}-{room_id_num}"
            node_id, endpoint, profile, protocol, lwt, cmd, port = room_to_ids(floor, room_num)

            # Room asset
            assets.append({
                "asset_name": room_name,
                "asset_type": "Room",
                "parent": floor_name,
                "label": f"Room {floor}{room_num:02d}",
            })

            # Device entry
            devices.append({
                "device_name":   node_id,
                "device_label":  f"{'MQTT' if room_num<=10 else 'CoAP'} Sensor - {room_name}",
                "device_profile": profile,
                "protocol":      protocol,
                "floor":         floor,
                "room_number":   room_num,
                "endpoint":      endpoint,
                "lwt_topic":     lwt,
                "cmd_topic":     cmd,
                "coap_port":     port,
                "linked_room":   room_name,
                "credentials":   f"psw_{node_id.replace('-', '_')}",  # PSK placeholder
            })

    # Write devices CSV
    with DEVICES_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=devices[0].keys())
        w.writeheader()
        w.writerows(devices)

    # Write assets CSV
    with ASSETS_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=assets[0].keys())
        w.writeheader()
        w.writerows(assets)

    mqtt_count = sum(1 for d in devices if d["protocol"] == "MQTT")
    coap_count = sum(1 for d in devices if d["protocol"] == "CoAP")

    print(f" Generated {DEVICES_CSV} — {len(devices)} devices ({mqtt_count} MQTT + {coap_count} CoAP)")
    print(f"Generated {ASSETS_CSV}  — {len(assets)} assets (Campus / Building / Floor / Room)")

if __name__ == "__main__":
    main()
