#!/usr/bin/env python3

"""
ARTIQ controller to interface with a BME high-voltage power supply and log its
status data to InfluxDB.
"""

from asyncio import sleep
from artiq.tools import atexit_register_coroutine
from llama.influxdb import aggregate_stats_default
from llama.rpc import add_chunker_methods, run_simple_rpc_server
from llama.channels import ChunkedChannel
from .driver import I2CInterface, StateType
from .poller import Poller
import logging
import math

logger = logging.getLogger(__name__)

def setup_args(parser):
    parser.add_argument("--i2c-bus-idx", default=None, type=int, required=True,
                        help="index of the I2C bus to use (cf. /dev/i2c-<n>).")
    parser.add_argument("--i2c-dev-addr", default=None, type=int, required=True,
                        help="7-bit address of the power supply on the I2C bus "
                             "(cf. i2cdetect)")
    parser.add_argument("--voltage-factor", default=None, type=float, required=True,
                        help="highest voltage output by the particular hw model, in volts")


def setup_interface(args, influx_pusher, loop):
    i2c = I2CInterface(args.i2c_bus_idx, args.i2c_dev_addr)

    channels = dict()

    def add(name, ty) -> None:
        def bin_finished(values):
            if influx_pusher:
                influx_pusher.push(name, aggregate_stats_default(values))
        channels[ty] = ChunkedChannel(name, bin_finished, 256, 30, loop)

    # TODO: Better names once deduced what these actually are.
    add("imon_up", StateType.imon_up)
    add("imon_2", StateType.imon_2)
    add("umon_up", StateType.umon_up)
    add("ntc_2", StateType.ntc_2)
    add("ntc_1", StateType.ntc_1)
    add("imon_1", StateType.imon_1)
    add("u_48v", StateType.u_48v)
    add("u_24v", StateType.u_24v)
    add("ratio", StateType.ratio)

    callbacks = {}
    for ty, chunker in channels.items():
        callbacks[ty] = lambda x, chunker=chunker: chunker.push(x)
    callbacks[StateType.status_flags] = lambda x: logger.warn("Status flags changed: %s", x)
    poller = Poller(i2c, callbacks)
    atexit_register_coroutine(poller.stop)

    class Interface:
        def __init__(self):
            # It seems like we can't easily figure out whether the high-voltage
            # stage is currently enabled, as the hardware only sends the
            # status flag field when it changes. Thus, we have to do the wait
            # the first time after startup (signified by None).
            self._set_point_volts = None

        async def get_voltage(self):
            """
            Return the last output voltage programmed.

            This is the value as stored by the controller; there is no hardware
            readback, so None is returned when the voltage has not been
            programmed yet.
            """
            return self._set_point_volts

        async def set_voltage(self, set_point_volts):
            """
            Set the output voltage, in volts.

            Even with the set point at zero, the hardware will output a small
            residual voltage (likely an artifact of imperfect calibration
            between the two bipolar stages used by the power supply). To
            completely extinguish the signal, as well as for safety reasons,
            set point 0.0 will also completely disable the output stage.

            :return: `False` if the setpoint is known to be the same as the
                currently programmed one; `True` otherwise.
            """

            if set_point_volts < 0.0:
                raise ValueError("Output voltage cannot be negative")
            if set_point_volts > args.voltage_factor:
                raise ValueError("Output voltage cannot exceed {} V".format(
                    args.voltage_factor))

            first = self._set_point_volts is None
            if set_point_volts > 0.0 and (first or self._set_point_volts == 0.0):
                await poller.enable_hv(True)

                # The hardware implements some sort of soft-start mechanism,
                # only enabling the output after about one second.
                await sleep(2.0)

            await poller.set_hv_set_point(set_point_volts / args.voltage_factor)

            if set_point_volts == 0.0 and (first or self._set_point_volts > 0.0):
                # Give the hardware some time to ramp down the voltage, as
                # vaguely suggested by the manufacturer's recommended shutdown
                # procedures (which assume that you control the power supply
                # using the chunky 10-turn knob on the front panel).
                await sleep(2.0)
                await poller.enable_hv(False)

            previous_volts = self._set_point_volts
            self._set_point_volts = set_point_volts

            return first or not math.isclose(previous_volts, set_point_volts)

        async def reset_fault(self):
            """
            Reset the hardware fault detection circuitry after a fault has
            occurred (e.g. over-current/-temperature), allowing the output
            to be enabled again.

            Also disables the output (which, in case the hardware is actually
            in a failure state, would have already occurred).
            """
            await self.set_voltage(0.0)
            await poller.reset_fault()

    rpc_interface = Interface()
    for c in channels.values():
        add_chunker_methods(rpc_interface, c)
    return rpc_interface


def main():
    run_simple_rpc_server(4009, setup_args, "hv_psu", setup_interface)


if __name__ == "__main__":
    main()
