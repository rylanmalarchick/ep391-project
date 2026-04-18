"""Serial reader + CSV logger for EP391 telemetry.

Reads CRLF-terminated ASCII packets from a serial port (real FT232 or a
pty from fake_source.py), parses them, appends to a CSV log, and echoes
one-line summaries to stdout.

Usage:
    python reader.py --port /dev/ttyUSB0 --log telemetry.csv
    python reader.py --port /dev/pts/5  --log telemetry.csv   # pty
    python reader.py --port /dev/ttyUSB0 --log telemetry.csv --tracking-one-led
"""
from __future__ import annotations
import argparse
import csv
import sys
import time
from pathlib import Path

from packet import FIELDS, parse

try:
    import serial  # pyserial
except ImportError:
    serial = None  # type: ignore


def open_port(port: str, baud: int):
    if serial is None:
        raise SystemExit("pyserial not installed: pip install pyserial")
    return serial.Serial(port, baudrate=baud, timeout=1.0)


def iter_lines(ser):
    buf = bytearray()
    while True:
        chunk = ser.read(64)
        if not chunk:
            continue
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            yield line.decode("ascii", errors="replace").rstrip("\r")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--log", default="telemetry.csv")
    ap.add_argument("--tracking-one-led", action="store_true",
                    help="label T1 as LED raw counts and T2 as spare placeholder")
    args = ap.parse_args()

    log_path = Path(args.log)
    new_file = not log_path.exists()
    log_f = log_path.open("a", newline="")
    writer = csv.writer(log_f)
    if new_file:
        writer.writerow(["t_host", *FIELDS])
        log_f.flush()

    ser = open_port(args.port, args.baud)
    print(f"reading {args.port} @ {args.baud}, logging to {log_path}",
          file=sys.stderr)

    try:
        for line in iter_lines(ser):
            pkt = parse(line)
            if pkt is None:
                if line.strip():
                    print(f"[skip] {line}", file=sys.stderr)
                continue
            t = time.time()
            writer.writerow([f"{t:.3f}", pkt.seq, pkt.vbat, pkt.ibat,
                             pkt.angle, pkt.t1, pkt.t2])
            log_f.flush()
            if args.tracking_one_led:
                print(f"seq={pkt.seq:5d} vbat={pkt.vbat:4d} ibat={pkt.ibat:4d} "
                      f"angle={pkt.angle:3d} led={pkt.t1:4d} spare={pkt.t2:4d}")
            else:
                print(f"seq={pkt.seq:5d} vbat={pkt.vbat:4d} ibat={pkt.ibat:4d} "
                      f"angle={pkt.angle:3d} t1={pkt.t1:4d} t2={pkt.t2:4d}")
    except KeyboardInterrupt:
        pass
    finally:
        log_f.close()
        ser.close()


if __name__ == "__main__":
    main()
