"""
monitor.py
Real-time audio pass-through: BlackHole → your Bluetooth headphones.

Reads audio from BlackHole (where Zoom sends its output) and plays it
back through your current system output device simultaneously.
This means ffmpeg captures from BlackHole for recording while you
hear the call normally through AirPods/Bose.

Install:  pip install sounddevice numpy
Run:      python monitor.py
          python monitor.py --list        (see available devices)
          python monitor.py --output "Christopher's AirPods Pro"
"""

import argparse
import signal
import sys
import time

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
import os

load_dotenv()

BLACKHOLE_DEVICE = os.getenv("BLACKHOLE_DEVICE", "BlackHole 2ch")
SAMPLE_RATE      = 48000   # BlackHole's native rate — avoids resampling
BLOCK_SIZE       = 1024    # ~21ms latency at 48kHz; lower = less lag, more CPU
CHANNELS         = 2       # stereo


def find_device(name: str, kind: str) -> int:
    """Find a device by partial name match. kind = 'input' or 'output'."""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if name.lower() in d["name"].lower() and d[f"max_{kind}_channels"] > 0:
            return i
    raise ValueError(
        f"Device '{name}' not found as {kind}.\n"
        f"Run with --list to see available devices."
    )


def get_default_output_name() -> str:
    """Return the name of the current default output device."""
    return sd.query_devices(sd.default.device[1])["name"]


def list_devices():
    print("\nAvailable audio devices:\n")
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        ins  = d["max_input_channels"]
        outs = d["max_output_channels"]
        if ins > 0 or outs > 0:
            tag = []
            if ins  > 0: tag.append(f"in:{ins}")
            if outs > 0: tag.append(f"out:{outs}")
            print(f"  [{i:2d}] {d['name']}  ({', '.join(tag)})")
    print()


def run_passthrough(output_device_name: str | None = None):
    output_name = output_device_name or get_default_output_name()

    print(f"  Input:   {BLACKHOLE_DEVICE}")
    print(f"  Output:  {output_name}")
    print(f"  Rate:    {SAMPLE_RATE}Hz  Block: {BLOCK_SIZE} samples "
          f"(~{1000 * BLOCK_SIZE / SAMPLE_RATE:.0f}ms latency)")
    print("  Ctrl+C to stop\n")

    try:
        input_device  = find_device(BLACKHOLE_DEVICE, "input")
        output_device = find_device(output_name, "output")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    def callback(indata, outdata, frames, time_info, status):
        if status:
            print(f"  [audio] {status}", flush=True)
        outdata[:] = indata  # direct pass-through — no processing

    with sd.Stream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        channels=CHANNELS,
        dtype="float32",
        device=(input_device, output_device),
        callback=callback,
        latency="low",
    ):
        print("  Pass-through active ✓")
        # Block until Ctrl+C
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n  Stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Route BlackHole audio to your headphones in real time"
    )
    parser.add_argument("--list",   action="store_true", help="List audio devices")
    parser.add_argument("--output", type=str, default=None,
                        help="Output device name (partial match). "
                             "Default: current system output.")
    args = parser.parse_args()

    if args.list:
        list_devices()
        return

    run_passthrough(args.output)


if __name__ == "__main__":
    main()
