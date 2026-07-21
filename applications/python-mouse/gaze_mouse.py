#!/usr/bin/env python3
"""
gaze_mouse.py — drive the mouse cursor from tobiifreed's gaze stream,
with an on-demand fullscreen calibration GUI and optional eye-origin
head-movement correction.

Connects to the tobiifreed unix socket, subscribes to gaze data, and maps
the normalized (0..1) gaze coordinates to absolute screen coordinates
using a virtual uinput device (absolute pointer).

Three hotkeys (bind these in your Linux desktop environment):

    SIGUSR1 — pause/resume gaze -> mouse control
    SIGUSR2 — toggle the fullscreen calibration window
    SIGUSR3 — toggle eye-origin head-movement correction
              (NOTE: Linux has no real SIGUSR3. This is implemented using
              the SIGRTMIN real-time signal instead — send that signal to
              get the same effect; it's just referred to as "SIGUSR3"
              throughout this file and its messages for convenience.)

While the calibration window is open, mouse emulation is bypassed
entirely (your real mouse/touchpad works normally) so you can click on
targets. A red dot follows your *raw*, uncorrected gaze so you can see
the tracker's current error. Left-click anywhere to record a calibration
point at that location (storing the offset between where you clicked and
where the tracker thought you were looking); a green dot shows the corrected
gaze position. Right-click removes the nearest calibration point.
Points are saved to calib_points.json immediately and reloaded automatically
on the next run.

Eye-origin head-movement correction (SIGUSR3 / SIGRTMIN) is a separate,
optional layer on top of the calibration-point correction. While enabled,
frame-to-frame changes in eye origin position (i.e. how much your head has
moved since the last sample) are scaled by a gain factor and accumulated
into an extra x/y offset added to the mapped mouse position. This can help
compensate for gaze-mapping drift caused by head movement between/during
calibration. Toggling it off and back on resets the accumulated offset and
re-baselines against the eye origin at the moment it's re-enabled, so there
is no jump discontinuity.

    kill -USR1 <pid>          # pause/resume
    kill -USR2 <pid>          # toggle calibration window
    kill -SIGRTMIN <pid>      # toggle eye-origin head-movement correction ("SIGUSR3")
    pkill -USR1 -f gaze_mouse.py
    pkill -USR2 -f gaze_mouse.py
    pkill -SIGRTMIN -f gaze_mouse.py

Requires:
    pip install evdev pygame
    Read/write access to /dev/uinput (root, or add yourself to the
    'input' group / add a udev rule such as:
        KERNEL=="uinput", MODE="0660", GROUP="input"
    )

------------------------------------------------------------------------
Wire format (from daemon_protocol.zig) and GazeSample layout (from
tobiifree_core.zig) — see calibration.py's docstring for the calibration
math; the protocol/struct details are unchanged from before:

  Header:  [u8 msg_type] [u32 LE payload_len]   (5 bytes)
  Gaze:    header with msg_type = Srv.gaze (0x01), payload is the raw
           bytes of core.GazeSample (392 bytes, fields as declared in
           tobiifree_core.zig — validity_L/validity_R are u32, 0=valid).
  Subscribe command: header with msg_type = Cmd.subscribe (0x01), empty
           payload.
  Socket path: $XDG_RUNTIME_DIR/tobiifreed/gaze.sock (falls back to /tmp
           if XDG_RUNTIME_DIR is unset).

  NOTE on eye origins: the struct has 12 embedded 3D (x,y,z f64) blocks
  after the 2D gaze fields, described in this file's original comments as
  "eye origins, trackbox pos, 3D gaze, display-space variants". This
  script assumes the FIRST two of those 12 blocks are the left/right eye
  origin (in whatever coordinate system tobiifreed reports — typically a
  user coordinate system in mm). If tobiifree_core.zig orders its fields
  differently, adjust the `origin_L` / `origin_R` slice below.
------------------------------------------------------------------------
"""

import argparse
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

