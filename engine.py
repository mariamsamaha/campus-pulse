import asyncio
import json
import logging
import os
import random
import time

from room import Room 
from mqtt_manager import MQTTManager

#Logging 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("engine")

#Configuration 
BUILDING_ID     = "b01"
NUM_FLOORS      = 10
ROOMS_PER_FLOOR = 20
TICK_INTERVAL   = 5.0    
MAX_JITTER      = 5.0    
HEARTBEAT = 5   

# MQTT Manager
mqtt_manager = MQTTManager(
    broker_host=os.environ.get('MQTT_BROKER_HOST', 'localhost'),
    broker_port=1883,
    client_id="campus_engine"
)


#Fleet builder
def build_fleet() -> list[Room]:
    rooms = []

    for floor in range(1, NUM_FLOORS + 1):              
        for room_num in range(1, ROOMS_PER_FLOOR + 1):  

            room = Room(
                building=BUILDING_ID,
                floor=floor,
                room_num=room_num,
            )
            rooms.append(room)

    logger.info("Fleet built: %d rooms across %d floors", len(rooms), NUM_FLOORS)
    return rooms


#Single room task
async def room_task(room: Room, sim_start: float) -> None:

    #Startup jitter
    jitter_delay = random.uniform(0, MAX_JITTER)
    logger.debug("[%s] startup jitter: %.2fs", room.id, jitter_delay)
    await asyncio.sleep(jitter_delay)

    logger.info("[%s] task started", room.id)

    tick_count = 0 

    #Main simulation loop
    while True:
        tick_start = asyncio.get_event_loop().time()
        sim_clock = time.time() - sim_start

        #Full tick cycle 
        try:
            room.apply_physics(sim_clock)
            room.apply_environmental_correlations()
            room.validate_state()
            if random.random() < 0.10:
                room.set_occupancy(not room.occupancy)
            logger.info(
                "[%s] tick %d | T=%.2f°C H=%.1f%% Occ=%s HVAC=%s",
                room.id,
                tick_count,
                room.temp,
                room.humidity,
                room.occupancy,
                room.hvac_mode,
            )
            payload = room.telemetry_payload()
            topic   = f"{room.mqtt_path}/telemetry"
            # await mock_publish(topic, payload)
            await mqtt_manager.publish(topic, payload)

            if tick_count % HEARTBEAT == 0:
                hb_topic = f"{room.mqtt_path}/heartbeat"
                # await mock_publish(hb_topic, room.heartbeat_payload())
                await mqtt_manager.publish(hb_topic, room.heartbeat_payload())

        except Exception as exc:
            logger.error(
                "[%s] tick %d error: %s",
                room.id, tick_count, exc,
                exc_info=True,
            )

        tick_count += 1
        elapsed      = asyncio.get_event_loop().time() - tick_start
        actual_sleep = max(0.0, TICK_INTERVAL - elapsed)
        await asyncio.sleep(actual_sleep)   

#Engine entry point
async def run_engine() -> None:
    sim_start = time.time()
    logger.info("Engine starting at epoch %d", int(sim_start))
    
    await mqtt_manager.connect()
    
    rooms = build_fleet()

    tasks = [
        asyncio.create_task(
            room_task(room, sim_start),
            name=room.id  
        )
        for room in rooms
    ]

    logger.info("Launched %d concurrent room tasks", len(tasks))
    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    try:
        asyncio.run(run_engine())
    except KeyboardInterrupt:
        logger.info("Engine stopped by user (Ctrl+C).")