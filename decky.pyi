"""
Type stubs for the decky module provided by Decky Loader at runtime.
"""
import logging

HOME: str
USER: str
DECKY_VERSION: str
DECKY_USER: str
DECKY_USER_HOME: str
DECKY_HOME: str
DECKY_PLUGIN_SETTINGS_DIR: str
DECKY_PLUGIN_RUNTIME_DIR: str
DECKY_PLUGIN_LOG_DIR: str
DECKY_PLUGIN_DIR: str
DECKY_PLUGIN_NAME: str
DECKY_PLUGIN_VERSION: str
DECKY_PLUGIN_AUTHOR: str
DECKY_PLUGIN_LOG: str
logger: logging.Logger

async def emit(event: str, *args) -> None: ...
def migrate_settings(*files_or_directories: str) -> None: ...
def migrate_runtime(*files_or_directories: str) -> None: ...
def migrate_logs(*files_or_directories: str) -> None: ...
def migrate_any(target_dir: str, *files_or_directories: str) -> None: ...
