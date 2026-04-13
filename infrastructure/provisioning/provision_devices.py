import requests
import json
import time

# ThingsBoard Configuration
TB_HOST = "localhost"
TB_PORT = "9090"
URL = f"http://{TB_HOST}:{TB_PORT}"
USERNAME = "tenant@thingsboard.org"
PASSWORD = "tenant"

# Simulation Configuration (matches settings.yml)
NUM_FLOORS = 10
ROOMS_PER_FLOOR = 20
BUILDING_ID = "b01"

def get_token():
    payload = {"username": USERNAME, "password": PASSWORD}
    response = requests.post(f"{URL}/api/auth/login", json=payload)
    response.raise_for_status()
    return response.json()["token"]

def create_asset(token, name, asset_type, parent_id=None):
    payload = {"name": name, "type": asset_type}
    headers = {"X-Authorization": f"Bearer {token}"}
    response = requests.post(f"{URL}/api/asset", json=payload, headers=headers)
    response.raise_for_status()
    asset = response.json()
    
    if parent_id:
        relation = {
            "from": {"id": parent_id["id"], "entityType": parent_id["entityType"]},
            "to": {"id": asset["id"]["id"], "entityType": "ASSET"},
            "type": "Contains",
            "typeGroup": "COMMON"
        }
        requests.post(f"{URL}/api/relation", json=relation, headers=headers).raise_for_status()
    
    return asset["id"]

def create_device(token, name, device_type, parent_id=None):
    payload = {"name": name, "type": device_type}
    headers = {"X-Authorization": f"Bearer {token}"}
    response = requests.post(f"{URL}/api/device", json=payload, headers=headers)
    response.raise_for_status()
    device = response.json()
    
    if parent_id:
        relation = {
            "from": {"id": parent_id["id"], "entityType": parent_id["entityType"]},
            "to": {"id": device["id"]["id"], "entityType": "DEVICE"},
            "type": "Contains",
            "typeGroup": "COMMON"
        }
        requests.post(f"{URL}/api/relation", json=relation, headers=headers).raise_for_status()
    
    return device["id"]

def main():
    print("Connecting to ThingsBoard...")
    try:
        token = get_token()
    except Exception as e:
        print(f"Error: Could not connect to ThingsBoard at {URL}. Is it running?")
        print(e)
        return

    print("Building Asset Hierarchy...")
    campus_id = create_asset(token, "Smart Campus", "Campus")
    building_id = create_asset(token, f"Building {BUILDING_ID.upper()}", "Building", campus_id)

    device_count = 0
    for f in range(NUM_FLOORS):
        print(f"  Provisioning Floor {f}...")
        floor_id = create_asset(token, f"Floor {f}", "Floor", building_id)
        
        # Protocol choice: MQTT for floors 0-4, CoAP for 5-9
        protocol = "MQTT" if f < 5 else "CoAP"
        
        for r in range(ROOMS_PER_FLOOR):
            room_name = f"Room {f}-{r:02d}"
            room_id = create_asset(token, room_name, "Room", floor_id)
            
            device_name = f"node-{f}-{r:02d}"
            create_device(token, device_name, protocol, room_id)
            device_count += 1

    print(f"Finished! Provisioned {device_count} devices and asset hierarchy.")

if __name__ == "__main__":
    main()
