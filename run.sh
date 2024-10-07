#!/bin/bash
set +u
CONFIG_PATH=/data/options.json

# Read the port from the environment variable or default to 3000
EUFY_WS_PORT=${EUFY_SECURITY_WS_PORT:-3000}

echo "Starting EufyP2PStream. eufy_security_ws_port is $EUFY_WS_PORT"
python3 -u /app/eufyp2pstream.py $EUFY_WS_PORT
echo "Exited with code $?"
