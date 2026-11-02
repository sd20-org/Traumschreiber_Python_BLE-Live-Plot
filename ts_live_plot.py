#!/usr/bin/env python3
"""Live view of all Traumschreiber channels over Bluetooth Low Energy.

Connects to a Traumschreiber, starts streaming and plots every channel the device sends
(normally all 8) below each other, in microvolts, updated four times per second.

    python ts_live_plot.py                      # find the nearest Traumschreiber
    python ts_live_plot.py --addr AA:BB:CC:DD:EE:FF
    python ts_live_plot.py --filter             # band-pass 1-35 Hz for display
    python ts_live_plot.py --snapshot 20        # no window: record 20 s, save a PNG, exit

Press "f" in the window to switch the display filter (1-35 Hz, 50 Hz notch) on and off.
"""
import argparse
import asyncio
import struct
import sys
import threading
import time

import numpy as np
from bleak import BleakClient, BleakScanner
from scipy import signal

# GATT characteristics of the Traumschreiber ExG service
def _uuid(short):
    return f"0483{short}-6c9d-6ca9-5d41-03ad4fff4abb"

DATA, CONTROL, STATUS = _uuid("dddb"), _uuid("dddc"), _uuid("dddd")
CMD_START, CMD_STOP = b"\x01", b"\x02"
FS = 250.0  # samples per second and channel

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--addr", help="Bluetooth address of the device; without it, the nearest one is used")
ap.add_argument("--name", default="Traumschreiber", help="name to search for (default: Traumschreiber)")
ap.add_argument("--gain", type=float, default=0.0,
                help="amplifier gain; default: read from the device (firmware 0.0.91+), else 4")
ap.add_argument("--window", type=float, default=5.0, help="seconds shown (default 5)")
ap.add_argument("--ylim", type=float, default=0.0, help="fixed y axis +-N uV; 0 = automatic (default)")
ap.add_argument("--filter", action="store_true",
                help="start with the display filter on (1-35 Hz band-pass and mains notch)")
ap.add_argument("--mains", type=float, default=50.0, help="mains frequency for the notch: 50 or 60 (default 50)")
ap.add_argument("--snapshot", type=float, help="record this many seconds, save --out and exit")
ap.add_argument("--out", default="ts_live_plot.png", help="file name for --snapshot")
args = ap.parse_args()

# ADS131M08: 24-bit values, full scale +-1.2 V / gain
def uv_per_count(gain):
    return 1.2 / gain / (1 << 23) * 1e6


N = int(args.window * FS)
LEAD = int(5 * FS)  # extra history so the display filter has settled at the left edge
VALID_GAINS = {1, 2, 4, 8, 16, 32, 64, 128}


def read_status(status):
    """Status characteristic: [14] number of channels n, [15..15+n) channel numbers, then
    compression bits (1), delta max (4), sample counter (4), SD state (4), gain (1),
    bytes per value (1) and, since firmware 0.0.91, one gain per channel (8)."""
    nch = status[14]
    chans = list(status[15:15 + nch])
    gains = [4] * 8
    p = 15 + nch + 1 + 4 + 4 + 4
    if p < len(status) and status[p] in VALID_GAINS:
        gains = [status[p]] * 8
    if p + 2 + 8 <= len(status) and all(g in VALID_GAINS for g in status[p + 2:p + 10]):
        gains = list(status[p + 2:p + 10])
    if args.gain:
        gains = [args.gain] * 8
    return nch, chans, [gains[c] for c in chans]

lock = threading.Lock()
st = dict(nch=0, chans=[], gains=[], scale=None, buf=None, received=0, last_sn=None, lost=0,
          connected=False, label="", error=None, stop=False)


def parse_packet(_sender, data: bytearray):
    """One notification: 0xC0, then per sample
    sample number (uint32) | device time in us (uint64) | n x int24 | status (uint32),
    all little-endian. A packet that is just 0xC0 is a heartbeat."""
    nch = st["nch"]
    if not data or data[0] != 0xC0 or nch == 0:
        return
    size = 4 + 8 + 3 * nch + 4
    rows, numbers = [], []
    for p in range(1, len(data) - size + 1, size):
        numbers.append(struct.unpack_from("<I", data, p)[0])
        q = p + 12
        rows.append([int.from_bytes(data[q + 3 * c:q + 3 * c + 3], "little", signed=True)
                     for c in range(nch)])
    if not rows:
        return
    block = np.asarray(rows, dtype=float) * st["scale"]
    with lock:
        for sn in numbers:
            if st["last_sn"] is not None and sn > st["last_sn"] + 1:
                st["lost"] += sn - st["last_sn"] - 1
            st["last_sn"] = sn
        k = min(len(block), N + LEAD)
        st["buf"] = np.roll(st["buf"], -k, axis=0)
        st["buf"][-k:] = block[-k:]
        st["received"] += len(block)


