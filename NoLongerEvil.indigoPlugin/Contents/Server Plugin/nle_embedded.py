"""Embedded NoLongerEvil server.

Runs the vendored NoLongerEvil self-hosted server (MIT, Cody Kociemba) inside
the plugin process, on its own thread and asyncio loop. The thermostat talks
to the device port exactly as it would talk to the Docker image; the plugin
then talks to the control port on 127.0.0.1 using the same client as the
Docker mode, so both modes share one code path.

The server reads its configuration from environment variables when the
`nolongerevil` package is first imported, so ports/paths can only be applied
once per plugin process. Changing them needs a plugin restart.
"""

import asyncio
import logging
import os
import threading
import time

_imported_settings = None  # the env we imported with, to detect changes


class _ForwardHandler(logging.Handler):
    """Forward the server's log records into the plugin's Indigo logger."""

    def __init__(self, target_logger):
        super().__init__()
        self.target = target_logger

    def emit(self, record):
        try:
            self.target.log(record.levelno, f"NLE server: {record.getMessage()}")
        except Exception:
            pass


class EmbeddedServer:
    def __init__(self, logger, data_dir, api_origin, device_port, control_port,
                 control_lan=False, server_log_level=logging.WARNING):
        self.logger = logger
        self.data_dir = data_dir
        self.api_origin = api_origin
        self.device_port = int(device_port)
        self.control_port = int(control_port)
        self.control_lan = control_lan
        self.server_log_level = server_log_level

        self._thread = None
        self._loop = None
        self._stop_event = None
        self._started = threading.Event()
        self._error = None
        self.state_service = None  # exposed for raw-bucket diagnostics

    # ---------------------------------------------------------------- config
    def _env(self):
        return {
            "API_ORIGIN": self.api_origin,
            "SERVER_HOST": "0.0.0.0",
            "SERVER_PORT": str(self.device_port),
            "CONTROL_HOST": "0.0.0.0" if self.control_lan else "127.0.0.1",
            "CONTROL_PORT": str(self.control_port),
            "SQLITE3_DB_PATH": os.path.join(self.data_dir, "database.sqlite"),
            "DEBUG_LOGS_DIR": os.path.join(self.data_dir, "debug-logs"),
            "DEVICE_LOGS_DIR": os.path.join(self.data_dir, "device-logs"),
            "DEBUG_LOGGING": "false",
            "REQUIRE_DEVICE_PAIRING": "false",
        }

    def _prepare_import(self):
        """Set env and logging hooks, then import the server package once."""
        global _imported_settings
        env = self._env()
        if _imported_settings is not None:
            if env != _imported_settings:
                self.logger.warning(
                    "Embedded server settings changed - restart the plugin to apply them"
                )
            return
        os.makedirs(self.data_dir, exist_ok=True)
        os.environ.update(env)
        # Never let a stray MQTT_HOST from the environment switch MQTT on
        os.environ.pop("MQTT_HOST", None)

        # Replace the server's logger factory (stdout handlers, no propagation)
        # with plain loggers that propagate to one forwarding handler.
        import nolongerevil.lib.logger as nle_logger

        def get_logger(name):
            if name == "__main__":
                name = "main"
            if not name.startswith("nolongerevil"):
                name = f"nolongerevil.{name}"
            return logging.getLogger(name)

        nle_logger.get_logger = get_logger
        root = logging.getLogger("nolongerevil")
        root.handlers = [_ForwardHandler(self.logger)]
        root.propagate = False
        root.setLevel(self.server_log_level)
        # aiohttp's per-request access log would flood the Indigo log
        access = logging.getLogger("aiohttp.access")
        access.setLevel(logging.WARNING)
        access.propagate = False
        _imported_settings = env

    def set_log_level(self, level):
        self.server_log_level = level
        logging.getLogger("nolongerevil").setLevel(level)

    # ------------------------------------------------------------- lifecycle
    def start(self, timeout=30):
        if self._thread and self._thread.is_alive():
            return True
        self._prepare_import()
        self._started.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run_thread, name="NLE-embedded", daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        if self._error:
            raise RuntimeError(self._error)
        if not self._started.is_set():
            raise RuntimeError("Embedded server did not start within %ss" % timeout)
        return True

    def stop(self, timeout=10):
        if self._loop and self._stop_event:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout)
        self._thread = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and self._started.is_set())

    @property
    def control_url(self):
        return f"http://127.0.0.1:{self.control_port}"

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self.logger.error(f"Embedded NLE server stopped: {self._error}")
            self._started.set()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    async def _serve(self):
        # Imported here so the Docker / hosted modes never need these packages
        from aiohttp import web

        from nolongerevil.config import settings
        from nolongerevil.integrations.integration_manager import IntegrationManager
        from nolongerevil.main import (
            create_control_app,
            create_proxy_app,
            ensure_homeassistant_user,
        )
        from nolongerevil.services.device_availability import DeviceAvailability
        from nolongerevil.services.device_state_service import DeviceStateService
        from nolongerevil.services.sqlmodel_service import SQLModelService
        from nolongerevil.services.subscription_manager import SubscriptionManager
        from nolongerevil.services.weather_service import WeatherService

        self._stop_event = asyncio.Event()
        settings.ensure_data_dir()

        storage = SQLModelService()
        await storage.initialize()
        await ensure_homeassistant_user(storage)

        state_service = DeviceStateService(storage)
        await state_service.initialize()
        self.state_service = state_service

        subscription_manager = SubscriptionManager()
        weather_service = WeatherService(storage)
        await weather_service.initialize()

        device_availability = DeviceAvailability(subscription_manager)
        device_availability.initialize_from_serials(state_service.get_all_serials())

        integration_manager = IntegrationManager(storage, state_service, subscription_manager)
        state_service.set_integration_manager(integration_manager)
        device_availability.set_integration_manager(integration_manager)

        await device_availability.start()
        await integration_manager.start()

        proxy_app = create_proxy_app(
            state_service, subscription_manager, weather_service, device_availability, storage
        )
        control_app = create_control_app(
            state_service, subscription_manager, device_availability, storage
        )
        control_app["integration_manager"] = integration_manager

        keepalive = int(settings.connection_hold_timeout) + 60
        proxy_runner = web.AppRunner(proxy_app, keepalive_timeout=keepalive)
        control_runner = web.AppRunner(control_app)
        await proxy_runner.setup()
        await control_runner.setup()
        try:
            await web.TCPSite(proxy_runner, settings.server_host, settings.server_port).start()
            await web.TCPSite(control_runner, settings.control_host, settings.control_port).start()
        except OSError as exc:
            await proxy_runner.cleanup()
            await control_runner.cleanup()
            await weather_service.close()
            await state_service.close()
            raise RuntimeError(f"could not open port ({exc}) - is something else using it?")

        self.logger.info(
            f"Embedded NLE server listening: thermostat port {settings.server_port}, "
            f"control port {settings.control_port} "
            f"({'LAN' if self.control_lan else 'localhost only'}), "
            f"origin {settings.api_origin}"
        )
        self._started.set()

        await self._stop_event.wait()

        await integration_manager.stop()
        await device_availability.stop()
        # Thermostat long-polls and dashboard/plugin SSE streams never finish
        # on their own; give them a moment, then cut them off.
        for runner in (proxy_runner, control_runner):
            try:
                await asyncio.wait_for(runner.cleanup(), 3)
            except asyncio.TimeoutError:
                pass
        await weather_service.close()
        await state_service.close()
        self.state_service = None
        self.logger.info("Embedded NLE server stopped")

    # ----------------------------------------------------------- diagnostics
    def all_objects(self):
        """Every stored bucket for every serial, same shape as the DB reader."""
        if not self.state_service:
            return None
        out = {}
        for o in list(self.state_service.get_all_objects()):
            out.setdefault(o.serial, {})[o.object_key] = {
                "object_revision": o.object_revision, "value": o.value}
        return out

    def raw_objects(self, serial):
        """Return every stored bucket for a serial (thread-safe read of a dict)."""
        if not self.state_service:
            return None
        objs = self.state_service.get_objects_by_serial(serial)
        return {o.object_key: {"object_revision": o.object_revision, "value": o.value} for o in objs}


def wait_for_port(host, port, timeout=5.0):
    import socket

    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), 0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False
