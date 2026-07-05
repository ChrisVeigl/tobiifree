#!/usr/bin/env python3
"""
calibrate.py 

Connects to the tobiifreed unix socket, and performs a 5-point calibration.
A calibration blob is generated and applied to the daemon, which will then
correct gaze data for the current user.
This is a standalone script, but it can also be imported as a module and the
`main()` function can be called to run the calibration process.
The calibration points are hardcoded to the 5-point grid used in the web demo.
The calibration process is as follows:
1. Connect to the tobiifreed daemon via its unix socket.
2. Send a CMD_START_CALIBRATION command to the daemon.
3. For each of the 5 calibration points:
   a. Display a dot on the screen at the calibration point.
   b. Wait for the user to press ENTER while looking at the dot.
   c. Send a CMD_ADD_CALIBRATION_POINT command to the daemon with the normalized
      coordinates of the calibration point.
4. After all points are captured, send a CMD_FINISH_CALIBRATION command to the
   daemon to compute the calibration blob.
5. Send a CMD_CAL_APPLY command to the daemon with the calibration blob to apply
   the calibration.
The script uses pygame to display the calibration points and capture user input.
"""

import os
import socket
import struct
import sys
import time
from pathlib import Path

try:
    import pygame
except ImportError:
    print("Pygame is not installed. Please install it using: pip install pygame")
    sys.exit(1)

# Command IDs from daemon_protocol.zig
CMD_SUBSCRIBE = 0x01
CMD_START_CALIBRATION = 0x20
CMD_ADD_CALIBRATION_POINT = 0x21
CMD_FINISH_CALIBRATION = 0x22
CMD_CAL_APPLY = 0x23

# Server message types
SRV_GAZE = 0x01
SRV_RESPONSE = 0x02
SRV_ERR = 0xFF

HEADER_SIZE = 5

def get_socket_path():
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR', '/tmp')
    return Path(runtime_dir) / 'tobiifreed' / 'gaze.sock'

def encode_header(msg_type, payload_len):
    return struct.pack('<BI', msg_type, payload_len)

def decode_header(header_bytes):
    if len(header_bytes) != HEADER_SIZE:
        raise EOFError("Incomplete header")
    msg_type, payload_len = struct.unpack('<BI', header_bytes)
    return msg_type, payload_len

def request(sock, cmd_type, payload=b''):
    header = encode_header(cmd_type, len(payload))
    sock.sendall(header + payload)

    # Need to consume messages until we get a response to our command
    while True:
        header_bytes = sock.recv(HEADER_SIZE)
        if not header_bytes:
            raise EOFError("Disconnected from daemon")
            
        msg_type, payload_len = decode_header(header_bytes)
        
        # Read payload
        resp_payload = b''
        while len(resp_payload) < payload_len:
            chunk = sock.recv(payload_len - len(resp_payload))
            if not chunk:
                raise EOFError("Disconnected while reading payload")
            resp_payload += chunk

        if msg_type == SRV_ERR:
            raise RuntimeError(f"Daemon Error for cmd 0x{cmd_type:02x}")
        elif msg_type == SRV_RESPONSE:
            if len(resp_payload) > 0 and resp_payload[0] == cmd_type:
                # It's our response
                return resp_payload[1:]
        # Ignore SRV_GAZE and responses to other commands

def main():
    sock_path = get_socket_path()
    print(f"Connecting to tobiifreed at {sock_path}...")
    
    if not sock_path.exists():
        print("Error: Socket does not exist. Make sure tobiifreed is running.")
        sys.exit(1)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(sock_path))
    except Exception as e:
        print(f"Failed to connect: {e}")
        sys.exit(1)

    print("Connected to daemon socket.")
    print("Starting calibration...")
    request(sock, CMD_START_CALIBRATION)

    pygame.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    w, h = screen.get_size()
    pygame.mouse.set_visible(False)
    
    font = pygame.font.Font(None, 36)
    big_font = pygame.font.Font(None, 48)

    # 5-point calibration grid used in the web demo
    pts = [
        (0.5, 0.5),
        (0.1, 0.1),
        (0.9, 0.1),
        (0.1, 0.9),
        (0.9, 0.9),
    ]
    current_idx = 0
    running = True
    capturing = False

    def draw_state():
        screen.fill((0, 0, 0))
        if current_idx < len(pts):
            x_norm, y_norm = pts[current_idx]
            px, py = int(x_norm * w), int(y_norm * h)
            
            color = (255, 255, 0) if capturing else (0, 255, 0)
            pygame.draw.circle(screen, color, (px, py), 15)
            
            msg = f"Point {current_idx + 1}/{len(pts)}: Look at the dot and press ENTER. (Esc to cancel)"
            if capturing:
                msg = f"Capturing point {current_idx + 1}... hold your gaze!"
            
            text_surf = font.render(msg, True, (255, 255, 255))
            screen.blit(text_surf, (w // 2 - text_surf.get_width() // 2, h - 100))
        else:
            msg = "Computing calibration..."
            text_surf = big_font.render(msg, True, (255, 255, 255))
            screen.blit(text_surf, (w // 2 - text_surf.get_width() // 2, h // 2 - text_surf.get_height() // 2))
            
        pygame.display.flip()

    while running:
        draw_state()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_RETURN:
                    if current_idx < len(pts) and not capturing:
                        capturing = True
                        draw_state()
                        
                        # Process events to show the "Capturing" UI update immediately
                        pygame.event.pump()
                        
                        x, y = pts[current_idx]
                        print(f">> Capturing point {current_idx + 1} at ({x:.1f}, {y:.1f})...")
                        payload = struct.pack('<dd', x, y)
                        request(sock, CMD_ADD_CALIBRATION_POINT, payload)
                        
                        current_idx += 1
                        capturing = False
                        
                        if current_idx >= len(pts):
                            
                            print("Computing calibration...")
                            blob = request(sock, CMD_FINISH_CALIBRATION)
                            print(f"Got calibration blob of {len(blob)} bytes.")
                            
                            print("Applying calibration...")
                            request(sock, CMD_CAL_APPLY, blob)
                            print("Calibration applied successfully!")
                            
                            time.sleep(1)
                            running = False
        
        time.sleep(0.01)

    pygame.quit()
    sock.close()

if __name__ == '__main__':
    main()