async def find_device():
    if args.addr:
        return await BleakScanner.find_device_by_address(args.addr, timeout=20)
    print(f"Searching for '{args.name}' ...")
    found = await BleakScanner.discover(timeout=8, return_adv=True)
    hits = sorted(((adv.rssi, d) for d, adv in found.values()
                   if d.name and args.name.lower() in d.name.lower()), key=lambda x: -x[0])
    for rssi, d in hits:
        print(f"  {d.name}  {d.address}  {rssi} dBm")
    return hits[0][1] if hits else None


async def ble_main():
    try:
        dev = await find_device()
        if dev is None:
            st["error"] = "No Traumschreiber found. Is it switched on and not connected to a phone?"
            return
        print(f"Connecting to {dev.name} ({dev.address}) ...")
        async with BleakClient(dev, timeout=20) as client:
            nch, chans, gains = read_status(await client.read_gatt_char(STATUS))
            with lock:
                st.update(nch=nch, chans=chans, gains=gains, buf=np.zeros((N + LEAD, nch)),
                          scale=np.array([uv_per_count(g) for g in gains]),
                          connected=True, label=dev.name or "Traumschreiber")
            print(f"Connected, channels {chans}, gain {sorted(set(gains))}. "
                  "Close the window or press Ctrl+C to stop.")
            await client.start_notify(DATA, parse_packet)
            await client.write_gatt_char(CONTROL, CMD_START, response=True)
            while not st["stop"] and client.is_connected:
                await asyncio.sleep(0.2)
            try:
                await client.write_gatt_char(CONTROL, CMD_STOP, response=True)
            except Exception:
                pass
    except Exception as ex:
        st["error"] = f"Bluetooth error: {ex}"
    finally:
        st["connected"] = False


ble = threading.Thread(target=lambda: asyncio.run(ble_main()), daemon=True)
ble.start()
t0 = time.monotonic()
while not st["connected"] and st["error"] is None and time.monotonic() - t0 < 60:
    time.sleep(0.2)
if not st["connected"]:
    print(st["error"] or "Could not connect.")
    sys.exit(1)

import matplotlib
if args.snapshot:
    matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

nch, chans = st["nch"], st["chans"]
# Display filter: notch at the mains frequency, then band-pass 1-35 Hz. The band-pass
# alone damps 50 Hz by only about 16 dB, far too little for an open input that picks
# up mains hum of several millivolts.
SOS = np.vstack([signal.tf2sos(*signal.iirnotch(args.mains, 30.0, fs=FS)),
                 signal.butter(4, [1.0, 35.0], btype="bandpass", fs=FS, output="sos")])
show_filtered = [args.filter]

fig, axes = plt.subplots(nch, 1, figsize=(12, 1.2 * nch + 1), sharex=True, squeeze=False)
axes = axes[:, 0]
fig.subplots_adjust(left=0.09, right=0.98, top=0.94, bottom=0.06, hspace=0.15)
t = np.arange(N) / FS - args.window
lines = []
for i, ax in enumerate(axes):
    lines.append(ax.plot(t, np.zeros(N), lw=0.7, color="tab:blue")[0])
    ax.set_ylabel(f"ch {chans[i]}\nuV", fontsize=8)
    ax.tick_params(labelsize=7)
axes[-1].set_xlabel("time (s)")
axes[-1].set_xlim(-args.window, 0)
title = fig.suptitle("")


def on_key(event):
    if event.key == "f":
        show_filtered[0] = not show_filtered[0]


fig.canvas.mpl_connect("key_press_event", on_key)


def update(_frame=None):
    with lock:
        data = st["buf"].copy()
        received, lost = st["received"], st["lost"]
    n = min(received, N)
    if n < 10:
        return
    m = min(received, N + LEAD)
    for i in range(nch):
        if show_filtered[0]:
            # Causal filter over the longer history: no ringing at the newest
            # samples, and 5 s lead-in for the oldest ones shown.
            x = data[-m:, i]
            y, _ = signal.sosfilt(SOS, x, zi=signal.sosfilt_zi(SOS) * x[0])
            y = y[-n:]
        else:
            y = signal.detrend(data[-n:, i])
        lines[i].set_data(t[-n:], y)
        if args.ylim > 0:
            axes[i].set_ylim(-args.ylim, args.ylim)
        else:
            lo, hi = np.percentile(y, [0.5, 99.5])
            pad = max(hi - lo, 1.0) * 0.15
            axes[i].set_ylim(lo - pad, hi + pad)
    mode = f"filtered 1-35 Hz, {args.mains:g} Hz notch" if show_filtered[0] else "raw, drift removed"
    title.set_text(f"{st['label']} | last {n / FS:.0f} s of {received / FS:.0f} s received | "
                   f"lost samples: {lost} | "
                   f"gain x{'/'.join(f'{g:g}' for g in sorted(set(st['gains'])))} | {mode}" + ("" if st["connected"] else " | DISCONNECTED"))


try:
    if args.snapshot:
        time.sleep(args.snapshot)
        update()
        fig.savefig(args.out, dpi=110)
        print(f"Saved {args.out}")
    else:
        animation = FuncAnimation(fig, update, interval=250, cache_frame_data=False)
        plt.show()
finally:
    st["stop"] = True
    ble.join(timeout=5)
