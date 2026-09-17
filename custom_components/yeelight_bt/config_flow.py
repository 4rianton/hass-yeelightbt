"""Config flow for yeelight_bt"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr

from .const import CONF_ENTRY_MANUAL, CONF_ENTRY_METHOD, CONF_ENTRY_SCAN, DOMAIN
from .yeelightbt import MODEL_UNKNOWN, model_from_name

_LOGGER = logging.getLogger(__name__)


class Yeelight_btConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore
    """Handle a config flow for yeelight_bt."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @property
    def data_schema(self) -> vol.Schema:
        """Return the data schema for integration."""
        return vol.Schema({vol.Required(CONF_NAME): str, vol.Required(CONF_MAC): str})

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        _LOGGER.debug("Discovered bluetooth device: %s", discovery_info)
        await self.async_set_unique_id(dr.format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()

        self.devices = [
            f"{discovery_info.address} ({model_from_name(discovery_info.name)})"
        ]
        return await self.async_step_device()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""

        if user_input is None:
            schema = {
                vol.Required(CONF_ENTRY_METHOD): vol.In(
                    [CONF_ENTRY_SCAN, CONF_ENTRY_MANUAL]
                )
            }
            return self.async_show_form(step_id="user", data_schema=vol.Schema(schema))
        method = user_input[CONF_ENTRY_METHOD]
        _LOGGER.debug(f"Method selected: {method}")
        if method == CONF_ENTRY_SCAN:
            return await self.async_step_scan()
        else:
            self.devices = []
            return await self.async_step_device()

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the discovery by scanning."""
        if user_input is None:
            return self.async_show_form(step_id="scan")
        # Use HA's shared discovery history, including connectable proxies.
        # Creating a local scanner here bypasses proxies and competes for BLE.
        configured = self._async_current_ids()
        self.devices = [
            f"{info.address} ({model_from_name(info.name)})"
            for info in async_discovered_service_info(self.hass, connectable=True)
            if model_from_name(info.name) != MODEL_UNKNOWN
            and dr.format_mac(info.address) not in configured
        ]
        if not self.devices:
            return self.async_abort(reason="no_devices_found")

        return await self.async_step_device()

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle setting up a device."""
        # _LOGGER.debug(f"User_input: {user_input}")
        if not user_input:
            schema_mac = str
            if self.devices:
                schema_mac = vol.In(self.devices)
            schema = vol.Schema(
                {vol.Required(CONF_NAME): str, vol.Required(CONF_MAC): schema_mac}
            )
            return self.async_show_form(step_id="device", data_schema=schema)

        user_input[CONF_MAC] = user_input[CONF_MAC][:17]
        unique_id = dr.format_mac(user_input[CONF_MAC])
        _LOGGER.debug(f"Yeelight UniqueID: {unique_id}")

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
