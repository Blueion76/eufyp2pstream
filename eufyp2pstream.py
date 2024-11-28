import os
import signal
import sys
import asyncio
import socket
import select
from aiohttp import ClientSession
from queue import Queue, Empty
from threading import Thread, Event
import json
import argparse
from websocket import EufySecurityWebSocket

# Constants
RECV_CHUNK_SIZE = 4096
MAX_QUEUE_SIZE = 100
SOCKET_TIMEOUT = 2
QUEUE_GET_TIMEOUT = 0.5
WEBSOCKET_RECONNECT_DELAY = 5

# Event Configuration
EVENT_CONFIGURATION = {
    "livestream video data": {"name": "video_data", "value": "buffer", "type": "event"},
    "livestream audio data": {"name": "audio_data", "value": "buffer", "type": "event"},
}

# Message Templates
START_P2P_LIVESTREAM_MESSAGE = {"messageId": "start_livestream", "command": "device.start_livestream", "serialNumber": None}
STOP_P2P_LIVESTREAM_MESSAGE = {"messageId": "stop_livestream", "command": "device.stop_livestream", "serialNumber": None}
START_TALKBACK = {"messageId": "start_talkback", "command": "device.start_talkback", "serialNumber": None}
SEND_TALKBACK_AUDIO_DATA = {"messageId": "talkback_audio_data", "command": "device.talkback_audio_data", "serialNumber": None, "buffer": None}
STOP_TALKBACK = {"messageId": "stop_talkback", "command": "device.stop_talkback", "serialNumber": None}
SET_API_SCHEMA = {"messageId": "set_api_schema", "command": "set_api_schema", "schemaVersion": 13}
START_LISTENING_MESSAGE = {"messageId": "start_listening", "command": "start_listening"}
DRIVER_CONNECT_MESSAGE = {"messageId": "driver_connect", "command": "driver.connect"}

# Global Variables
camera_handlers = {}
run_event = Event()
debug = False

def log_message(message, force=False):
    """Log a message if debug mode is enabled."""
    if debug or force:
        print(message)
        sys.stdout.flush()

def exit_handler(signum, frame):
    """Signal handler for application shutdown."""
    log_message(f"Signal {signum} received, shutting down.", True)
    run_event.set()

# Signal Setup
signal.signal(signal.SIGINT, exit_handler)
signal.signal(signal.SIGTERM, exit_handler)

class CameraStreamHandler:
    """Handles video/audio streaming for a camera."""
    def __init__(self, serial_number, base_port, run_event):
        self.serial_number = serial_number
        self.run_event = run_event
        self.ws = None
        self.sockets = {}
        self.threads = {}
        self.base_port = base_port

    def setup_sockets(self):
        """Create and configure sockets for video, audio, and backchannel."""
        roles = ["video", "audio", "backchannel"]
        for i, role in enumerate(roles):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("0.0.0.0", self.base_port + i))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(SOCKET_TIMEOUT)
            sock.listen()
            self.sockets[role] = sock

    def start_stream(self):
        """Start threads for handling client connections."""
        log_message(f"Starting stream for camera {self.serial_number}")
        roles = ["video", "audio", "backchannel"]
        for role in roles:
            thread = ClientAcceptThread(self.sockets[role], self.run_event, role, self.ws, self.serial_number)
            thread.start()
            self.threads[role] = thread

    def stop_stream(self):
        """Stop all threads and close sockets."""
        for thread in self.threads.values():
            thread.join()
        for sock in self.sockets.values():
            sock.close()

    def set_ws(self, ws):
        """Associate the WebSocket instance."""
        self.ws = ws

class ClientAcceptThread(Thread):
    """Accepts client connections for a specific role."""
    def __init__(self, sock, run_event, role, ws, serial_number):
        super().__init__()
        self.sock = sock
        self.run_event = run_event
        self.role = role
        self.ws = ws
        self.serial_number = serial_number
        self.child_threads = []

    def run(self):
        log_message(f"Accepting {self.role} connections for {self.serial_number}")
        while not self.run_event.is_set():
            try:
                client_sock, _ = self.sock.accept()
                client_sock.setblocking(False)
                if self.role == "backchannel":
                    thread = ClientRecvThread(client_sock, self.run_event, self.role, self.ws, self.serial_number)
                else:
                    thread = ClientSendThread(client_sock, self.run_event, self.role, self.ws, self.serial_number)
                thread.start()
                self.child_threads.append(thread)
            except socket.timeout:
                continue
        log_message(f"Stopping {self.role} accept thread for {self.serial_number}")

class ClientSendThread(Thread):
    """Handles sending data to a client."""
    def __init__(self, client_sock, run_event, role, ws, serial_number):
        super().__init__()
        self.client_sock = client_sock
        self.run_event = run_event
        self.role = role
        self.ws = ws
        self.serial_number = serial_number
        self.queue = Queue(MAX_QUEUE_SIZE)

    def run(self):
        log_message(f"Client send thread for {self.role} started.")
        try:
            while not self.run_event.is_set():
                try:
                    data = self.queue.get(timeout=QUEUE_GET_TIMEOUT)
                    self.client_sock.sendall(bytearray(data["data"]))
                except Empty:
                    continue
        finally:
            self.client_sock.close()
            log_message(f"Client send thread for {self.role} stopped.")

class ClientRecvThread(Thread):
    """Handles receiving data from a client."""
    def __init__(self, client_sock, run_event, role, ws, serial_number):
        super().__init__()
        self.client_sock = client_sock
        self.run_event = run_event
        self.role = role
        self.ws = ws
        self.serial_number = serial_number

    def run(self):
        log_message(f"Client recv thread for {self.role} started.")
        try:
            while not self.run_event.is_set():
                try:
                    data = self.client_sock.recv(RECV_CHUNK_SIZE)
                    if data:
                        message = SEND_TALKBACK_AUDIO_DATA.copy()
                        message["serialNumber"] = self.serial_number
                        message["buffer"] = list(data)
                        asyncio.run(self.ws.send_message(json.dumps(message)))
                except socket.error:
                    continue
        finally:
            self.client_sock.close()
            log_message(f"Client recv thread for {self.role} stopped.")

async def init_websocket(ws_port, camera_handlers):
    """Initialize WebSocket and manage reconnection."""
    while not run_event.is_set():
        try:
            async with ClientSession() as session:
                ws = EufySecurityWebSocket("172.16.1.28", ws_port, session, on_open, on_message, on_close, on_error)
                for handler in camera_handlers.values():
                    handler.set_ws(ws)
                await ws.connect()
                await ws.send_message(json.dumps(START_LISTENING_MESSAGE))
                await asyncio.sleep(1000)
        except Exception as ex:
            log_message(f"WebSocket error: {ex}")
            await asyncio.sleep(WEBSOCKET_RECONNECT_DELAY)

if __name__ == "__main__":
    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    parser.add_argument("--camera_serials", nargs="+", required=True, help="List of camera serials.")
    parser.add_argument("--ws_port", type=int, default=3000, help="WebSocket port.")
    args = parser.parse_args()

    debug = args.debug
    log_message(f"Debug mode: {debug}")

    BASE_PORT = 63336
    for i, serial in enumerate(args.camera_serials):
        handler = CameraStreamHandler(serial, BASE_PORT + i * 3, run_event)
        handler.setup_sockets()
        camera_handlers[serial] = handler

    asyncio.run(init_websocket(args.ws_port, camera_handlers))