try:
    from evdev import UInput, AbsInfo, ecodes as e
except ImportError:
    sys.exit("Missing dependency. Install with: pip install evdev")

try:
    import pygame
except ImportError:
    sys.exit("Missing dependency. Install with: pip install pygame")

from gaze_correction import CalibrationStore, DEFAULT_PATH as DEFAULT_CALIB_PATH, DEFAULT_RADIUS


# ── Protocol constants ──────────────────────────────────────────────────

HEADER_SIZE = 5          # 1 byte msg_type + 4 byte little-endian payload_len
HEADER_FMT = "<BI"

MSG_TYPE_GAZE = 0x01      # Srv.gaze


class Cmd:
    SUBSCRIBE = 0x01      # Cmd.subscribe


GAZE_STRUCT_FMT = (
    "<"
    "IIII"    # present_mask, frame_counter, validity_L, validity_R
    "q"       # timestamp_us
    "dd"      # pupil_L_mm, pupil_R_mm
    "2d2d2d"  # gaze_point_2d_norm, gaze_point_2d_L_norm, gaze_point_2d_R_norm
    "3d3d3d3d3d3d3d3d3d3d3d3d"  # 12 f64x3 blocks (eye origins, trackbox pos, 3D gaze, display-space variants)
    "2d"      # gaze_point_2d_unfiltered
)
GAZE_STRUCT_SIZE = struct.calcsize(GAZE_STRUCT_FMT)  # 392 bytes

VALID = 0   # validity_L/validity_R: 0 == valid, 4 == not detected


# ── Socket path ─────────────────────────────────────────────────────────

def default_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return os.path.join(runtime_dir, "tobiifreed", "gaze.sock")


# ── Screen size detection ───────────────────────────────────────────────

def detect_screen_size():
    """Best-effort screen size via xrandr. Returns None if it can't tell."""
    try:
        out = subprocess.check_output(["xrandr"], text=True, stderr=subprocess.DEVNULL)
        m = re.search(r"current\s+(\d+)\s*x\s*(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


# ── uinput virtual pointer ──────────────────────────────────────────────

def make_uinput_device(width: int, height: int) -> UInput:
    """
    Create an absolute-positioning virtual pointer device (the same style
    as e.g. QEMU's virtual USB tablet).

    Deliberately NOT advertising BTN_TOOL_PEN/BTN_TOUCH: those capability
    bits make udev/libinput classify the device as a tablet/touchscreen,
    which gates cursor movement behind a "tool in proximity"/"touch down"
    event we'd never send. Sticking to BTN_LEFT + ABS_X/ABS_Y keeps it a
    plain absolute pointer that moves immediately on ABS events.
    """
    capabilities = {
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(value=0, min=0, max=width - 1, fuzz=0, flat=0, resolution=0)),
            (e.ABS_Y, AbsInfo(value=0, min=0, max=height - 1, fuzz=0, flat=0, resolution=0)),
        ],
        e.EV_KEY: [e.BTN_LEFT],
    }
    return UInput(capabilities, name="tobii-gaze-mouse", version=0x1)


# ── Socket helpers ───────────────────────────────────────────────────────

def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("tobiifreed closed the connection")
        buf.extend(chunk)
    return bytes(buf)


def send_message(sock: socket.socket, msg_type: int, payload: bytes = b"") -> None:
    header = struct.pack(HEADER_FMT, msg_type, len(payload))
    sock.sendall(header + payload)


# ── Shared gaze state (written by the socket thread, read by main thread) ─

