"""Read (and with --write NEW, set) the USB serial string of the single attached CP2102.

Usage: uv run --with pyusb python scripts/cp210x_serial.py [--write NEW]
Needs Homebrew libusb. Unplug every other CP210x first. Replug afterwards to see the change.

Uses the CP210x vendor configuration requests (bRequest 0xFF), as cp210x-program does.
Refuses unless exactly one 10C4:EA60 device is attached.
"""
import sys
import usb.core, usb.util, usb.backend.libusb1

be = usb.backend.libusb1.get_backend(find_library=lambda _: "/opt/homebrew/lib/libusb-1.0.dylib")
devs = list(usb.core.find(find_all=True, idVendor=0x10C4, idProduct=0xEA60, backend=be))
if len(devs) != 1:
    sys.exit(f"refusing: {len(devs)} CP210x (10C4:EA60) devices attached, need exactly 1")
d = devs[0]
print(f"bus {d.bus} address {d.address} ports {d.port_numbers} bcdDevice {d.bcdDevice:#06x}")

def rd(reg, n):
    return bytes(d.ctrl_transfer(0xC0, 0xFF, reg, 0, n))

part = rd(0x370B, 1)[0]
lock = rd(0x370A, 1)[0]
print(f"part number {part:#04x} (0x02 = CP2102), lock {lock:#04x} (0xff = unlocked)")
print("serial string:", repr(usb.util.get_string(d, d.iSerialNumber)))

if len(sys.argv) == 3 and sys.argv[1] == "--write":
    new = sys.argv[2]
    if part != 0x02 or lock != 0xFF:
        sys.exit("refusing to write: not an unlocked CP2102")
    enc = new.encode("utf-16-le")
    desc = bytes([len(enc) + 2, 0x03]) + enc
    if len(desc) > 63 * 2 + 2:
        sys.exit("serial too long")
    n = d.ctrl_transfer(0x40, 0xFF, 0x3704, 0, desc)
    print(f"wrote {n} bytes")
