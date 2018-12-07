"""
Adapts the synchronous low-level interface to an asynchronous environment by
polling for status updates in the background while forwarding any control
commands.
"""

import logging
from . import driver
from asyncio import AbstractEventLoop, CancelledError, get_event_loop, Lock, sleep
from contextlib import suppress
from typing import Callable, Mapping

logger = logging.getLogger(__name__)


class Poller:
    def __init__(self,
                 interface: driver.I2CInterface,
                 callbacks_for_states: Mapping[driver.StateType, Callable[[float], None]],
                 polling_interval: float=0.01,
                 loop: AbstractEventLoop=None):
        """
        Initialise a new Poller instance.

        :param interface: The I2CInterface to use. Note that Poller assumes
            exclusive ownership of the object and might operate on it from a
            background thread.
        :param callbacks_for_states: A map from hardware status update
            driver.StateTypes to callbacks to invoke with the new value when
            they occur.
        :param polling_interval: The target interval between polling the
            hardware for state updates, in seconds. The actual interval might
            be longer if the event loop is busy.
        :param loop: The event loop to use (None for asyncio default).
        """
        self._interface = interface
        self._callbacks_for_states = callbacks_for_states
        self.polling_interval = polling_interval
        self._loop = loop if loop else get_event_loop()

        # Hardware communication lock.
        self._interface_lock = Lock()

        self._shutdown = False

        # Start polling in a background coroutine.
        self._poll_task = self._loop.create_task(self._run_poll_loop())

    async def stop(self):
        """
        Stop the background polling task and wait for it to exit.
        """
        self._shutdown = True
        with suppress(CancelledError):
            await self._poll_task

    async def enable_hv(self, enabled: bool) -> None:
        """
        Enable or disable the hardware high-voltage output stage.
        """
        await self._run_on_hardware(lambda i: i.write_control_flags(
            {driver.ControlFlag.hv_on} if enabled else set()))

    async def set_hv_set_point(self, value: float) -> None:
        """
        Set the target output voltage.

        Even at zero voltage, the hardware might produce a small residual
        potential at its outputs; use `enable_hv(False)` to completely disable
        it.

        :param value: The set point, from zero to one. The corresponding
            physical range depends on the hardware configuration/calibration.
        """
        if value < 0 or value > 1:
            raise ValueError("High-voltage set point must be between 0 and 1.")

        await self._run_on_hardware(lambda i: i.write_hv_set_point(int(
            round(value * (2**12 - 1)))))

    async def reset_fault(self) -> None:
        """
        Disable the high-voltage output and clears the hardware fault status.
        """
        def write(i: driver.I2CInterface):
            i.write_control_flags({driver.ControlFlag.reset})
            i.write_control_flags(set())
        await self._run_on_hardware(write)

    async def _run_poll_loop(self) -> None:
        """
        Run the idle poll loop.
        """
        while not self._shutdown:
            last_poll_time = self._loop.time()

            ty, val = await self._run_on_hardware(lambda i: i.read_state_update())
            if ty in self._callbacks_for_states:
                if ty != driver.StateType.status_flags:
                    val /= 2**12 - 1
                self._callbacks_for_states[ty](val)

            elapsed = self._loop.time() - last_poll_time
            await sleep(max(0.0, self.polling_interval - elapsed))

    async def _run_on_hardware(self, fun):
        """
        Execute the passed function on a background thread, passing the hardware
        driver I2CInterface as a parameter and serialising access to it.
        """
        with (await self._interface_lock):
            return await self._loop.run_in_executor(None, fun, self._interface)
