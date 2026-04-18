#!/usr/bin/env python3.12
"""EP391 real-time telemetry dashboard.

Tails the CSV produced by reader.py and displays a live 5-panel dashboard:
  - Battery voltage (V)
  - Payload current (A)
  - Sun angle (degrees, polar + time-series)
  - Temperature T1 and T2 (°C)

All values are converted to physical units using calibration constants
in packet.py. Raw ADC counts are never shown to the user.

Usage:
    python3.12 plot.py --log /tmp/telemetry.csv
    python plot.py --log /tmp/telemetry.csv --window 120
    python plot.py --log /tmp/telemetry.csv --raw   # show counts if cal not set
    python plot.py --log /tmp/telemetry.csv --raw --tracking-one-led
"""
from __future__ import annotations
import argparse
import math
import time
from collections import deque
from pathlib import Path

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.animation import FuncAnimation
from matplotlib.patches import FancyArrowPatch

from packet import CAL, parse, to_physical

# ── Layout constants ──────────────────────────────────────────────────────────
WINDOW_DEFAULT = 120   # seconds of history to show
UPDATE_MS      = 500   # animation interval (ms) — well under the 1 Hz spec
ARROW_COLOR    = "#e84040"
GRID_ALPHA     = 0.25
LINE_WIDTH     = 1.5


# ── CSV tail ──────────────────────────────────────────────────────────────────

class CsvTailer:
    """Non-blocking CSV tail reader.

    Tracks file position purely via readline()/tell()/seek() — never mixes
    with csv.DictReader iteration, which would trigger Python's file
    iterator read-ahead buffering and silently corrupt the position.
    """

    HEADERS = ("t_host", "seq", "vbat", "ibat", "angle", "t1", "t2")

    def __init__(self, path: Path):
        self.path = path
        self.f = path.open("r")
        first = self.f.readline()
        if not first.startswith("t_host"):
            self.f.seek(0)

    def pull(self) -> list[dict]:
        """Return every complete row available right now. Non-blocking."""
        rows = []
        while True:
            pos = self.f.tell()
            line = self.f.readline()
            if not line or not line.endswith("\n"):
                self.f.seek(pos)
                return rows
            parts = line.rstrip("\r\n").split(",")
            if len(parts) == 7:
                rows.append(dict(zip(self.HEADERS, parts)))


# ── Dashboard ─────────────────────────────────────────────────────────────────

