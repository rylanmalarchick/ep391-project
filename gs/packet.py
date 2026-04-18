"""EP391 telemetry packet definition, parser, and calibration.

Wire format (ASCII, \r\n terminated):
    SEQ,VBAT,IBAT,ANGLE,T1,T2

Calibration constants are placeholders until each subsystem owner
provides their real values. Update the CAL dict below — the rest of
the ground station picks up the change automatically.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, asdict
from typing import Optional

FIELDS = ("seq", "vbat", "ibat", "angle", "t1", "t2")

# ── Calibration constants ─────────────────────────────────────────────────────
# Update these once each person provides their circuit parameters.
# All conversions assume AVCC = 5.0 V and 10-bit ADC (1023 counts = 5 V).

CAL = {
    # Person 1 — Power
    # V_bat = (count / 1023) * AVCC * divider_ratio
    "vbat_divider_ratio": 3.0,      # placeholder: replace with real ratio

    # I_bat = ((count / 1023) * AVCC) / (r_sense_ohms * amp_gain)
    "ibat_r_sense_ohms": 1.0,       # placeholder
    "ibat_amp_gain":     10.0,      # placeholder

    # Person 3 — Temperature (Steinhart-Hart / B-parameter model)
    # R_therm = R_series * (1023/count - 1)
    # T_K = 1 / (1/T0 + (1/B) * ln(R_therm / R0))
    # T_C = T_K - 273.15
    "t_r_series":  10000.0,         # series resistor, ohms
    "t1_r0":       10000.0,         # thermistor nominal R at T0
    "t1_t0":       298.15,          # T0 in kelvin (25 °C)
    "t1_b":        3950.0,          # B coefficient

    "t2_r0":       10000.0,
    "t2_t0":       298.15,
    "t2_b":        3950.0,

    "avcc": 5.0,
}


# ── Raw packet ────────────────────────────────────────────────────────────────

@dataclass
class Packet:
    seq:   int
    vbat:  int    # raw ADC counts
    ibat:  int    # raw ADC counts
    angle: int    # degrees 0..359
    t1:    int    # raw ADC counts (signed)
    t2:    int    # raw ADC counts (signed)

    def as_dict(self) -> dict:
        return asdict(self)


# ── Converted (physical units) packet ─────────────────────────────────────────

@dataclass
class PhysicalPacket:
    seq:       int
    v_bat:     float   # volts
    i_bat:     float   # amps
    angle:     int     # degrees
    t1_c:      float   # celsius
    t2_c:      float   # celsius


def _counts_to_volts(count: int, avcc: float = CAL["avcc"]) -> float:
    return (count / 1023.0) * avcc


def _thermistor_celsius(count: int, r_series: float,
                        r0: float, t0: float, b: float) -> float:
    """Convert raw ADC count to °C using B-parameter model.
    Returns NaN if count is 0 or 1023 (rail saturation).
    """
    if count <= 0 or count >= 1023:
        return float("nan")
    v = (count / 1023.0) * CAL["avcc"]
    r_therm = r_series * (CAL["avcc"] / v - 1.0)
    if r_therm <= 0:
        return float("nan")
    try:
        t_k = 1.0 / (1.0 / t0 + (1.0 / b) * math.log(r_therm / r0))
    except (ValueError, ZeroDivisionError):
        return float("nan")
    return t_k - 273.15


def to_physical(pkt: Packet) -> PhysicalPacket:
    """Convert raw ADC counts to physical units using CAL constants."""
    c = CAL
    v_bat = _counts_to_volts(pkt.vbat) * c["vbat_divider_ratio"]
    i_bat = _counts_to_volts(pkt.ibat) / (c["ibat_r_sense_ohms"] * c["ibat_amp_gain"])
    t1_c  = _thermistor_celsius(pkt.t1,  c["t_r_series"],
                                 c["t1_r0"], c["t1_t0"], c["t1_b"])
    t2_c  = _thermistor_celsius(pkt.t2,  c["t_r_series"],
                                 c["t2_r0"], c["t2_t0"], c["t2_b"])
    return PhysicalPacket(
        seq=pkt.seq, v_bat=v_bat, i_bat=i_bat,
        angle=pkt.angle, t1_c=t1_c, t2_c=t2_c,
    )


# ── Parser ────────────────────────────────────────────────────────────────────

def parse(line: str) -> Optional[Packet]:
    """Parse one CRLF-stripped line. Returns None on comment/blank/malformed."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(",")
    if len(parts) != 6:
        return None
    try:
        return Packet(
            seq=int(parts[0]),
            vbat=int(parts[1]),
            ibat=int(parts[2]),
            angle=int(parts[3]),
            t1=int(parts[4]),
            t2=int(parts[5]),
        )
    except ValueError:
        return None


def format(pkt: Packet) -> str:
    return f"{pkt.seq},{pkt.vbat},{pkt.ibat},{pkt.angle},{pkt.t1},{pkt.t2}\r\n"
