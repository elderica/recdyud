"""USB access to the DY-UD200.

The command set and the initialisation sequence follow BonDriver_dyud
(``cDY_UD200::OpenTuner`` and friends).  The firmware update commands of the
original driver are intentionally not implemented.
"""

import array
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import usb.backend.libusb1
import usb.core
import usb.util

from . import nativelib
from .protocol import RESPONSE_SIZE, CommandCodec, ProtocolError, t1_block

log = logging.getLogger(__name__)

VENDOR_ID = 0x1C11
PRODUCT_ID = 0x1004

EP_COMMAND_OUT = 0x02
EP_RESPONSE_IN = 0x84
EP_TS_IN = 0x86

INTERFACE = 0

COMMAND_TIMEOUT_MS = 3000
# 188 * 512: a whole number of TS packets and of 512-byte USB packets.
TS_READ_SIZE = 188 * 512

SEGMENT_MODE_FULLSEG = 0x00020000
SEGMENT_MODE_ONESEG = 0x00010000

# Lock status values above this mean "TS locked" (BonDriver: GetLockStatus() > 8).
LOCK_THRESHOLD = 8

# Initialisation commands (type 0x0f, AES-256).  Bytes 16..31 become the AES-128
# key of the following commands.  The "new" variant is used by firmware 1.0.5-2.
_CMD_INIT = bytes.fromhex("0ff0e02000000000238dc0d00ed03875e838dfeb337d3952e378ebd1b784ef65009a0f3a")
_CMD_INIT_NEW = bytes.fromhex("0ff0e020000000007b3651e5217c7b1ce838dfeb337d3952e378ebd1b784ef6500000000")


def _cmd(code: int, param: bytes = bytes(4), length: int = 4) -> bytes:
    return bytes([0x1F, 0xF0, code, length]) + param


CMD_INIT_NF = _cmd(0xE3)
CMD_CLOSE = _cmd(0xE2)
CMD_GET_FIRMWARE_VERSION = _cmd(0x9F)
CMD_GET_SERIAL = _cmd(0x95)
CMD_GET_LOCK_STATUS = _cmd(0xB1, bytes([0x26, 0x0A, 0x00, 0x00]))
CMD_GET_SIGNAL_LEVEL = _cmd(0xD5)
CMD_START_TS = _cmd(0xB4)
CMD_STOP_TS = _cmd(0xB5)
CMD_BCAS_DEACTIVATE = bytes.fromhex("1ff0f0050200420000")
CMD_BCAS_ACTIVATE = bytes.fromhex("1ff0f0050200410000")

# Firmware versions BonDriver_dyud was tested with.
KNOWN_FIRMWARE = {0x01000202, 0x01000502, 0x01000503}
FIRMWARE_NEEDS_REINIT = 0x01000502


class DeviceError(Exception):
    pass


class DeviceNotFound(DeviceError):
    pass


class DeviceBusy(DeviceError):
    pass


@dataclass(frozen=True)
class FirmwareVersion:
    raw: int

    def __str__(self) -> str:
        r = self.raw
        return f"{r >> 24}.{(r >> 16) & 0xFF}.{(r >> 8) & 0xFF}-{r & 0xFF}"

    @property
    def known(self) -> bool:
        return self.raw in KNOWN_FIRMWARE


@dataclass(frozen=True)
class SignalLevel:
    valid: bool
    raw: int  # 24-bit, 8.16 fixed point

    @property
    def value(self) -> float:
        """The value BonDriver_dyud reports from GetSignalLevel()."""
        return self.raw / 65536.0 * 20.0 if self.valid else 0.0


@dataclass(frozen=True)
class DeviceLocation:
    bus: int
    address: int
    port_path: str

    def __str__(self) -> str:
        return f"{self.bus:03d}:{self.address:03d} (port {self.port_path})"


def _backend():
    path = nativelib.find_library(nativelib.LIBUSB)
    backend = usb.backend.libusb1.get_backend(find_library=lambda _name: path)
    if backend is None:
        raise DeviceError(f"failed to load libusb from {path}")
    return backend


def _location(dev: usb.core.Device) -> DeviceLocation:
    ports = ".".join(str(p) for p in (dev.port_numbers or ()))
    return DeviceLocation(dev.bus, dev.address, f"{dev.bus}-{ports}" if ports else str(dev.bus))


def enumerate_devices() -> list[tuple[DeviceLocation, usb.core.Device]]:
    devices = usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=PRODUCT_ID, backend=_backend())
    result = [(_location(d), d) for d in devices]
    result.sort(key=lambda item: (item[0].bus, item[0].port_path, item[0].address))
    return result


def _matches(selector: str, index: int, loc: DeviceLocation) -> bool:
    s = selector.strip()
    if s.isdigit():
        return int(s) == index
    if s.startswith("/dev/bus/usb/"):
        parts = s.removeprefix("/dev/bus/usb/").split("/")
        return len(parts) == 2 and (int(parts[0]), int(parts[1])) == (loc.bus, loc.address)
    if ":" in s:
        bus, _, addr = s.partition(":")
        return (int(bus), int(addr)) == (loc.bus, loc.address)
    return s == loc.port_path


