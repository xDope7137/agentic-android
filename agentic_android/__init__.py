"""Agentic Android — Claude drives an Android device over ADB + uiautomator."""

import logging as _logging

# Quiet python-dotenv's "could not parse" noise (libraries like FastMCP load .env).
_logging.getLogger("dotenv").setLevel(_logging.ERROR)

from .adb import ADB, ADBError
from .device import Device
from .agent import AgenticAndroid

__all__ = ["ADB", "ADBError", "Device", "AgenticAndroid"]
__version__ = "0.1.0"