class SharedGaze:
    def __init__(self, width: int, height: int):
        self._lock = threading.Lock()
        self.raw_x = width / 2.0
        self.raw_y = height / 2.0
        self.valid = False

        # Decoded eye-origin debug info (see update_origin/get_origin below).
        self.origin_L = (0.0, 0.0, 0.0)
        self.origin_R = (0.0, 0.0, 0.0)
        self.origin_vL = None
        self.origin_vR = None

    def update(self, x: float, y: float, valid: bool) -> None:
        with self._lock:
            self.raw_x, self.raw_y, self.valid = x, y, valid

    def get(self):
        with self._lock:
            return self.raw_x, self.raw_y, self.valid

    def update_origin(self, origin_L, origin_R, vL: int, vR: int) -> None:
        with self._lock:
            self.origin_L, self.origin_R = origin_L, origin_R
            self.origin_vL, self.origin_vR = vL, vR

    def get_origin(self):
        with self._lock:
            return self.origin_L, self.origin_R, self.origin_vL, self.origin_vR


# ── Main application ─────────────────────────────────────────────────────

class GazeMouseApp:
    def __init__(self, args, calib: CalibrationStore):
        self.args = args
        self.calib = calib
        self.width = args.width
        self.height = args.height

        self.paused = False
        self.calibration_mode = False
        self._displayed_calibration_mode = False  # what the pygame window currently shows

        # Eye-origin head-movement correction state (toggled via SIGUSR3/SIGRTMIN).
        self.head_correction_enabled = False
        self.head_offset_x = 0.0
        self.head_offset_y = 0.0
        self._prev_origin_xy = None  # (x, y) of the eye origin last frame, for delta computation
        self._last_eye_origin_print = 0.0  # monotonic timestamp, for throttling --print-eye-origin

        self.shared = SharedGaze(self.width, self.height)
        self.ui = make_uinput_device(self.width, self.height)
        self.smoothed_x = None
        self.smoothed_y = None
        self._debug_count = 0

        self._stop = threading.Event()

        signal.signal(signal.SIGUSR1, self._toggle_pause)
        signal.signal(signal.SIGUSR2, self._toggle_calibration)
        # True SIGUSR3 doesn't exist on Linux, so SIGRTMIN stands in for it.
        # It's referred to as "SIGUSR3" in log messages for convenience.
        self._head_correction_signal = signal.SIGRTMIN
        signal.signal(self._head_correction_signal, self._toggle_head_correction)
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    # ---- signal handlers ----

    def _toggle_pause(self, signum, frame):
        self.paused = not self.paused
        print(f"[gaze_mouse] {'paused' if self.paused else 'resumed'}", file=sys.stderr)

    def _toggle_calibration(self, signum, frame):
        self.calibration_mode = not self.calibration_mode
        print(f"[gaze_mouse] calibration window {'shown' if self.calibration_mode else 'hidden'}",
              file=sys.stderr)

    def _toggle_head_correction(self, signum, frame):
        self.head_correction_enabled = not self.head_correction_enabled
        # Reset the accumulated offset and the delta baseline every time this
        # is toggled (on OR off) so re-enabling never produces a jump: the
        # first frame after toggling just re-establishes the baseline eye
        # origin rather than applying a stale/large delta.
        self.head_offset_x = 0.0
        self.head_offset_y = 0.0
        self._prev_origin_xy = None
        print(f"[gaze_mouse] eye-origin head-movement correction (SIGUSR3) "
              f"{'enabled' if self.head_correction_enabled else 'disabled'} (offset reset)",
              file=sys.stderr)

    def _shutdown(self, signum, frame):
        print("[gaze_mouse] shutting down", file=sys.stderr)
        self._stop.set()
        try:
            self.ui.close()
        except Exception:
            pass
        try:
            pygame.quit()
        except Exception:
            pass
        sys.exit(0)

    # ---- gaze validity ----

    def eye_valid(self, vL: int, vR: int) -> bool:
        mode = self.args.eye
        if mode == "left":
            return vL == VALID
        if mode == "right":
            return vR == VALID
        if mode == "both":
            return vL == VALID and vR == VALID
        return vL == VALID or vR == VALID  # "either" (default)

    # ---- eye-origin helpers ----

    @staticmethod
    def _pick_origin_xy(origin_L, origin_R, vL: int, vR: int):
        """
        Average the (x, y) components (z/depth ignored) of whichever eye
        origin(s) are currently valid, for use as a 2D head-position proxy.
        Returns None if neither eye is valid this frame.
        """
        samples = []
        if vL == VALID:
            samples.append((origin_L[0], origin_L[1]))
        if vR == VALID:
            samples.append((origin_R[0], origin_R[1]))
        if not samples:
            return None
        ox = sum(s[0] for s in samples) / len(samples)
        oy = sum(s[1] for s in samples) / len(samples)
        return ox, oy

    # ---- background gaze-reading thread ----

    def handle_gaze_payload(self, payload: bytes) -> None:
        if len(payload) < GAZE_STRUCT_SIZE:
            if self.args.debug:
                print(f"[gaze_mouse][debug] payload too short: {len(payload)} bytes "
                      f"(expected {GAZE_STRUCT_SIZE}): {payload.hex()}", file=sys.stderr)
            return

        (
            present_mask, frame_counter, vL, vR, timestamp_us,
            pupil_L_mm, pupil_R_mm,
            x, y,               # gaze_point_2d_norm — final filtered combined 2D gaze
            x_L, y_L,           # gaze_point_2d_L_norm
            x_R, y_R,           # gaze_point_2d_R_norm
            *_rest,             # 12 f64x3 blocks + trailing gaze_point_2d_unfiltered (2d)
        ) = struct.unpack(GAZE_STRUCT_FMT, payload[:GAZE_STRUCT_SIZE])

        # _rest is a flat tuple: 12 x (x, y, z) blocks (36 doubles) followed
        # by the final 2D unfiltered gaze point (2 doubles).
        #
        # ASSUMPTION (verify against tobiifree_core.zig — adjust the slice
        # below if it doesn't match): per this file's own struct-layout
        # comment, block order is "eye origins, trackbox pos, 3D gaze,
        # display-space variants", so the first two 3D blocks are taken as
        # the left/right eye origin.
        origin_L = _rest[0:3]
        origin_R = _rest[3:6]

        valid = self.eye_valid(vL, vR)

        if self.args.debug:
            self._debug_count += 1
            if self._debug_count <= 20 or self._debug_count % 60 == 0:
                print(f"[gaze_mouse][debug] #{self._debug_count} vL={vL} vR={vR} "
                      f"x={x:.3f} y={y:.3f} valid={valid} paused={self.paused} "
                      f"calib_mode={self.calibration_mode} "
                      f"head_corr={self.head_correction_enabled} "
                      f"origin_L={origin_L} origin_R={origin_R}", file=sys.stderr)

        # Publish decoded eye origins for display/debugging (calibration
        # window readout and --print-eye-origin), independent of whether
        # head-movement correction is enabled.
        self.shared.update_origin(origin_L, origin_R, vL, vR)

        if self.args.print_eye_origin:
            now = time.monotonic()
            if now - self._last_eye_origin_print >= (1.0 / self.args.print_eye_origin_rate):
                self._last_eye_origin_print = now
                print(
                    f"[gaze_mouse][eye-origin] vL={vL} vR={vR} "
                    f"L=({origin_L[0]:.2f}, {origin_L[1]:.2f}, {origin_L[2]:.2f}) "
                    f"R=({origin_R[0]:.2f}, {origin_R[1]:.2f}, {origin_R[2]:.2f})",
                    file=sys.stderr,
                )

        # Update the eye-origin / head-movement tracking regardless of gaze
        # validity for x/y, as long as at least one eye's origin is valid.
        # This runs whether or not head correction is currently enabled, so
        # that re-enabling it doesn't see a huge stale delta (see
        # _toggle_head_correction, which clears _prev_origin_xy on toggle).
        origin_xy = self._pick_origin_xy(origin_L, origin_R, vL, vR)
        if origin_xy is not None:
            if self.head_correction_enabled:
                if self._prev_origin_xy is not None:
                    dx = origin_xy[0] - self._prev_origin_xy[0]
                    dy = origin_xy[1] - self._prev_origin_xy[1]
                    self.head_offset_x += dx * self.args.head_gain
                    self.head_offset_y += dy * self.args.head_gain
            self._prev_origin_xy = origin_xy

        if not valid:
            self.shared.update(self.shared.raw_x, self.shared.raw_y, False)
            return

        # Clamp normalized coordinates defensively, then convert to pixels.
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)

        if self.args.smoothing > 0:
            a = self.args.smoothing
            self.smoothed_x = x if self.smoothed_x is None else (a * x + (1 - a) * self.smoothed_x)
            self.smoothed_y = y if self.smoothed_y is None else (a * y + (1 - a) * self.smoothed_y)
            x, y = self.smoothed_x, self.smoothed_y

        raw_px = x * (self.width - 1)
        raw_py = y * (self.height - 1)
        self.shared.update(raw_px, raw_py, True)

        # Calibration window bypasses mouse emulation entirely.
        if self.calibration_mode or self.paused:
            return

        corr_x, corr_y = self.calib.compute_correction(raw_px, raw_py)
        px = raw_px + corr_x
        py = raw_py + corr_y

        if self.head_correction_enabled:
            px += self.head_offset_x
            py -= self.head_offset_y
            #print(f"[gaze_mouse] head-correction offset applied: "
            #      f"({self.head_offset_x:.2f}, {self.head_offset_y:.2f})")

        px = int(min(max(px, 0), self.width - 1))
        py = int(min(max(py, 0), self.height - 1))

        self.ui.write(e.EV_ABS, e.ABS_X, px)
        self.ui.write(e.EV_ABS, e.ABS_Y, py)
        self.ui.syn()

    def _gaze_thread_run_once(self, sock: socket.socket) -> None:
        send_message(sock, Cmd.SUBSCRIBE)
        print("[gaze_mouse] subscribed, streaming gaze -> mouse "
              f"({self.width}x{self.height})", file=sys.stderr)

        while not self._stop.is_set():
            header = recv_exact(sock, HEADER_SIZE)
            msg_type, payload_len = struct.unpack(HEADER_FMT, header)
            payload = recv_exact(sock, payload_len) if payload_len else b""

            if msg_type == MSG_TYPE_GAZE:
                self.handle_gaze_payload(payload)
            elif self.args.debug:
                print(f"[gaze_mouse][debug] non-gaze msg_type=0x{msg_type:02x} "
                      f"len={payload_len}", file=sys.stderr)

    def gaze_thread_main(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(self.args.socket)
                    self._gaze_thread_run_once(sock)
            except (ConnectionError, FileNotFoundError, OSError) as exc:
                if self._stop.is_set():
                    return
                print(f"[gaze_mouse] connection lost/failed: {exc}; "
                      f"retrying in {self.args.retry_delay}s", file=sys.stderr)
                time.sleep(self.args.retry_delay)

    # ---- pygame calibration GUI (runs on the main thread) ----

    def _show_calibration_window(self) -> None:
        try:
            self.screen = pygame.display.set_mode((self.width, self.height), pygame.FULLSCREEN)
        except pygame.error:
            self.screen = pygame.display.set_mode((self.width, self.height))
        pygame.display.set_caption("Gaze calibration")
        pygame.mouse.set_visible(True)
        pygame.event.set_grab(False)

    def _hide_calibration_window(self) -> None:
        try:
            self.screen = pygame.display.set_mode((1, 1), pygame.HIDDEN)
        except pygame.error:
            # Older pygame without HIDDEN support: fall back to iconify.
            pygame.display.set_mode((200, 100))
            pygame.display.iconify()

    def _draw_calibration_frame(self, font) -> None:
        screen = self.screen
        screen.fill((15, 15, 20))

        mx, my = pygame.mouse.get_pos()
        pygame.draw.circle(
            screen,
            (140, 140, 140),                  # grey
            (mx, my),
            int(self.calib.default_radius),
            width=2,
        )

        radius_overlay = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        for p in self.calib.points:
            r = int(p.get("radius", self.calib.default_radius))
            pygame.draw.circle(radius_overlay, (255, 140, 0, 40),
                                (int(p["target_x"]), int(p["target_y"])), r)
        screen.blit(radius_overlay, (0, 0))

        for p in self.calib.points:
            pygame.draw.circle(screen, (255, 140, 0), (int(p["target_x"]), int(p["target_y"])), 8)
            pygame.draw.circle(screen, (255, 200, 140), (int(p["target_x"]), int(p["target_y"])), 8, width=2)

        raw_x, raw_y, valid = self.shared.get()
        if valid:
            corr_dx, corr_dy = self.calib.compute_correction(raw_x, raw_y)
            corr_x = raw_x + corr_dx
            corr_y = raw_y + corr_dy

            # Raw (uncorrected) gaze: red
            pygame.draw.circle(screen, (220, 40, 40), (int(raw_x), int(raw_y)), 10)
            pygame.draw.circle(screen, (255, 180, 180), (int(raw_x), int(raw_y)), 10, width=2)

            # Corrected gaze: green
            pygame.draw.circle(screen, (0, 220, 0), (int(corr_x), int(corr_y)), 10)
            pygame.draw.circle(screen, (180, 255, 180), (int(corr_x), int(corr_y)), 10, width=2)
        else:
            msg = font.render("waiting for valid gaze data...", True, (200, 60, 60))
            screen.blit(msg, (20, self.height - 40))

        origin_L, origin_R, ovL, ovR = self.shared.get_origin()
        lines = [
            "Left-click: add calibration point at this spot",
            "Right-click: remove nearest calibration point",
            f"+ / - : adjust radius (currently {int(self.calib.default_radius)}px)",
            "C: clear all calibration points",
            f"{len(self.calib.points)} calibration point(s) stored",
            f"Head-movement correction (SIGUSR3): "
            f"{'ON' if self.head_correction_enabled else 'off'} (gain={self.args.head_gain})",
            f"eye_origin_L_mm (v={ovL}): "
            f"({origin_L[0]:.1f}, {origin_L[1]:.1f}, {origin_L[2]:.1f})",
            f"eye_origin_R_mm (v={ovR}): "
            f"({origin_R[0]:.1f}, {origin_R[1]:.1f}, {origin_R[2]:.1f})",
            "Send SIGUSR2 again to exit calibration",
        ]
        for i, line in enumerate(lines):
            txt = font.render(line, True, (230, 230, 230))
            screen.blit(txt, (20, 20 + i * 26))

        pygame.display.flip()

    def _handle_calibration_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.calibration_mode = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:  # left click: add point
                    raw_x, raw_y, valid = self.shared.get()
                    if valid:
                        tx, ty = event.pos
                        self.calib.add_point(tx, ty, raw_x, raw_y)
                        print(f"[gaze_mouse] calibration point added at ({tx},{ty}), "
                              f"raw gaze was ({raw_x:.0f},{raw_y:.0f})", file=sys.stderr)
                    else:
                        print("[gaze_mouse] ignored click: no valid gaze data right now", file=sys.stderr)
                elif event.button == 3:  # right click: remove nearest point
                    if self.calib.remove_nearest(*event.pos):
                        print("[gaze_mouse] removed nearest calibration point", file=sys.stderr)
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                    self.calib.default_radius = min(2000, self.calib.default_radius + 10)
                    self.calib.save()
                elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    self.calib.default_radius = max(20, self.calib.default_radius - 10)
                    self.calib.save()
                elif event.key == pygame.K_c:
                    self.calib.clear()
                    print("[gaze_mouse] cleared all calibration points", file=sys.stderr)
                elif event.key == pygame.K_ESCAPE:
                    self.calibration_mode = False

    def run(self) -> None:
        threading.Thread(target=self.gaze_thread_main, daemon=True).start()

        pygame.init()
        font = pygame.font.SysFont(None, 24)
        self._hide_calibration_window()

        print(f"[gaze_mouse] pid={os.getpid()} — hotkeys: SIGUSR1 pause/resume, "
              f"SIGUSR2 toggle calibration, SIGUSR3 (SIGRTMIN) toggle head-movement "
              f"correction (kill -USR1/-USR2/-SIGRTMIN {os.getpid()})", file=sys.stderr)

        clock = pygame.time.Clock()
        while not self._stop.is_set():
            if self.calibration_mode != self._displayed_calibration_mode:
                if self.calibration_mode:
                    self._show_calibration_window()
                else:
                    self._hide_calibration_window()
                self._displayed_calibration_mode = self.calibration_mode

            if self.calibration_mode:
                self._handle_calibration_events()
                self._draw_calibration_frame(font)
                clock.tick(60)
            else:
                pygame.event.pump()  # keep SDL responsive while hidden
                clock.tick(15)


def parse_args():
    p = argparse.ArgumentParser(description="Map tobiifreed gaze data to the mouse cursor via uinput, "
                                             "with an on-demand calibration GUI and optional eye-origin "
                                             "head-movement correction.")
    p.add_argument("--socket", default=default_socket_path(),
                    help="Path to tobiifreed's unix socket (default: $XDG_RUNTIME_DIR/tobiifreed/gaze.sock)")
    p.add_argument("--width", type=int, default=None, help="Screen width in pixels (default: auto-detect via xrandr)")
    p.add_argument("--height", type=int, default=None, help="Screen height in pixels (default: auto-detect via xrandr)")
    p.add_argument("--eye", choices=["left", "right", "both", "either"], default="either",
                    help="Which eye's validity to require (default: either)")
    p.add_argument("--smoothing", type=float, default=0.0,
                    help="Exponential moving average factor in (0,1]; 0 disables smoothing (default: 0)")
    p.add_argument("--retry-delay", type=float, default=2.0,
                    help="Seconds to wait before reconnecting after a lost connection (default: 2)")
    p.add_argument("--debug", action="store_true",
                    help="Print raw/parsed gaze samples and non-gaze messages to stderr")
    p.add_argument("--print-eye-origin", action="store_true",
                    help="Continuously print the decoded left/right eye origin "
                         "(eye_origin_L_mm / eye_origin_R_mm) to stderr, throttled to "
                         "--print-eye-origin-rate Hz. Useful for verifying the struct "
                         "extraction is correct.")
    p.add_argument("--print-eye-origin-rate", type=float, default=5.0,
                    help="Max prints per second for --print-eye-origin (default: 5)")
    p.add_argument("--calib-file", default=DEFAULT_CALIB_PATH,
                    help=f"Path to calibration points JSON file (default: {DEFAULT_CALIB_PATH})")
    p.add_argument("--radius", type=float, default=None,
                    help=f"Initial calibration correction radius in pixels "
                         f"(default: {DEFAULT_RADIUS}, or whatever is stored in the calib file)")
    p.add_argument("--head-gain", type=float, default=1.0,
                    help="Gain applied to frame-to-frame eye-origin (head) movement when "
                         "head-movement correction is toggled on via SIGUSR3 (SIGRTMIN); "
                         "the resulting scaled delta is accumulated into the mouse x/y "
                         "position each frame. Units depend on tobiifreed's eye-origin "
                         "coordinate system (often mm) — tune to taste (default: 1.0)")
    args = p.parse_args()

    if args.width is None or args.height is None:
        detected = detect_screen_size()
        if detected is None:
            p.error("could not auto-detect screen size; pass --width/--height explicitly")
        args.width = args.width or detected[0]
        args.height = args.height or detected[1]

    return args


def main():
    args = parse_args()
    calib = CalibrationStore(path=args.calib_file,
                              default_radius=args.radius if args.radius is not None else DEFAULT_RADIUS)
    if args.radius is not None:
        calib.default_radius = args.radius  # explicit CLI override wins over the stored value

    app = GazeMouseApp(args, calib)
    app.run()


if __name__ == "__main__":
    main()