def _select(selector: str | None) -> Iterator[tuple[DeviceLocation, usb.core.Device]]:
    devices = enumerate_devices()
    if not devices:
        raise DeviceNotFound(f"DY-UD200 ({VENDOR_ID:04x}:{PRODUCT_ID:04x}) not found")
    if selector is None or selector in ("", "auto"):
        yield from devices
        return
    matched = [d for i, d in enumerate(devices) if _matches(selector, i, d[0])]
    if not matched:
        found = ", ".join(str(loc) for loc, _ in devices)
        raise DeviceNotFound(f"no DY-UD200 matches {selector!r} (found: {found})")
    yield from matched


class DyUd200:
    """A claimed DY-UD200.  Use :func:`open_device` to obtain one."""

    def __init__(self, dev: usb.core.Device, location: DeviceLocation) -> None:
        self._dev = dev
        self.location = location
        self._codec = CommandCodec()
        self._io_lock = threading.Lock()
        self._bcas_pcb = 0x00
        self.firmware: FirmwareVersion | None = None
        self.serial: str | None = None
        self._claimed = False

    # -- lifecycle ---------------------------------------------------------

    def _claim(self) -> None:
        dev = self._dev
        try:
            try:
                if dev.is_kernel_driver_active(INTERFACE):
                    dev.detach_kernel_driver(INTERFACE)
            except NotImplementedError, usb.core.USBError:
                pass
            try:
                cfg = dev.get_active_configuration()
            except usb.core.USBError:
                cfg = None
            if cfg is None or cfg.bConfigurationValue != 1:
                dev.set_configuration(1)
            usb.util.claim_interface(dev, INTERFACE)
        except usb.core.USBError as e:
            usb.util.dispose_resources(dev)
            if e.errno == 16:  # EBUSY
                raise DeviceBusy(f"DY-UD200 {self.location} is in use") from e
            if e.errno == 13:  # EACCES
                raise DeviceError(
                    f"permission denied for DY-UD200 {self.location}; install the udev rule (see README)"
                ) from e
            raise DeviceError(f"failed to open DY-UD200 {self.location}: {e}") from e
        self._claimed = True

    def _drain(self, endpoint: int, size: int, limit: int = 64) -> int:
        """Discard stale data left by a previous (possibly killed) process."""
        total = 0
        for _ in range(limit):
            try:
                total += len(self._dev.read(endpoint, size, timeout=20))
            except usb.core.USBTimeoutError:
                break
            except usb.core.USBError:
                break
        return total

    def initialize(self) -> None:
        """Run the initialisation sequence of BonDriver_dyud's OpenTuner()."""
        stale = self._drain(EP_RESPONSE_IN, 512)
        if stale:
            log.debug("discarded %d stale response bytes", stale)

        self.command(_CMD_INIT, check=False)
        self.stop_streaming()
        # "New firmware init" - returns an error code on known firmware; ignored.
        self.command(CMD_INIT_NF, check=False)

        self.firmware = self.get_firmware_version()
        log.debug("firmware version %s", self.firmware)
        if not self.firmware.known:
            log.warning("firmware %s has not been tested with BonDriver_dyud", self.firmware)
        if self.firmware.raw == FIRMWARE_NEEDS_REINIT:
            self.command(CMD_CLOSE, check=False)
            self.command(_CMD_INIT_NEW, check=False)
            self.stop_streaming()
            self.command(CMD_INIT_NF, check=False)

        self.serial = self.get_serial_number()
        self.set_segment_mode(SEGMENT_MODE_FULLSEG)

        stale = self._drain(EP_TS_IN, TS_READ_SIZE)
        if stale:
            log.debug("discarded %d stale TS bytes", stale)

    def close(self) -> None:
        """Stop streaming, send the close command and release the device."""
        if self._claimed:
            try:
                self.stop_streaming()
                self.command(CMD_CLOSE, check=False)
            except (DeviceError, usb.core.USBError) as e:
                log.debug("close: %s", e)
        self.release()

    def release(self) -> None:
        """Release the USB interface without sending any command."""
        if self._claimed:
            try:
                usb.util.release_interface(self._dev, INTERFACE)
            except usb.core.USBError:
                pass
            self._claimed = False
        usb.util.dispose_resources(self._dev)

    def __enter__(self) -> DyUd200:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- commands ------------------------------------------------------------

    def command(self, cmd: bytes, *, check: bool = True, timeout: int = COMMAND_TIMEOUT_MS) -> bytes:
        """Send a command and return the decrypted 128-byte response."""
        frame = self._codec.encode(cmd)
        with self._io_lock:
            try:
                self._dev.write(EP_COMMAND_OUT, frame, timeout=timeout)
                raw = self._dev.read(EP_RESPONSE_IN, 512, timeout=timeout)
            except usb.core.USBError as e:
                raise DeviceError(f"command {cmd[2]:#04x} failed: {e}") from e
        if len(raw) < RESPONSE_SIZE:
            raise DeviceError(f"command {cmd[2]:#04x}: short response ({len(raw)} bytes)")
        try:
            res = self._codec.decode(bytes(raw))
        except ProtocolError as e:
            raise DeviceError(f"command {cmd[2]:#04x}: {e}") from e
        if check and res[0] != 0:
            raise DeviceError(f"command {cmd[2]:#04x} returned status {res[0]:#04x}")
        return res

    def get_firmware_version(self) -> FirmwareVersion:
        res = self.command(CMD_GET_FIRMWARE_VERSION, check=False)
        return FirmwareVersion(int.from_bytes(res[2:6], "big"))

    def get_serial_number(self) -> str:
        res = self.command(CMD_GET_SERIAL, check=False)
        return res[2:18].split(b"\0", 1)[0].decode("ascii", "replace").strip()

    def get_lock_status(self) -> int:
        res = self.command(CMD_GET_LOCK_STATUS, check=False)
        return res[3] if res[0] == 0 else 0

    def get_signal_level(self) -> SignalLevel:
        res = self.command(CMD_GET_SIGNAL_LEVEL, check=False)
        if res[0] != 0:
            return SignalLevel(False, 0)
        return SignalLevel(res[2] != 0, int.from_bytes(res[3:6], "big"))

    def set_segment_mode(self, mode: int) -> None:
        self.command(_cmd(0xD1, mode.to_bytes(4, "big")))

    def set_frequency(self, khz: int) -> None:
        self.command(_cmd(0xD2, khz.to_bytes(4, "big") + bytes(2), length=6))

    def start_streaming(self) -> None:
        self.command(CMD_START_TS, check=False)

    def stop_streaming(self) -> None:
        self.command(CMD_STOP_TS, check=False)

    def tune(self, khz: int) -> None:
        """Tune to a frequency and start the TS stream (BonDriver: SetChannel)."""
        self.stop_streaming()
        self.set_segment_mode(SEGMENT_MODE_FULLSEG)
        self.set_frequency(khz)
        self.start_streaming()

    def wait_lock(self, timeout: float, interval: float = 0.1) -> int:
        """Poll the lock status until it exceeds LOCK_THRESHOLD.  Returns the last status."""
        deadline = time.monotonic() + timeout
        status = 0
        while True:
            status = self.get_lock_status()
            if status > LOCK_THRESHOLD or time.monotonic() >= deadline:
                return status
            time.sleep(interval)

    # -- B-CAS ---------------------------------------------------------------

    def bcas_reset(self) -> None:
        """Reset the card reader; the T=1 sequence number starts from 0 again."""
        self.command(CMD_BCAS_DEACTIVATE, check=False)
        self.command(CMD_BCAS_ACTIVATE, check=False)
        self._bcas_pcb = 0x00

    def bcas_transmit(self, apdu: bytes, retries: int = 2) -> bytes:
        """Send an APDU to the internal B-CAS card and return the response (with SW1 SW2)."""
        last: Exception | None = None
        for _ in range(retries + 1):
            pcb = self._bcas_pcb
            self._bcas_pcb ^= 0x40
            try:
                res = self.command(t1_block(apdu, pcb), check=False)
            except DeviceError as e:
                last = e
                continue
            if res[0] != 0 or res[1] < 6:
                last = DeviceError(f"B-CAS command failed (status {res[0]:#04x}, length {res[1]})")
                continue
            n = res[6]
            if 7 + n > 124:
                last = DeviceError(f"B-CAS response too long ({n} bytes)")
                continue
            return bytes(res[7 : 7 + n])
        raise DeviceError(f"B-CAS transmit failed: {last}")

    # -- TS ------------------------------------------------------------------

    def read_ts(self, buffer: array.array, timeout: int = 1000) -> int:
        """Read TS data into ``buffer``; returns the number of bytes read (0 on timeout)."""
        try:
            return self._dev.read(EP_TS_IN, buffer, timeout=timeout)
        except usb.core.USBTimeoutError:
            return 0


def open_device(selector: str | None = None) -> DyUd200:
    """Claim a DY-UD200 and initialise it.

    ``selector`` is ``None``/``"auto"`` (first free device), an index (``"0"``),
    ``"BUS:ADDR"``, ``/dev/bus/usb/BBB/AAA`` or a port path such as ``"5-1.2"``.
    """
    busy: list[str] = []
    for loc, dev in _select(selector):
        tuner = DyUd200(dev, loc)
        try:
            tuner._claim()
        except DeviceBusy:
            busy.append(str(loc))
            continue
        try:
            tuner.initialize()
        except BaseException:
            tuner.close()
            raise
        return tuner
    raise DeviceBusy(f"all matching DY-UD200 devices are in use: {', '.join(busy)}")
