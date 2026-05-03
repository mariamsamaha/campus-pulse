from room import Room

# Create one room
r = Room("b01", 5, 2)
print(r)

# Simulate 5 ticks
import time
sim_clock = 12 * 3600  # noon
for i in range(5):
    r.apply_physics(sim_clock + i * 5)
    r.apply_environmental_correlations()
    r.validate_state()
    print(f"Tick {i+1}: {r}")

# Test shadow state
r.receive_desired_state({"hvac_mode": "ECO", "target_temp": 24.0})
r.apply_desired_state()
print("Shadow:", r.shadow_payload())

# Test OTA
r.apply_ota_config({"version": "v1.1", "alpha": 0.015})
print("OTA ack:", r.ota_ack_payload(["alpha=0.015"]))