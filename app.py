#!/usr/bin/env python3
"""Split-flap display: web app and command line.

  python3 app.py                         start the web app (default)
  python3 app.py send "HELLO" -a left    show text, like the old i2c_transmit.php
  python3 app.py clear
  python3 app.py status                  what every module shows right now
  python3 app.py scan                    list I2C addresses that answer

Add --simulate to try everything without the display attached.
"""
import argparse
import logging
import os
import sys

from splitflap.controller import Controller, load_config
from splitflap.display import ANIMATIONS

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    parser = argparse.ArgumentParser(description="Split-flap display controller")
    parser.add_argument("--config", default=os.path.join(HERE, "config.json"))
    parser.add_argument("--simulate", action="store_true", help="use the built-in simulator")
    parser.add_argument("--port", type=int, help="override the port from config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web app (default)")
    send = sub.add_parser("send", help="show text on the display")
    send.add_argument("text")
    send.add_argument("-a", "--animation", default="start", choices=sorted(ANIMATIONS))
    send.add_argument("--same", action="store_true", help="skip modules that already show the character")
    send.add_argument("--wipe", action="store_true", help="blank the modules after the text")
    send.add_argument("--slowdown", type=int, default=1, help="stretch the animation (1-10)")
    send.add_argument("--noblock", action="store_true", help="return without waiting for the flaps")
    sub.add_parser("clear", help="blank every module")
    sub.add_parser("status", help="show what every module displays")
    sub.add_parser("scan", help="scan the I2C bus")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    ctrl = Controller(config, os.path.join(HERE, "data"), simulate=True if args.simulate else None)
    command = args.command or "serve"

    if command == "serve":
        from splitflap.server import serve
        ctrl.resume()
        serve(ctrl, config["host"], args.port or config["port"])
        return 0

    if ctrl.bus is None:
        print(ctrl.bus_error, file=sys.stderr)
        return 1

    if command == "scan":
        result = ctrl.scan()
        print("found:   ", " ".join(result["found"]) or "nothing")
        print("expected:", " ".join(result["expected"]))
        if result["missing"]:
            print("missing: ", " ".join(result["missing"]), "-> check wiring or the address in config.json")
        return 0 if not result["missing"] else 1

    if command == "status":
        statuses, error = ctrl.read_statuses(max_age=0)
        for i, s in enumerate(statuses):
            m = ctrl.geo.modules[i]
            ch = "moving" if s is not None and s >= 255 else repr(ctrl.charset.char_for_status(s))
            print("row %d col %2d  board 0x%02x/%d  %s" % (m.row, m.col, m.board.address, m.index, ch))
        if error:
            print("error:", error, file=sys.stderr)
        return 1 if error else 0

    if command == "clear":
        ctrl.clear()
    else:
        shown = ctrl.show(text=args.text, animation=args.animation, same=args.same,
                          wipe=args.wipe, slowdown=args.slowdown)
        print("sending %r" % shown)
    if command == "clear" or not args.noblock:
        ctrl.wait()
    if ctrl.last_error:
        print("error:", ctrl.last_error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
