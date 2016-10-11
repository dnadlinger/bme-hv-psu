"""
Low-level interface to communicate with a BME high-voltage power supply over
its I2C interface.

This is necessarily a blocking interface due to the low-level system calls for
interfacing with the I2C bus.
"""

import posix
import struct

from enum import Enum, unique
from fcntl import ioctl
from typing import AbstractSet, Optional, Tuple, Union


# These magic numbers must match the Linux kernel headers and do not seem to be
# available from any standard Python module.
I2C_SLAVE = 0x0703


class CommunicationError(Exception):
    """An error in the low-level I2C communication with the power supply."""
    pass


@unique
class StateType(Enum):
    """
    The different state update channels provided by the hardware.

    The first several channels are internal analog read-back values (no
    further documentation available but their names), the last a set of
    possible status conditions (see StatusFlag).
    """

    imon_up = 0x0
    imon_2 = 0x1
    umon_up = 0x2
    ntc_2 = 0x3
    ntc_1 = 0x4
    imon_1 = 0x5
    u_48v = 0x6
    u_24v = 0x7
    ratio = 0x8

    status_flags = 0xc


@unique
class StatusFlag(Enum):
    """Status flag bits returned by the hardware."""

    overcurrent_0 = 0x1
    undervoltage_48v = 0x2
    hv_module_failure_2 = 0x4
    undervoltage_24v = 0x8
    overcurrent_4 = 0x10
    overcurrent_5 = 0x20
    hv_module_failure_6 = 0x40
    hv_module_failure_7 = 0x80
    overtemperature_driver_head = 0x100
    overtemperature_unused = 0x200
    ntc_open = 0x400
    fan_failure_11 = 0x800
    fan_failure_12 = 0x1000
    fan_failure_13 = 0x2000
    hv_discharge = 0x4000
    interlock_15 = 0x8000
    interlock_16 = 0x10000
    current_ratio_error = 0x20000
    current_ratio_checking_active = 0x100000

    #: Error checking is inhibited by connecting internal jumper (pins 3 and 4
    #: on quadratic 4-pin header in the left/centre of the main PCB). Allows
    #: enabling high voltage without driver head connected.
    inhibit_error = 0x200000

    #: This seems to be set all the time in I2C mode.
    pc_board_remote = 0x400000

    failure = 0x4000000
    hv_on = 0x8000000


def describe_status_flag(flag: StatusFlag) -> str:
    """
    Return a human-readable message corresponding to the given status flag.
    """
    if flag in [StatusFlag.overcurrent_0, StatusFlag.overcurrent_4,
                StatusFlag.overcurrent_5]:
        return "Current too high"
    if flag == StatusFlag.undervoltage_48v:
        return "48 V supply rail too low"
    if flag in [StatusFlag.hv_module_failure_2, StatusFlag.hv_module_failure_6,
                StatusFlag.hv_module_failure_7]:
        return "High-voltage module failure"
    if flag == StatusFlag.undervoltage_24v:
        return "24 V supply rail too low"
    if flag == StatusFlag.overtemperature_driver_head:
        return "Temperature in driver head too high"
    if flag == StatusFlag.overtemperature_unused:
        # The documentation for the I2C interface defines this, but
        # describes it as unused.
        return "Temperature too high (unused)"
    if flag == StatusFlag.ntc_open:
        return "NTC open"
    if flag in [StatusFlag.fan_failure_11, StatusFlag.fan_failure_12,
                StatusFlag.fan_failure_13]:
        return "Fan failure detected"
    if flag == StatusFlag.hv_discharge:
        return "High-voltage discharge detected"
    if flag in [StatusFlag.interlock_15, StatusFlag.interlock_16]:
        return "Interlock tripped"
    if flag == StatusFlag.current_ratio_error:
        # Unclear what this actually means.
        return "Current ratio error"
    if flag == StatusFlag.current_ratio_checking_active:
        return "Current ratio checking active"
    if flag == StatusFlag.inhibit_error:
        return "Error checking inhibited"
    if flag == StatusFlag.pc_board_remote:
        return "PCB remote active"
    if flag == StatusFlag.failure:
        # Unclear what this actually means - equivalent to the generic
        # failure indicator on the front panel?
        return "Failure"
    if flag == StatusFlag.hv_on:
        return "High-voltage is enabled"
    raise ValueError("Unknown status flag: {}".format(flag))


