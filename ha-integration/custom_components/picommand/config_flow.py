"""Config flow for PiCommand."""
from __future__ import annotations
import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from . import DOMAIN

STEP_USER_DATA_SCHEMA = vol.Schema({
    vol.Required("url", default="http://100.127.5.18"): str,
    vol.Required("username", default="admin"): str,
    vol.Required("password"): str,
})

async def _get_token(url, username, password):
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(
            f"{url.rstrip('/')}/api/auth/login",
            data={"username": username, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["access_token"]

class PiCommandConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                token = await _get_token(user_input["url"], user_input["username"], user_input["password"])
                return self.async_create_entry(
                    title=f"PiCommand ({user_input['url']})",
                    data={"url": user_input["url"], "token": token, "username": user_input["username"]},
                )
            except aiohttp.ClientResponseError as e:
                errors["base"] = "invalid_auth" if e.status == 401 else "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)
