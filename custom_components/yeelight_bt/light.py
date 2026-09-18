"""Home Assistant light entities for Yeelight Bluetooth lamps."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_last_service_info,
)
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.color import color_hs_to_RGB, color_RGB_to_hs

from .const import DOMAIN
from .yeelightbt import (
    MODEL_CANDELA,
    TRANSPORT_ERRORS,
    Lamp,
    ReconnectDeferred,
    model_from_name,
)

_LOGGER = logging.getLogger(__name__)
# Each lamp has its own lock; one unreachable lamp must not block another.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = hass.data[DOMAIN][config_entry.entry_id]
    info = async_last_service_info(hass, device.address, connectable=True)
    entity = YeelightBT(
        config_entry.data.get(CONF_NAME) or DOMAIN,
        device,
        device_callback=lambda: async_ble_device_from_address(
            hass, device.address, connectable=True
        ),
        model=model_from_name(info.name if info else device.name),
    )
    async_add_entities([entity])


class YeelightBT(LightEntity):
    """Expose confirmed lamp state and propagate service failures to HA."""

    _attr_should_poll = True
    _attr_supported_features = LightEntityFeature(0)

    def __init__(
        self,
        name: str,
        ble_device: BLEDevice,
        device_callback: Callable[[], BLEDevice | None] | None = None,
        model: str | None = None,
    ) -> None:
        self._attr_name = name
        # Preserve the existing entity unique ID so automations keep working.
        self._attr_unique_id = ble_device.address
        self._dev = Lamp(ble_device, device_callback=device_callback, model=model)
        self._remove_callback = self._dev.add_callback_on_state_changed(self._status_cb)
        self._command_lock = asyncio.Lock()
        self._removed = False
        self._update_failed = False
        self._prop_min_max = self._dev.get_prop_min_max()
        self._attr_min_color_temp_kelvin = self._prop_min_max["temperature"]["min"]
        self._attr_max_color_temp_kelvin = self._prop_min_max["temperature"]["max"]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_shutdown
            )
        )
        self.async_schedule_update_ha_state(force_refresh=True)

    async def _async_shutdown(self, event: Any = None) -> None:
        if not self._removed:
            self._removed = True
            self._remove_callback()
        await self._dev.close()

    async def async_will_remove_from_hass(self) -> None:
        await self._async_shutdown()
        await super().async_will_remove_from_hass()

    @property
    def device_info(self) -> dict[str, Any]:
        info = {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Yeelight",
            "model": self._dev.model,
        }
        if self._dev.versions:
            info["sw_version"] = "-".join(map(str, self._dev.versions[1:4]))
        return info

    @property
    def available(self) -> bool:
        return self._dev.available

    @property
    def brightness(self) -> int:
        return round(255 * self._dev.brightness / 100)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        if self._dev.model == MODEL_CANDELA:
            return None
        return color_RGB_to_hs(*self._dev.color)

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._dev.model == MODEL_CANDELA or self._dev.mode != Lamp.MODE_WHITE:
            return None
        return self.scale_temp_reversed(self._dev.temperature)

    @property
    def is_on(self) -> bool:
        return self._dev.is_on

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        if self._dev.model == MODEL_CANDELA:
            return {ColorMode.BRIGHTNESS}
        return {ColorMode.COLOR_TEMP, ColorMode.HS}

    @property
    def color_mode(self) -> ColorMode:
        if self._dev.model == MODEL_CANDELA:
            return ColorMode.BRIGHTNESS
        if self._dev.mode == Lamp.MODE_WHITE:
            return ColorMode.COLOR_TEMP
        return ColorMode.HS

    @callback
    def _status_cb(self) -> None:
        if self.hass is not None and not self._removed:
            self.async_write_ha_state()

    async def async_update(self) -> None:
        async with self._command_lock:
            if self._removed:
                return
            try:
                await self._dev.get_state()
            except ReconnectDeferred as err:
                _LOGGER.debug("%s: %s", self.name, err)
            except TRANSPORT_ERRORS as err:
                _LOGGER.warning("%s: Could not update lamp: %s", self.name, err)
                self._update_failed = True
            else:
                if self._update_failed:
                    _LOGGER.info("%s: Lamp connection recovered", self.name)
                self._update_failed = False

    async def async_turn_on(self, **kwargs: Any) -> None:
        async with self._command_lock:
            try:
                if kwargs.get(ATTR_BRIGHTNESS) == 0:
                    await self._dev.turn_off()
                    return
                await self._dev.connect()
                if not self.is_on:
                    await self._dev.turn_on()
                brightness = (
                    max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255))
                    if ATTR_BRIGHTNESS in kwargs
                    else (self._dev.brightness or 100)
                )
                if (
                    ATTR_HS_COLOR in kwargs
                    and ColorMode.HS in self.supported_color_modes
                ):
                    await self._dev.set_color(
                        *color_hs_to_RGB(*kwargs[ATTR_HS_COLOR]), brightness=brightness
                    )
                elif (
                    ATTR_COLOR_TEMP_KELVIN in kwargs
                    and ColorMode.COLOR_TEMP in self.supported_color_modes
                ):
                    await self._dev.set_temperature(
                        self.scale_temp(kwargs[ATTR_COLOR_TEMP_KELVIN]),
                        brightness=brightness,
                    )
                elif ATTR_BRIGHTNESS in kwargs:
                    await self._dev.set_brightness(brightness)
            except TRANSPORT_ERRORS as err:
                raise HomeAssistantError(
                    f"Could not control {self.name}: {err}"
                ) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        async with self._command_lock:
            try:
                await self._dev.turn_off()
            except TRANSPORT_ERRORS as err:
                raise HomeAssistantError(
                    f"Could not control {self.name}: {err}"
                ) from err

    def scale_temp(self, temp: int) -> int:
        """Scale the temperature so that the white in HA UI correspond to the
        white on the lamp!"""
        a = self._prop_min_max["temperature"]["min"]
        b = self._prop_min_max["temperature"]["max"]
        mid = 2740  # the temp HA wants to set at when cliking on white in UI
        white = 4080  # the temp that correspond to true white on the lamp

        if temp < mid:
            new_temp = (white - a) / (mid - a) * temp + a * (mid - white) / (mid - a)
        else:
            new_temp = (b - white) / (b - mid) * temp + b * (white - mid) / (b - mid)
        return round(new_temp)

    def scale_temp_reversed(self, temp: int) -> int:
        """Reverse the scale to match HA UI"""
        a = self._prop_min_max["temperature"]["min"]
        b = self._prop_min_max["temperature"]["max"]
        mid = 2740
        white = 4080

        if temp < white:
            new_temp = (mid - a) / (white - a) * temp - a * (mid - white) / (white - a)
        else:
            new_temp = (b - mid) / (b - white) * temp - b * (white - mid) / (b - white)
        return round(new_temp)
