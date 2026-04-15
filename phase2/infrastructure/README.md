# Phase 2: Infrastructure & Cloud Setup (Nour's Scope)

This directory contains the core infrastructure and cloud configuration for Phase 2.

## 📁 Structure
- `hivemq/`: Configuration and ACLs for the HiveMQ CE broker.
- `thingsboard/`: Persistence and logs for the ThingsBoard IoT platform.
- `nodered/`: 10 instances of Node-RED for floor-level gateway simulation.
- `provisioning/`: Scripts to automate the registration of assets and devices.
- `certs/`: Scripts to generate TLS/DTLS certificates.

## 🚀 Getting Started

### 1. Generate Certificates
Run the certificate generation script to create the JKS keystore for HiveMQ:
```bash
chmod +x infrastructure/certs/generate_certs.sh
./infrastructure/certs/generate_certs.sh
```

### 2. Launch the Stack
Start all services (HiveMQ, ThingsBoard, and 10 Node-RED instances):
```bash
docker-compose up -d
```

### 3. Provision ThingsBoard Assets
Once ThingsBoard is healthy (check `http://localhost:9090`), run the provisioning script:
```bash
python3 infrastructure/provisioning/provision_devices.py
```
*Note: Default credentials (tenant@thingsboard.org / tenant) are used.*

## 🔒 Security Summary
- **MQTT TLS**: Handled via HiveMQ listener on port 8883 using the generated `broker-keystore.jks`.
- **ACLs**: Configured in `hivemq/conf/access-control.xml` to isolate traffic per floor.
- **CoAP DTLS**: Provisioned nodes are assigned credentials for DTLS in the TB dashboard (can be semi-automated in the script).

## 📊 Evaluation Walkthrough
1. **Infrastructure**: Show `docker ps` with all 13 containers (HiveMQ, TB, 10x NR, Postgres).
2. **Device Registry**: Show the Asset Hierarchy in TB: `Main Campus` -> `B01` -> `Floor 0` -> `Room 0-00`.
3. **Security**: Demonstrate an MQTT connection failure when using a floor-0 client ID to publish to a floor-1 topic.
4. **Dashboards**: Import the provided JSON (see `thingsboard/` folder) to show real-time telemetry from the 200 nodes.