def build_figure(*, tracking_one_led: bool, raw: bool):
    """Create and return (fig, axes_dict, artists_dict)."""
    fig = plt.figure(figsize=(13, 8), facecolor="#1a1a2e")
    fig.canvas.manager.set_window_title("EP391 Telemetry Ground Station")

    gs = gridspec.GridSpec(
        3, 3,
        figure=fig,
        hspace=0.45,
        wspace=0.38,
        left=0.07, right=0.97,
        top=0.91, bottom=0.07,
    )

    # Panel positions:
    #  [0,0] V_bat    [0,1] I_bat    [0,2] polar angle (spans rows 0-1)
    #  [1,0] T1       [1,1] T2
    #  [2,:]  status bar (text only, no axes)

    ax_vbat  = fig.add_subplot(gs[0, 0], facecolor="#0d0d1a")
    ax_ibat  = fig.add_subplot(gs[0, 1], facecolor="#0d0d1a")
    ax_polar = fig.add_subplot(gs[0:2, 2], polar=True, facecolor="#0d0d1a")
    ax_t1    = fig.add_subplot(gs[1, 0], facecolor="#0d0d1a")
    ax_t2    = fig.add_subplot(gs[1, 1], facecolor="#0d0d1a")

    def style_ax(ax, title, ylabel, color):
        ax.set_facecolor("#0d0d1a")
        ax.tick_params(colors="#cccccc", labelsize=8)
        ax.set_title(title, color=color, fontsize=9, pad=4)
        ax.set_ylabel(ylabel, color="#aaaaaa", fontsize=8)
        ax.set_xlabel("time (s)", color="#aaaaaa", fontsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#333355")
        ax.grid(True, alpha=GRID_ALPHA, color="#4444aa")
        ax.xaxis.label.set_color("#aaaaaa")

    if tracking_one_led:
        t1_title = "LED Sensor"
        t1_ylabel = "counts" if raw else "raw"
        t2_title = "Spare / Placeholder"
        t2_ylabel = "counts" if raw else "value"
    else:
        t1_title = "Temperature T1"
        t1_ylabel = "cts" if raw else "°C"
        t2_title = "Temperature T2"
        t2_ylabel = "cts" if raw else "°C"

    style_ax(ax_vbat, "Battery Voltage",  "cts" if raw else "V",   "#4fc3f7")
    style_ax(ax_ibat, "Payload Current",  "cts" if raw else "A",   "#81c784")
    style_ax(ax_t1,   t1_title,           t1_ylabel,               "#ffb74d")
    style_ax(ax_t2,   t2_title,           t2_ylabel,               "#ff8a65")

    # Polar (sun angle) styling
    ax_polar.set_facecolor("#0d0d1a")
    ax_polar.tick_params(colors="#cccccc", labelsize=7)
    ax_polar.set_title("Sun Angle", color="#ce93d8", fontsize=9, pad=10)
    ax_polar.set_theta_zero_location("N")
    ax_polar.set_theta_direction(-1)          # clockwise like a compass
    ax_polar.set_rticks([])
    ax_polar.set_rlim(0, 1)
    ax_polar.grid(True, alpha=GRID_ALPHA, color="#4444aa")
    for spine in ax_polar.spines.values():
        spine.set_edgecolor("#333355")

    # Time-series lines
    ln_vbat, = ax_vbat.plot([], [], color="#4fc3f7", lw=LINE_WIDTH)
    ln_ibat, = ax_ibat.plot([], [], color="#81c784", lw=LINE_WIDTH)
    ln_t1,   = ax_t1.plot(  [], [], color="#ffb74d", lw=LINE_WIDTH)
    ln_t2,   = ax_t2.plot(  [], [], color="#ff8a65", lw=LINE_WIDTH)

    # Polar arrow (will be redrawn each frame)
    arrow_holder = [None]   # mutable container so update() can replace it

    # Current-value readouts (large text in each panel corner)
    def add_readout(ax, color):
        return ax.text(
            0.97, 0.92, "---",
            transform=ax.transAxes,
            ha="right", va="top",
            fontsize=11, fontweight="bold",
            color=color, family="monospace",
        )

    ro_vbat = add_readout(ax_vbat, "#4fc3f7")
    ro_ibat = add_readout(ax_ibat, "#81c784")
    ro_t1   = add_readout(ax_t1,   "#ffb74d")
    ro_t2   = add_readout(ax_t2,   "#ff8a65")
    ro_angle = ax_polar.text(
        0, 0, "---°",
        ha="center", va="center",
        fontsize=12, fontweight="bold",
        color="#ce93d8", family="monospace",
    )

    # Status bar
    status_text = fig.text(
        0.5, 0.01,
        "waiting for data…",
        ha="center", va="bottom",
        fontsize=8, color="#888888", family="monospace",
    )

    fig.suptitle(
        "EP391 Solar Payload — Telemetry Ground Station",
        color="#ddddff", fontsize=12, y=0.97,
    )

    axes = dict(vbat=ax_vbat, ibat=ax_ibat, polar=ax_polar, t1=ax_t1, t2=ax_t2)
    artists = dict(
        ln_vbat=ln_vbat, ln_ibat=ln_ibat, ln_t1=ln_t1, ln_t2=ln_t2,
        ro_vbat=ro_vbat, ro_ibat=ro_ibat, ro_t1=ro_t1, ro_t2=ro_t2,
        ro_angle=ro_angle, arrow_holder=arrow_holder, status=status_text,
    )
    return fig, axes, artists


def main() -> None:
    ap = argparse.ArgumentParser(description="EP391 real-time telemetry dashboard")
    ap.add_argument("--log",    default="telemetry.csv", help="CSV log from reader.py")
    ap.add_argument("--window", type=int, default=WINDOW_DEFAULT,
                    help="seconds of history to display (default 120)")
    ap.add_argument("--raw",    action="store_true",
                    help="plot raw ADC counts instead of physical units")
    ap.add_argument("--tracking-one-led", action="store_true",
                    help="interpret T1 as LED raw counts and T2 as spare placeholder")
    args = ap.parse_args()

    path = Path(args.log)
    while not path.exists():
        print(f"waiting for {path} …")
        time.sleep(0.5)

    tailer = CsvTailer(path)

    bufs: dict[str, deque] = {
        k: deque(maxlen=args.window)
        for k in ("t", "vbat", "ibat", "angle", "t1", "t2")
    }
    t0_ref: list[float | None] = [None]

    fig, axes, art = build_figure(tracking_one_led=args.tracking_one_led,
                                  raw=args.raw)

    def update(_frame):
        # Drain every row that has arrived since the last frame.
        for row in tailer.pull():
            try:
                t_host = float(row["t_host"])
                raw = parse(
                    f"{row['seq']},{row['vbat']},{row['ibat']},"
                    f"{row['angle']},{row['t1']},{row['t2']}"
                )
            except (KeyError, ValueError):
                continue
            if raw is None:
                continue

            if t0_ref[0] is None:
                t0_ref[0] = t_host
            t_rel = t_host - t0_ref[0]

            if args.raw:
                bufs["t"].append(t_rel)
                bufs["vbat"].append(raw.vbat)
                bufs["ibat"].append(raw.ibat)
                bufs["angle"].append(raw.angle)
                bufs["t1"].append(raw.t1)
                bufs["t2"].append(raw.t2)
            else:
                phys = to_physical(raw)
                bufs["t"].append(t_rel)
                bufs["vbat"].append(phys.v_bat)
                bufs["ibat"].append(phys.i_bat)
                bufs["angle"].append(phys.angle)
                bufs["t1"].append(phys.t1_c)
                bufs["t2"].append(phys.t2_c)

        if not bufs["t"]:
            return []

        ts = list(bufs["t"])

        # ── time-series panels ────────────────────────────────────────────
        def refresh_line(ln, key, ax, fmt, readout):
            vals = list(bufs[key])
            ln.set_data(ts, vals)
            ax.relim()
            ax.autoscale_view()
            # x-axis: show last window seconds
            if ts:
                ax.set_xlim(max(0, ts[-1] - args.window), ts[-1] + 1)
            last = vals[-1] if vals else None
            if last is not None and not (isinstance(last, float) and math.isnan(last)):
                readout.set_text(fmt.format(last))
            else:
                readout.set_text("---")

        if args.raw:
            refresh_line(art["ln_vbat"], "vbat", axes["vbat"], "{:.0f} cts", art["ro_vbat"])
            refresh_line(art["ln_ibat"], "ibat", axes["ibat"], "{:.0f} cts", art["ro_ibat"])
            refresh_line(art["ln_t1"],   "t1",   axes["t1"],   "{:.0f} cts", art["ro_t1"])
            refresh_line(art["ln_t2"],   "t2",   axes["t2"],   "{:.0f} cts", art["ro_t2"])
        else:
            refresh_line(art["ln_vbat"], "vbat", axes["vbat"], "{:.2f} V",  art["ro_vbat"])
            refresh_line(art["ln_ibat"], "ibat", axes["ibat"], "{:.3f} A",  art["ro_ibat"])
            refresh_line(art["ln_t1"],   "t1",   axes["t1"],   "{:.1f} °C", art["ro_t1"])
            refresh_line(art["ln_t2"],   "t2",   axes["t2"],   "{:.1f} °C", art["ro_t2"])

        # ── polar / angle panel ───────────────────────────────────────────
        angle_deg = bufs["angle"][-1]
        angle_rad = math.radians(angle_deg)

        # Remove previous arrow
        if art["arrow_holder"][0] is not None:
            art["arrow_holder"][0].remove()

        arrow = axes["polar"].annotate(
            "",
            xy=(angle_rad, 0.85),
            xytext=(0, 0),
            arrowprops=dict(
                arrowstyle="-|>",
                color=ARROW_COLOR,
                lw=2.0,
                mutation_scale=18,
            ),
            xycoords="data",
            textcoords="data",
            annotation_clip=False,
        )
        art["arrow_holder"][0] = arrow
        art["ro_angle"].set_text(f"{int(angle_deg)}°")

        # ── status bar ────────────────────────────────────────────────────
        seq_val = bufs["t"]  # use time length as proxy for seq count
        elapsed = ts[-1] if ts else 0
        n = len(ts)
        art["status"].set_text(
            f"samples: {n}   elapsed: {elapsed:.0f} s   "
            f"last update: {time.strftime('%H:%M:%S')}   "
            f"{'RAW COUNTS' if args.raw else 'calibrated'}"
            f"{'   one-led tracking mode' if args.tracking_one_led else ''}"
        )

        return [
            art["ln_vbat"], art["ln_ibat"], art["ln_t1"], art["ln_t2"],
            art["ro_vbat"], art["ro_ibat"], art["ro_t1"], art["ro_t2"],
            art["ro_angle"], art["status"],
        ]

    _anim = FuncAnimation(
        fig, update,
        interval=UPDATE_MS,
        blit=False,
        cache_frame_data=False,
    )

    plt.show()


if __name__ == "__main__":
    main()
