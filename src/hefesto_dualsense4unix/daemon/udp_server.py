"""UDP server compatível com o protocolo DSX (V2 5.10, ADR-003, V3-1).

Escuta em `127.0.0.1:6969` (configurável), parseia envelope JSON com
`version: 1` + `instructions[]`, aplica cada instrução no controle.

Fora de escopo aqui: persistir estado (fica no StateStore via handlers).
Rate limit (V3-1) roda no dispatch, global + per-IP com sweep periódico.

Schema resumido:

    { "version": 1,
      "instructions": [
        {"type": "TriggerUpdate", "parameters": [side, mode, p1..p7]},
        {"type": "RGBUpdate", "parameters": [idx, r, g, b]},
        {"type": "PlayerLED", "parameters": [idx, bitmask]},
        {"type": "MicLED", "parameters": [state]},
        {"type": "TriggerThreshold", "parameters": [side, value]},
        {"type": "ResetToUserSettings", "parameters": []}
      ]
    }

`version != 1` dropa com `log.warn` (V2 5.10).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from hefesto_dualsense4unix.core.controller import IController
from hefesto_dualsense4unix.core.trigger_effects import build_from_name
from hefesto_dualsense4unix.core.trigger_effects import off as trigger_off
from hefesto_dualsense4unix.daemon.state_store import StateStore
from hefesto_dualsense4unix.utils.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6969
MAX_DATAGRAM_BYTES = 4096

RATE_GLOBAL = 2000
RATE_PER_IP = 1000
SUPPORTED_VERSION = 1


class RateLimiter:
    """Dois limites sobrepostos: global + per-IP (V3-1).

    IPs inativos são evictados via `_sweep` periódico (máx 1x/s).
    """

    def __init__(
        self,
        rate_global: int = RATE_GLOBAL,
        rate_per_ip: int = RATE_PER_IP,
    ) -> None:
        self.rate_global = rate_global
        self.rate_per_ip = rate_per_ip
        self.global_window: deque[float] = deque(maxlen=rate_global)
        self.per_ip: dict[str, deque[float]] = {}
        self._last_sweep: float = 0.0

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < 1.0:
            return
        cutoff = now - 1.0
        self.per_ip = {
            ip: wnd
            for ip, wnd in self.per_ip.items()
            if wnd and wnd[-1] >= cutoff
        }
        self._last_sweep = now

    def allow(self, ip: str, *, now: float | None = None) -> bool:
        t = now if now is not None else time.monotonic()
        cutoff = t - 1.0
        self._sweep(t)

        while self.global_window and self.global_window[0] < cutoff:
            self.global_window.popleft()
        if len(self.global_window) >= self.rate_global:
            return False

        ip_window = self.per_ip.setdefault(ip, deque(maxlen=self.rate_per_ip))
        while ip_window and ip_window[0] < cutoff:
            ip_window.popleft()
        if len(ip_window) >= self.rate_per_ip:
            return False

        self.global_window.append(t)
        ip_window.append(t)
        return True


class DsxProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler: UdpHandler) -> None:
        self.handler = handler
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # Em Python 3.10 o objeto real é `_SelectorDatagramTransport`, que
        # formalmente herda de `asyncio.DatagramTransport` mas falha no
        # `isinstance` contra a classe pública exposta no namespace. O
        # contrato do asyncio já garante o tipo via API de
        # `create_datagram_endpoint`; atribuição direta evita o ruído de
        # AssertionError no journal a cada startup (BUG-UDP-01 / A-02).
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.handler.handle_datagram(data, addr)


@dataclass
class UdpHandler:
    controller: IController
    store: StateStore
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    warn_limit_once_per_sec: float = 1.0
    _last_warn_at: float = 0.0

    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        ip = addr[0]
        now = time.monotonic()
        if not self.rate_limiter.allow(ip, now=now):
            self.store.bump("udp.rate_limited")
            if now - self._last_warn_at >= self.warn_limit_once_per_sec:
                logger.warning("udp_rate_limited", ip=ip)
                self._last_warn_at = now
            return

        if len(data) > MAX_DATAGRAM_BYTES:
            self.store.bump("udp.oversize")
            logger.warning("udp_oversize", size=len(data), ip=ip)
            return

        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.store.bump("udp.parse_error")
            logger.warning("udp_parse_error", err=str(exc), ip=ip)
            return

        if not isinstance(payload, dict):
            self.store.bump("udp.parse_error")
            return

        version = payload.get("version")
        if version != SUPPORTED_VERSION:
            self.store.bump("udp.unsupported_version")
            logger.warning("udp_unsupported_version", version=version, ip=ip)
            return

        instructions = payload.get("instructions", [])
        if not isinstance(instructions, list):
            self.store.bump("udp.invalid_instructions")
            return

        for instr in instructions:
            self._dispatch_instruction(instr, ip=ip)

    def _dispatch_instruction(self, instr: dict[str, Any], *, ip: str) -> None:
        if not isinstance(instr, dict):
            self.store.bump("udp.invalid_instruction")
            return
        kind = instr.get("type")
        params = instr.get("parameters", [])
        if not isinstance(kind, str) or not isinstance(params, list):
            self.store.bump("udp.invalid_instruction")
            return

        try:
            if kind == "TriggerUpdate":
                self._do_trigger_update(params)
                logger.info("udp_trigger_applied", side=str(params[0]) if params else "?", mode=str(params[1]) if len(params) > 1 else "?", rest=str(params[2:]) if len(params) > 2 else "")
            elif kind == "RGBUpdate":
                self._do_rgb_update(params)
            elif kind == "PlayerLED":
                self._do_player_led(params)
            elif kind == "MicLED":
                self._do_mic_led(params)
            elif kind == "TriggerThreshold":
                self._do_trigger_threshold(params)
            elif kind == "ResetToUserSettings":
                self._do_reset()
            else:
                self.store.bump("udp.unknown_instruction")
                logger.warning("udp_unknown_instruction", kind=kind, ip=ip)
                return
            self.store.bump(f"udp.applied.{kind}")
        except Exception as exc:
            self.store.bump(f"udp.error.{kind}")
            logger.warning("udp_instruction_error", kind=kind, err=str(exc), ip=ip)

    def _do_trigger_update(self, params: list[Any]) -> None:
        if len(params) < 2:
            raise ValueError("TriggerUpdate precisa [side, mode, ...]")
        side_raw, mode_raw, *rest = params
        side = str(side_raw).lower()
        if side not in ("left", "right"):
            raise ValueError(f"TriggerUpdate side invalido: {side_raw}")
        if not isinstance(mode_raw, str):
            raise ValueError("TriggerUpdate mode precisa ser string")
        effect = build_from_name(mode_raw, rest)
        self.controller.set_trigger(side, effect)  # type: ignore[arg-type]

    def _do_rgb_update(self, params: list[Any]) -> None:
        if len(params) < 4:
            raise ValueError("RGBUpdate precisa [idx, r, g, b]")
        _idx, r, g, b = params[:4]
        # Clamp silencioso em [0, 255] para compatibilidade com clients DSX
        # imprecisos. Alinha comportamento ao handler IPC `led.set` que valida
        # range (achado 19 da auditoria forense V23).
        r_c = max(0, min(255, int(r)))
        g_c = max(0, min(255, int(g)))
        b_c = max(0, min(255, int(b)))
        self.controller.set_led((r_c, g_c, b_c))

    def _do_player_led(self, params: list[Any]) -> None:
        # Decodifica bitmask em tuple[bool, bool, bool, bool, bool] e propaga
        # ao controller. Bit i do inteiro mapeia para bits[i] (bit 0 = LED 0).
        if len(params) < 2:
            raise ValueError("PlayerLED precisa [idx, bitmask]")
        _idx, bitmask = params[:2]
        mask = int(bitmask)
        bits: tuple[bool, bool, bool, bool, bool] = (
            bool(mask & 0b00001),
            bool(mask & 0b00010),
            bool(mask & 0b00100),
            bool(mask & 0b01000),
            bool(mask & 0b10000),
        )
        self.controller.set_player_leds(bits)
        self.store.bump(f"udp.player_led.{mask}")

    def _do_mic_led(self, params: list[Any]) -> None:
        if not params:
            raise ValueError("MicLED precisa [state]")
        state = bool(params[0])
        self.controller.set_mic_led(state)
        self.store.bump(f"udp.mic_led.{int(state)}")

    def _do_trigger_threshold(self, params: list[Any]) -> None:
        if len(params) < 2:
            raise ValueError("TriggerThreshold precisa [side, value]")
        side_raw, value = params[0], int(params[1])
        side = str(side_raw).lower()
        if side not in ("left", "right"):
            raise ValueError(f"TriggerThreshold side invalido: {side_raw}")
        self.store.bump(f"udp.trigger_threshold.{side}.{value}")

    def _do_reset(self) -> None:
        self.controller.set_trigger("left", trigger_off())
        self.controller.set_trigger("right", trigger_off())


@dataclass
class UdpServer:
    controller: IController
    store: StateStore
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    rate_limiter: RateLimiter | None = None
    _transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        rate = self.rate_limiter or RateLimiter()
        handler = UdpHandler(
            controller=self.controller, store=self.store, rate_limiter=rate
        )
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: DsxProtocol(handler),
            local_addr=(self.host, self.port),
        )
        logger.info("udp_server_listening", host=self.host, port=self.port)

    async def stop(self) -> None:
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.close()
            self._transport = None


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MAX_DATAGRAM_BYTES",
    "RATE_GLOBAL",
    "RATE_PER_IP",
    "SUPPORTED_VERSION",
    "RateLimiter",
    "UdpHandler",
    "UdpServer",
]
