#!/usr/bin/env python3

"""
ARTIQ controller to interface with a BME high-voltage power supply and log its
status data to InfluxDB.
"""

from artiq.tools import atexit_register_coroutine
from llama.influxdb import aggregate_stats_default
from llama.rpc import add_chunker_methods, run_simple_rpc_server
from llama.sample_chunker import SampleChunker
from .driver import I2CInterface, StateType
from .poller import Poller


def setup_args(parser):
    parser.add_argument("--i2c-bus-idx", default=None, type=int, required=True,
                        help="index of the I2C bus to use (cf. /dev/i2c-<n>).")
    parser.add_argument("--i2c-dev-addr", default=None, type=int, required=True,
                        help="7-bit address of the power supply on the I2C bus "
                             "(cf. i2cdetect)")


def setup_interface(args, influx_pusher, loop):
    i2c = I2CInterface(args.i2c_bus_idx, args.i2c_dev_addr)

    channels = dict()

    def add(name, ty) -> None:
        def bin_finished(values):
            if influx_pusher:
                influx_pusher.push(name, aggregate_stats_default(values))
        channels[ty] = SampleChunker(name, bin_finished, 256, 30, loop)

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
    poller = Poller(i2c, callbacks)
    atexit_register_coroutine(poller.stop)

    # Cannot use object() because it has no __dict__.
    class Interface:
        pass
    rpc_interface = Interface()
    setattr(rpc_interface, "enable_hv", poller.enable_hv)
    setattr(rpc_interface, "reset_fault", poller.reset_fault)
    setattr(rpc_interface, "set_hv_set_point", poller.set_hv_set_point)
    for c in channels.values():
        add_chunker_methods(rpc_interface, c)
    return rpc_interface


def main():
    run_simple_rpc_server(4009, setup_args, "hv_psu", setup_interface)


if __name__ == "__main__":
    main()
