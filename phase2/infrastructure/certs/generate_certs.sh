#!/bin/bash

# Configuration
CERT_DIR="./infrastructure/certs"
PASSWORD="password"
KEYSTORE_PATH="$CERT_DIR/broker-keystore.jks"
TRUSTSTORE_PATH="$CERT_DIR/broker-truststore.jks"
CA_NAME="CampusPulseCA"
SERVER_NAME="hivemq"

mkdir -p $CERT_DIR

echo "Generating CA..."
openssl req -new -x509 -days 3650 -extensions v3_ca -keyout $CERT_DIR/ca.key -out $CERT_DIR/ca.crt -subj "/CN=$CA_NAME" -passout pass:$PASSWORD

echo "Creating Keystore for HiveMQ..."
# Create a key pair and store in keystore
keytool -genkey -alias $SERVER_NAME -keyalg RSA -keystore $KEYSTORE_PATH -dname "CN=$SERVER_NAME" -storepass $PASSWORD -keypass $PASSWORD

echo "Generating CSR..."
keytool -certreq -alias $SERVER_NAME -keystore $KEYSTORE_PATH -file $CERT_DIR/server.csr -storepass $PASSWORD

echo "Signing Certificate with CA..."
openssl x509 -req -CA $CERT_DIR/ca.crt -CAkey $CERT_DIR/ca.key -in $CERT_DIR/server.csr -out $CERT_DIR/server.crt -days 365 -CAcreateserial -passin pass:$PASSWORD

echo "Importing CA into Keystore..."
keytool -import -trustcacerts -alias root -file $CERT_DIR/ca.crt -keystore $KEYSTORE_PATH -storepass $PASSWORD -noprompt

echo "Importing Signed Server Certificate into Keystore..."
keytool -import -alias $SERVER_NAME -file $CERT_DIR/server.crt -keystore $KEYSTORE_PATH -storepass $PASSWORD -noprompt

echo "Creating Truststore for Clients..."
keytool -import -alias root -file $CERT_DIR/ca.crt -keystore $TRUSTSTORE_PATH -storepass $PASSWORD -noprompt

# Copy to hivemq config dir
cp $KEYSTORE_PATH ./infrastructure/hivemq/conf/

echo "Certificates generated in $CERT_DIR"
echo "Keystore copied to hivemq config."
