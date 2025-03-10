import asyncio
import logging
import time

from aiohttp import ClientSession

from .base import SIGNAL_CONNECTED, SIGNAL_UPDATE, XDevice, XRegistryBase
from .cloud import XRegistryCloud
from .local import XRegistryLocal

_LOGGER = logging.getLogger(__name__)

SIGNAL_ADD_ENTITIES = "add_entities"
LOCAL_TTL = 60


class XRegistry(XRegistryBase):
    config: dict = None
    task: asyncio.Task = None

    def __init__(self, session: ClientSession):
        super().__init__(session)

        self.devices: dict[str, XDevice] = {}

        self.cloud = XRegistryCloud(session)
        self.cloud.dispatcher_connect(SIGNAL_CONNECTED, self.cloud_connected)
        self.cloud.dispatcher_connect(SIGNAL_UPDATE, self.cloud_update)

        self.local = XRegistryLocal(session)
        self.local.dispatcher_connect(SIGNAL_CONNECTED, self.local_connected)
        self.local.dispatcher_connect(SIGNAL_UPDATE, self.local_update)

    def setup_devices(self, devices: list[XDevice]) -> list:
        from ..devices import get_spec

        entities = []

        for device in devices:
            did = device["deviceid"]
            try:
                device.update(self.config["devices"][did])
            except Exception:
                pass

            try:
                uiid = device["extra"]["uiid"]
                _LOGGER.debug(f"{did} UIID {uiid:04} | %s", device["params"])

                # at this moment entities can catch signals with device_id and
                # update their states, but they can be added to hass later
                entities += [cls(self, device) for cls in get_spec(device)]

                self.devices[did] = device

            except Exception as e:
                _LOGGER.warning(f"{did} !! can't setup device", exc_info=e)

        return entities

    @property
    def online(self) -> bool:
        return self.cloud.online is not None or self.local.online

    async def stop(self, *args):
        self.devices.clear()
        self.dispatcher.clear()

        await self.cloud.stop()
        await self.local.stop()

        if self.task:
            self.task.cancel()

    async def send(
        self,
        device: XDevice,
        params: dict = None,
        params_lan: dict = None,
        query_cloud: bool = True,
    ):
        """Send command to device with LAN and Cloud. Usual params are same.

        LAN will send new device state after update command, Cloud - don't.

        :param device: device object
        :param params: non empty to update state, empty to query state
        :param params_lan: optional if LAN params different (ex iFan03)
        :param query_cloud: optional query Cloud state after update state,
          ignored if params empty
        """
        seq = self.sequence()

        can_local = self.local.online and device.get("host")
        can_cloud = self.cloud.online and device.get("online")

        if can_local and can_cloud:
            # try to send a command locally (wait no more than a second)
            ok = await self.local.send(device, params_lan or params, seq, 1)

            # otherwise send a command through the cloud
            if ok != "online":
                ok = await self.cloud.send(device, params, seq)
                if ok != "online":
                    asyncio.create_task(self.check_offline(device))
                elif query_cloud and params:
                    # force update device actual status
                    await self.cloud.send(device, timeout=0)

        elif can_local:
            ok = await self.local.send(device, params_lan or params, seq, 5)
            if ok != "online":
                asyncio.create_task(self.check_offline(device))

        elif can_cloud:
            ok = await self.cloud.send(device, params, seq)
            if ok == "online" and query_cloud and params:
                await self.cloud.send(device, timeout=0)

        else:
            return

        # TODO: response state
        # self.dispatcher_send(device["deviceid"], state)

    async def send_bulk(self, device: XDevice, params: dict):
        assert "switches" in params

        if "params_bulk" in device:
            for new in params["switches"]:
                for old in device["params_bulk"]["switches"]:
                    # check on duplicates
                    if new["outlet"] == old["outlet"]:
                        old["switch"] = new["switch"]
                        break
                else:
                    device["params_bulk"]["switches"].append(new)
            return

        device["params_bulk"] = params
        await asyncio.sleep(0.1)

        return await self.send(device, device.pop("params_bulk"))

    async def check_offline(self, device: XDevice):
        if not device.get("host"):
            return

        ok = await self.local.send(device, {"cmd": "info"}, timeout=10)
        if ok == "online":
            device["local_ts"] = time.time() + LOCAL_TTL
            return

        device.pop("host", None)

        did = device["deviceid"]
        _LOGGER.debug(f"{did} !! Local4 | Device offline")
        self.dispatcher_send(did)

    def cloud_connected(self):
        for deviceid in self.devices.keys():
            self.dispatcher_send(deviceid)

        if not self.task:
            self.task = asyncio.create_task(self.run_forever())

    def local_connected(self):
        if not self.task:
            self.task = asyncio.create_task(self.run_forever())

    def cloud_update(self, msg: dict):
        did = msg["deviceid"]
        device = self.devices.get(did)
        # the device may be from another Home - skip it
        if not device or "online" not in device:
            return

        params = msg["params"]

        _LOGGER.debug(f"{did} <= Cloud3 | %s | {msg.get('sequence')}", params)

        # process online change
        if "online" in params:
            device["online"] = params["online"]
            # check if LAN online after cloud offline
            if not device["online"] and device.get("host"):
                asyncio.create_task(self.check_offline(device))

        elif device["online"] is False:
            device["online"] = True

        if "sledOnline" in params:
            device["params"]["sledOnline"] = params["sledOnline"]

        self.dispatcher_send(did, params)

    def local_update(self, msg: dict):
        did: str = msg["deviceid"]
        device: XDevice = self.devices.get(did)
        params: dict = msg.get("params")
        # check device in known devices list
        if not device:
            # check payload already decrypted (DIY devices)
            if not params:
                try:
                    # try to decrypt payload if we have right key in config
                    msg["params"] = params = self.local.decrypt_msg(
                        msg, self.config["devices"][did]["devicekey"]
                    )
                except Exception:
                    _LOGGER.debug(f"{did} !! skip setup for encrypted device")
                    # save device to known list, so no more decrypt tries
                    self.devices[did] = msg
                    return

            from ..devices import setup_diy

            # setup new device as DIY device
            device = setup_diy(msg)
            entities = self.setup_devices([device])
            self.dispatcher_send(SIGNAL_ADD_ENTITIES, entities)

        elif not params:
            if "devicekey" not in device:
                # this is known device with encrypted payload but without devicekey
                return
            try:
                # decrypt payload for known device with devicekey
                params = self.local.decrypt_msg(msg, device["devicekey"])
            except Exception as e:
                _LOGGER.debug("Can't decrypt message", exc_info=e)
                return

        elif "devicekey" in device:
            # unencripted device with devicekey in config, this means that the
            # DIY device is still connected to the ewelink account
            device.pop("devicekey")

        tag = "Local3" if "host" in msg else "Local0"

        _LOGGER.debug(f"{did} <= {tag} | %s | {msg.get('seq', '')}", params)

        # msg from zeroconf ServiceStateChange.Removed
        if params.get("online") is False:
            asyncio.create_task(self.check_offline(device))
            return

        if "sledOnline" in params:
            device["params"]["sledOnline"] = params["sledOnline"]

        # we can get data from device, but without host
        if "host" in msg and device.get("host") != msg["host"]:
            # params for custom sensor
            device["host"] = params["host"] = msg["host"]
            device["localtype"] = msg["localtype"]

        device["local_ts"] = time.time() + LOCAL_TTL

        self.dispatcher_send(did, params)

    async def run_forever(self):
        from ..devices import POW_UI_ACTIVE

        # collect pow devices
        pow_devices = [
            device
            for device in self.devices.values()
            if "extra" in device and device["extra"]["uiid"] in POW_UI_ACTIVE
        ]

        while True:
            ts = time.time()

            if self.cloud.online:
                for device in pow_devices:
                    if not device.get("online") or device.get("pow_ts", 0) > ts:
                        continue

                    dt, params = POW_UI_ACTIVE[device["extra"]["uiid"]]
                    device["pow_ts"] = ts + dt
                    asyncio.create_task(self.cloud.send(device, params, timeout=0))

            if self.local.online:
                for device in self.devices.values():
                    if "local_ts" not in device or device["local_ts"] > ts:
                        continue
                    device.pop("local_ts")
                    asyncio.create_task(self.check_offline(device))

            await asyncio.sleep(15)
