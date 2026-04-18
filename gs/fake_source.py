"""Dummy telemetry source for testing the ground station without hardware.

Generates realistic ADC counts that produce sensible physical values when
passed through the calibration in packet.py:
  - V_bat  ~ 9 V  (614 counts through /3 divider)
  - I_bat  ~ 50 mA (100 counts, 1-ohm shunt, gain 10)
  - Angle  : sweeps 0→359 at 3°/packet
  - T1/T2  : ~25 °C with small noise (Steinhart-Hart, 10k NTC)

Two modes:
  --stdout : print packets to stdout
  --pty    : create a pseudo-terminal and write to it
             (point reader.py --port <printed pty path>)

Usage:
    python fake_source.py --pty
    python fake_source.py --stdout
"""
from __future__ import annotations
import argparse
import math
import os
import random
import sys
import time

from packet import Packet, format as fmt_packet, CAL


# ── Count generators ──────────────────────────────────────────────────────────

def _volts_to_counts(v: float) -> int:
    """Convert a voltage to a 10-bit ADC count (clipped)."""
    return max(0, min(1023, round((v / CAL["avcc"]) * 1023)))


def _celsius_to_counts(t_c: float, r_series: float,
                       r0: float, t0_k: float, b: float) -> int:
    """Invert the Steinhart-Hart B-parameter model to get an ADC count."""
    t_k = t_c + 273.15
    r_therm = r0 * math.exp(b * (1.0 / t_k - 1.0 / t0_k))
    # Voltage divider: V_out = AVCC * R_series / (R_series + R_therm)
    v_out = CAL["avcc"] * r_series / (r_series + r_therm)
    return _volts_to_counts(v_out)


def gen(seq: int) -> Packet:
    c = CAL

    # Battery: ~9 V with slow drift and small noise
    t = seq * 1.0
    v_bat = 9.0 + 0.3 * math.sin(t / 30.0) + random.gauss(0, 0.02)
    # Scale back through divider so ADC sees it in 0-5 V range
    v_adc = v_bat / c["vbat_divider_ratio"]
    vbat_count = _volts_to_counts(v_adc)

    # Current: ~50 mA with occasional spikes
    i_bat = 0.05 + 0.01 * math.sin(t / 10.0) + random.gauss(0, 0.002)
    v_sense = i_bat * c["ibat_r_sense_ohms"] * c["ibat_amp_gain"]
    ibat_count = _volts_to_counts(v_sense)

    # Angle: slow sweep
    angle = (seq * 3) % 360

    # Temperatures: ~25 °C with gentle drift and noise
    t1_c = 25.0 + 5.0 * math.sin(t / 60.0) + random.gauss(0, 0.3)
    t2_c = 27.0 + 4.0 * math.sin(t / 45.0 + 1.0) + random.gauss(0, 0.3)
    t1_count = _celsius_to_counts(t1_c, c["t_r_series"],
                                   c["t1_r0"], c["t1_t0"], c["t1_b"])
    t2_count = _celsius_to_counts(t2_c, c["t_r_series"],
                                   c["t2_r0"], c["t2_t0"], c["t2_b"])

    return Packet(
        seq=seq & 0xFFFF,
        vbat=vbat_count,
        ibat=ibat_count,
        angle=angle,
        t1=t1_count,
        t2=t2_count,
    )


# ── Output modes ──────────────────────────────────────────────────────────────

def run_stdout(period: float) -> None:
    seq = 0
    sys.stdout.write("# EP391 fake_source (stdout)\r\n")
    sys.stdout.write("# fmt: SEQ,VBAT,IBAT,ANGLE,T1,T2\r\n")
    sys.stdout.flush()
    while True:
        sys.stdout.write(fmt_packet(gen(seq)))
        sys.stdout.flush()
        seq += 1
        time.sleep(period)


def run_pty(period: float) -> None:
    master, slave = os.openpty()
    slave_name = os.ttyname(slave)
    print(f"pty device: {slave_name}", file=sys.stderr)
    print(f"run:  python reader.py --port {slave_name} --log /tmp/fake.csv",
          file=sys.stderr)
    os.write(master, b"# EP391 fake_source (pty)\r\n")
    os.write(master, b"# fmt: SEQ,VBAT,IBAT,ANGLE,T1,T2\r\n")
    seq = 0
    while True:
        os.write(master, fmt_packet(gen(seq)).encode("ascii"))
        seq += 1
        time.sleep(period)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stdout", action="store_true")
    g.add_argument("--pty",    action="store_true")
    ap.add_argument("--period", type=float, default=1.0,
                    help="seconds between packets (default 1.0)")
    args = ap.parse_args()
    if args.stdout:
        run_stdout(args.period)
    else:
        run_pty(args.period)


if __name__ == "__main__":
    main()
