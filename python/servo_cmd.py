"""Send commands to the sweep_test sketch and print the Arduino's replies.

Usage:
    python3 servo_cmd.py 1500 1300 1700 1500      # absolute pulse widths (us)
    python3 servo_cmd.py 1500 +5 +5 +5 --pause 2  # relative steps, 2 s apart

Opening the serial port restarts the Uno, so we wait for its "ready" line first.
"""
import argparse
import time

import serial

PORT = "/dev/cu.usbmodem31201"
BAUD = 115200


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("commands", nargs="+", help="e.g. 1500, +5, -5, ?")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="seconds to wait after each command")
    parser.add_argument("--port", default=PORT)
    args = parser.parse_args()

    with serial.Serial(args.port, BAUD, timeout=3) as board:
        # Wait for the Uno to reboot and say "ready".
        deadline = time.time() + 5
        while time.time() < deadline:
            line = board.readline().decode(errors="replace").strip()
            if line:
                print("arduino:", line)
            if line == "ready":
                break

        for cmd in args.commands:
            board.write((cmd + "\n").encode())
            print(f"sent: {cmd:>6}   arduino:", board.readline().decode().strip())
            time.sleep(args.pause)


if __name__ == "__main__":
    main()