@unique
class ControlFlag(Enum):
    """
    Control flag bits to set on the hardware.

    The vendor-provided documentation only includes the names without further
    descriptions.
    """

    #: Enables the high-voltage block. The register value is ANDed with the
    #: physical front panel switch.
    hv_on = 0x1

    #: Resets a fault condition.
    reset = 0x2

    check_ratio = 0x4
    calibrate = 0x8
    remote = 0x16


class I2CInterface:
    """
    Wraps the system calls needed to communicate with the power supply on a
    local I2C bus.

    Currently Linux-only (with the likely candidate being a small ARM SoC board
    like the BeagleBone, Raspberry Pi, etc.).
    """

    # Implementation note: This uses raw Linux syscalls instead of an I2C
    # library because all the popular ones ("smbus", etc.) do not seem to
    # implement the raw four-byte reads required for the PSU interface.

    def __init__(self, bus_idx: int, dev_addr: int):
        """
        Establish a connection to the device on a particular I2C bus and the
        given address.

        :param bus_idx: The index of the I2C bus to use (cf. /dev/i2c-<n>).
        :param dev_addr: The 7-bit address of the power supply on the bus.
            This is the same address as reported by `i2cdetect`, and will be
            extended with the read/write bit internally.
        """
        file = "/dev/i2c-{}".format(bus_idx)
        self._fd = posix.open(file, posix.O_RDWR)

        if self._fd < 0:
            raise CommunicationError(
                "Failed to open '{}', invalid file descriptor: {}".
                format(file, self._fd))

        if ioctl(self._fd, I2C_SLAVE, dev_addr) < 0:
            raise CommunicationError(
                "Failed to set slave address on '{}' to {}".
                format(file, dev_addr))

        # Make sure the communication works.
        self.read_state_update()

    def read_state_update(self) -> \
            Optional[Tuple[ControlFlag, Union[int, AbstractSet[StatusFlag]]]]:
        """
        Read a state update from the hardware.

        :returns: A tuple (StateType, <value>) describing the update received,
            or None if no data was available.
        """

        # We always read four bytes, which the hardware seems to be happy to
        # provide, even if the analogue read-back values only occupy 2 bytes.
        response = posix.read(self._fd, 4)
        if len(response) != 4:
            raise CommunicationError("Only {} bytes read from I2C bus".format(
                len(response)))

        # The first four bits contain the "channel" index.
        msg_type = response[0] >> 4
        if msg_type == 0xf:
            # No more data, according to manual.
            return None

        try:
            state_type = StateType(msg_type)
        except ValueError:
            raise CommunicationError("Unexpected state update type: 0x{:x}".
                                     format(msg_type))

        if state_type == StateType.status_flags:
            flags = set()

            response_uint = struct.unpack(">L", response)[0]
            for flag in StatusFlag:
                if response_uint & flag.value:
                    flags.add(flag)

            return StateType.status_flags, flags
        else:
            # Value is stored in the upper 16 bits, with the first 4 being
            # msg_type.
            return state_type, (struct.unpack(">H", response[0:2])[0] & 0x0FFF)

    def write_control_flags(self, flags: AbstractSet[ControlFlag]) -> None:
        """
        Write the given control flags to the hardware.

        :param flags: The ControlFlags to set active. Note that the whole flag
            register is always written at once; all flags not specified here
            will be unset.
        """
        # Bits 1100 represent the control flag register.
        cmd = 0xc000
        for f in flags:
            cmd |= f.value
        self._write_command(cmd)

    def write_hv_set_point(self, set_point: int) -> None:
        """
        Write a new high-voltage set point to the hardware.

        :param set_point: The set point, in hardware units (0 to 2^12 - 1). The
            corresponding physical range depends on the hardware configuration/
            calibration.
        """
        if set_point < 0:
            raise ValueError("Set point must be positive integer.")
        if set_point >= (1 << 12):
            raise ValueError("Set point must be in 12 bit integer range.")

        # The set point value has "address" bits 0000.
        self._write_command(set_point)

    def _write_command(self, cmd: int) -> None:
        ret = posix.write(self._fd, struct.pack(">H", cmd))
        if ret != 2:
            raise CommunicationError("Failed to write two-byte I2C command, "
                                     "write returned {}".format(ret))
