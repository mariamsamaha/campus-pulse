import asyncio
import logging
import json
import time

from gmqtt import Client as MQTTClient
logger = logging.getLogger("mqtt_manager")

class MQTTManager:
    def __init__(self, broker_host, broker_port=1883, client_id="campus_engine"):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client = MQTTClient(client_id)
        self.is_connected = False

    async def connect(self):
        try:
            await self.client.connect(self.broker_host, self.broker_port)
            self.is_connected = True
            logger.info(f"Connected to MQTT Broker at {self.broker_host}")
        except Exception as e:
            logger.error(f"Failed to connect to broker: {e}")

    async def publish(self, topic: str, payload: dict, qos: int = 1):
        #Publishes a dictionary as a JSON string
        if self.is_connected:
            # converting dictionary to JSON string for the broker
            json_payload = json.dumps(payload)
            self.client.publish(topic, json_payload, qos=qos)
        else:
            logger.warning(f"Dropped message to {topic} (Not connected)")

    async def disconnect(self):
        await self.client.disconnect()

    

async def main():
    mqtt_manager = MQTTManager(broker_host='localhost', broker_port=1883, client_id='campus_client')
    await mqtt_manager.connect()
    await mqtt_manager.subscribe('campus/+/telemetry')

    # Keep the program running to listen for messages
    while True:
        await asyncio.sleep(1)