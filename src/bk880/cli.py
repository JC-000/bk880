"""Command-line interface: ``bk880 <command>``."""

from __future__ import annotations

import argparse
import csv
import json
import sys

from .meter import BK880, BK880Error, list_cp210x_ports, resolve_port, summarize

CSV_FIELDS = [
    "timestamp", "primary_param", "primary", "primary_unit",
    "secondary_param", "secondary", "secondary_unit", "deviation_pct", "bin",
]


def cmd_ports(args) -> None:
    for device, usb_serial, location in list_cp210x_ports():
        print(f"{device}\tserial {usb_serial}\tlocation {location}")


def cmd_idn(m: BK880, args) -> None:
    print(m.idn())


def cmd_config(m: BK880, args) -> None:
    print(json.dumps(m.get_config().to_dict(), indent=2))


def cmd_set(m: BK880, args) -> None:
    # Primary first: changing it resets the secondary and equivalent mode.
    if args.primary:
        m.set_primary(args.primary)
    if args.secondary:
        m.set_secondary(args.secondary)
    if args.freq:
        m.set_frequency(args.freq)
    if args.level:
        m.set_level(args.level)
    if args.equivalent:
        m.set_equivalent(args.equivalent)
    print(json.dumps(m.get_config().to_dict(), indent=2))


def cmd_fetch(m: BK880, args) -> None:
    readings = list(m.read_many(args.count, args.interval))
    for r in readings:
        print(json.dumps(r.to_dict()))
    if args.count > 1:
        print(json.dumps(summarize(readings), indent=2), file=sys.stderr)


def cmd_tol(m: BK880, args) -> None:
    if args.state == "on":
        m.set_tolerance(True, args.range)
    else:
        m.set_tolerance(False)
        if args.secondary:
            m.set_secondary(args.secondary)
    print(json.dumps(m.get_config().to_dict(), indent=2))


def cmd_record(m: BK880, args) -> None:
    if args.state in ("on", "off"):
        m.set_recording(args.state == "on")
    print(json.dumps({"recording": m.get_recording(), **m.get_recorded()}, indent=2))


def write_csv(path: str, readings) -> None:
    out = open(path, "w", newline="") if path != "-" else sys.stdout
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    n = 0
    try:
        for r in readings:
            writer.writerow(r.to_dict())
            out.flush()
            n += 1
    except KeyboardInterrupt:
        pass
    finally:
        if out is not sys.stdout:
            out.close()
        print(f"{n} readings logged", file=sys.stderr)


def cmd_log(m: BK880, args) -> None:
    write_csv(args.output, m.read_many(args.count or sys.maxsize, args.interval))


def cmd_stream(m: BK880, args) -> None:
    config = m.get_config()
    m.local()
    m.release_on_close = False  # a command would stop auto-fetch
    print(f"Configured as {config.primary}/{config.secondary or '-'}. Give the meter's "
          "USB button one short press to start auto-fetch; Ctrl-C to stop. The meter "
          "still switches itself off after its auto power-off delay (AoFF).", file=sys.stderr)
    def on_stall(gap: float) -> None:
        print(f"warning: no reading for {gap:.0f} s; the meter may have switched itself "
              "off (auto power-off). Turn it on and press the USB button to resume, or "
              "Ctrl-C to stop.", file=sys.stderr, flush=True)

    readings = m.stream(config, on_stall=on_stall)
    write_csv(args.output, (next(readings) for _ in range(args.count)) if args.count else readings)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bk880", description="B&K Precision 880 LCR meter")
    p.add_argument("--port", help="serial device (default: $BK880_PORT, the CP210x port with USB "
                   "serial $BK880_SERIAL or at $BK880_LOCATION, or auto-detect)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list CP210x serial ports, USB serials and locations (opens none)"
                   ).set_defaults(fn=cmd_ports, offline=True)
    sub.add_parser("idn", help="print instrument identity").set_defaults(fn=cmd_idn)
    sub.add_parser("config", help="print current settings as JSON").set_defaults(fn=cmd_config)

    s = sub.add_parser("set", help="change settings")
    s.add_argument("--primary", choices=["L", "C", "R", "Z", "DCR"], type=str.upper)
    s.add_argument("--secondary", choices=["D", "Q", "THETA", "ESR"], type=str.upper)
    s.add_argument("--freq", type=int, choices=[100, 120, 1000, 10000, 100000])
    s.add_argument("--level", type=float, choices=[0.3, 0.6, 1.0])
    s.add_argument("--equivalent", choices=["series", "parallel"])
    s.set_defaults(fn=cmd_set)

    f = sub.add_parser("fetch", help="take readings, print JSON lines")
    f.add_argument("-n", "--count", type=int, default=1)
    f.add_argument("-i", "--interval", type=float, default=0.0, help="seconds between readings")
    f.set_defaults(fn=cmd_fetch)

    lg = sub.add_parser("log", help="log readings to CSV until count or Ctrl-C")
    lg.add_argument("-o", "--output", default="-", help="CSV file (default stdout)")
    lg.add_argument("-n", "--count", type=int, default=0, help="0 = until Ctrl-C")
    lg.add_argument("-i", "--interval", type=float, default=1.0)
    lg.set_defaults(fn=cmd_log)

    st = sub.add_parser("stream", help="log the meter's auto-fetch output without polling")
    st.add_argument("-o", "--output", default="-", help="CSV file (default stdout)")
    st.add_argument("-n", "--count", type=int, default=0, help="0 = until Ctrl-C")
    st.set_defaults(fn=cmd_stream)

    t = sub.add_parser("tol", help="tolerance mode; 'on' captures the part in the socket as nominal")
    t.add_argument("state", choices=["on", "off"])
    t.add_argument("--range", type=int, choices=[1, 5, 10, 20], help="percent (with on)")
    t.add_argument("--secondary", choices=["D", "Q", "THETA", "ESR"], type=str.upper,
                   help="secondary to re-select (with off; tolerance mode clears it)")
    t.set_defaults(fn=cmd_tol)

    rc = sub.add_parser("record", help="the meter's own max/min/average of the primary")
    rc.add_argument("state", choices=["on", "off", "show"], nargs="?", default="show")
    rc.set_defaults(fn=cmd_record)

    args = p.parse_args(argv)
    if getattr(args, "offline", False):
        args.fn(args)
        return 0
    try:
        with BK880(resolve_port(args.port)) as m:
            args.fn(m, args)
    except BK880Error as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
