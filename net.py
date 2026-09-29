"""Автоопределение способа выхода в сеть.

Порядок: заданный в .env прокси → прямое соединение → системный прокси Windows →
локальные прокси популярных VPN-клиентов (Happ, v2rayN, Nekoray, Hiddify, Clash).
Ничего настраивать вручную не нужно: если VPN работает в режиме туннеля, подойдёт
прямое соединение; если он поднимает локальный SOCKS — бот сам его найдёт.
"""
from __future__ import annotations

import logging
import socket
import sys
from typing import Optional

import httpx

log = logging.getLogger("net")

TELEGRAM_PROBE = "https://api.telegram.org"

# Порты локальных прокси, которые поднимают распространённые клиенты
# (Happ, v2rayN/Xray, Nekoray, Hiddify, Clash, Tor и т.п.)
def _candidate_ports() -> list[int]:
    ports: list[int] = []
    for group in (
        range(2080, 2091),     # Happ, Hiddify
        range(10808, 10815),   # v2rayN / Xray
        range(1080, 1086),     # классический SOCKS
        range(7890, 7894),     # Clash
        range(20170, 20174),   # Nekoray
        (8118, 8080, 3128, 9050, 9150, 8889, 4321),
    ):
        ports.extend(group)
    seen, out = set(), []
    for p in ports:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


CANDIDATE_PORTS = tuple(_candidate_ports())


def _tcp_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def windows_system_proxy() -> list[str]:
    """Прокси из настроек Windows (Параметры → Сеть и Интернет → Прокси-сервер)."""
    if not sys.platform.startswith("win"):
        return []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
    except OSError:
        return []

    out: list[str] = []
    for part in server.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            scheme, _, addr = part.partition("=")
            scheme = scheme.strip().lower()
            if scheme.startswith("socks"):
                out.append(f"socks5://{addr.strip()}")
            elif scheme in ("http", "https"):
                out.append(f"http://{addr.strip()}")
        else:
            out.append(f"socks5://{part}")
            out.append(f"http://{part}")
    return out


async def _probe(url: str, proxy: Optional[str], timeout: float = 8.0) -> bool:
    """Проверяем реальную доступность url (через прокси или напрямую)."""
    try:
        async with httpx.AsyncClient(
            proxy=proxy, timeout=timeout, trust_env=False, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            return resp.status_code < 500
    except Exception:
        return False


async def detect(url: str, prefer: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Вернёт (доступен ли url, каким прокси пользоваться).

    proxy == None означает «работает напрямую».
    """
    if prefer:
        if await _probe(url, prefer):
            log.info("%s: работает через указанный прокси %s", url, prefer)
            return True, prefer
        log.warning("Прокси %s не отвечает — ищу другой путь", prefer)

    if await _probe(url, None):
        log.info("%s: доступен напрямую", url)
        return True, None

    candidates = list(windows_system_proxy())
    for port in CANDIDATE_PORTS:
        if _tcp_open("127.0.0.1", port):
            candidates.append(f"socks5://127.0.0.1:{port}")
            candidates.append(f"http://127.0.0.1:{port}")

    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if await _probe(url, cand):
            log.info("%s: работает через найденный прокси %s", url, cand)
            return True, cand

    log.error("%s: недоступен ни напрямую, ни через найденные прокси", url)
    return False, None
