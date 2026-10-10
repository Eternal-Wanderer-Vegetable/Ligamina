"""Tiny native lifecycle plugin used to freeze the NoneBot T01 baseline."""

from nonebot import get_driver

driver = get_driver()
lifecycle: list[str] = []
driver.on_startup(lambda: lifecycle.append("started"))
driver.on_shutdown(lambda: lifecycle.append("stopped"))
