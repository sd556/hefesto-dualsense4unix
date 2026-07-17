"""Backend real usando `pydualsense` para falar HID com o DualSense.

Thin adapter: traduz chamadas da `IController` para a API do pydualsense e
converte estado interno em `ControllerState` imutável. Mantém intencionalmente
sem lógica de negócio — facilita troca do backend no futuro (ADR-001).
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from pydualsense import pydualsense as _pydualsense
from pydualsense.enums import ConnectionType

from hefesto_dualsense4unix.core.controller import (
    ControllerState,
    IController,
    Side,
    Transport,
    TriggerEffect,
)
from hefesto_dualsense4unix.core.evdev_reader import EvdevReader

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class pydualsense(_pydualsense):  # noqa: N801 - mantém API/patch points existentes
    """pydualsense com pacing apenas no transporte Bluetooth.

    O loop upstream escreve um output report para cada input report sem intervalo.
    Em Linux/Bluetooth isso pode encher o socket HIDP (EAGAIN) e derrubar os
    devices de input. Dez milissegundos é o workaround confirmado no upstream
    flok/pydualsense#66; USB permanece sem atraso.
    """

    _BT_WRITE_INTERVAL_SECONDS = 0.01

    def writeReport(self, report: list[int]) -> None:  # noqa: N802 - API upstream
        super().writeReport(report)
        if self.conType == ConnectionType.BT:
            time.sleep(self._BT_WRITE_INTERVAL_SECONDS)


class PyDualSenseController(IController):
    """Implementação de `IController` baseada em `pydualsense`."""

    def __init__(self, evdev_reader: EvdevReader | None = None) -> None:
        self._ds: pydualsense | None = None
        self._transport: Transport = "usb"
        # BUG-DAEMON-NO-DEVICE-FATAL-01: estado "offline-OK". Marcado quando
        # `pydualsense.init()` levanta `Exception("No device detected")` —
        # daemon segue vivo, IPC/UDP/CLI funcionais, e `connect()` é
        # retentado periodicamente pelo `reconnect_loop` do daemon.
        self._offline: bool = False
        # HOTFIX-2: evdev como fonte primária de input (contorna conflito
        # com kernel hid_playstation). pydualsense segue como caminho de
        # output (triggers, LED, rumble).
        self._evdev = evdev_reader if evdev_reader is not None else EvdevReader()

    def connect(self) -> None:
        # Hot-reconnect probe: se já temos um `pydualsense` instanciado e
        # ele está conectado, reutiliza. Senão, zera o estado e tenta
        # `init()` novamente — fluxo compartilhado entre boot inicial e
        # `reconnect_loop` periódico.
        if self._ds is not None and self.is_connected():
            logger.debug("pydualsense ja conectado; reutilizando")
            return
        # Estado anterior pode ter ficado parcial (offline ou ds morto).
        # Zera antes de retentar para evitar `is_connected()` falso-positivo.
        self._ds = None
        ds = pydualsense()
        try:
            ds.init()
        except Exception as exc:
            # `pydualsense.__find_device()` levanta `Exception("No device detected")`
            # (string match — não é uma subclasse dedicada). Tratamos como
            # offline-OK; demais exceções (permissão hidraw, USB transitório)
            # propagam para o chamador (`connect_with_retry` faz backoff).
            if "No device detected" in str(exc):
                if not self._offline:
                    logger.info(
                        "controle offline (No device detected) — "
                        "daemon segue, retentando em background"
                    )
                self._ds = None
                self._offline = True
                return
            raise
        self._ds = ds
        self._offline = False
        self._transport = self._detect_transport(ds)
        # Inicia leitor evdev em paralelo; sem device evdev, cai no fallback
        # pydualsense pra input (pode ficar zerado se kernel hid_playstation
        # estiver capturando — ver HOTFIX-2).
        if self._evdev.is_available():
            self._evdev.start()
            logger.info("controle conectado via %s + evdev", self._transport)
        else:
            logger.info("controle conectado via %s (sem evdev, input pode ficar zerado)",
                        self._transport)

    def disconnect(self) -> None:
        if self._ds is None:
            return
        try:
            self._evdev.stop()
        except Exception as exc:
            logger.warning("falha ao parar evdev reader: %s", exc, exc_info=True)
        try:
            self._ds.close()
        finally:
            self._ds = None

    def is_connected(self) -> bool:
        if self._ds is None:
            return False
        # `ds.connected` é o canônico do pydualsense (bool); conType existe
        # mas pode estar setado mesmo depois de close.
        # AUDIT-FINDING-LOG-EXC-INFO-01: default conservador `False` quando attr
        # ausente — atributo indefinido significa estado desconhecido, não conectado.
        return bool(getattr(self._ds, "connected", False))

    def read_state(self) -> ControllerState:
        # BUG-DAEMON-NO-DEVICE-FATAL-01: quando offline, devolve snapshot
        # neutro em vez de levantar. Daemon segue rodando o poll_loop e
        # publica estado vazio para CLI/GUI/IPC.
        if self._ds is None:
            return ControllerState(
                battery_pct=0,
                l2_raw=0,
                r2_raw=0,
                connected=False,
                transport=self._transport,
                raw_lx=128,
                raw_ly=128,
                raw_rx=128,
                raw_ry=128,
                buttons_pressed=frozenset(),
            )
        ds = self._ds
        battery = self._read_battery_raw(ds)
        # HOTFIX-2: evdev é fonte primária de input quando disponível.
        if self._evdev.is_available():
            snap = self._evdev.snapshot()
            # Consolida botões: evdev (ramo primário) + HID-raw do Mic (INFRA-MIC-HID-01).
            # O botão Mic não tem keycode evdev estável — vem por `ds.state.micBtn`
            # (byte misc2, bit 0x04). Tratamento defensivo: primeiro tick pode
            # ter state cru antes do firmware enviar o primeiro report completo.
            buttons = set(snap.buttons_pressed)
            try:
                if bool(getattr(ds.state, "micBtn", False)):
                    buttons.add("mic_btn")
            except AttributeError:  # state cru no primeiro tick — ds.state pode faltar atributos
                logger.debug("ds_state_mic_btn_indisponivel_evdev_path", exc_info=True)
            buttons_pressed = frozenset(buttons)
            return ControllerState(
                battery_pct=battery,
                l2_raw=snap.l2_raw,
                r2_raw=snap.r2_raw,
                connected=self.is_connected(),
                transport=self._transport,
                raw_lx=snap.lx,
                raw_ly=snap.ly,
                raw_rx=snap.rx,
                raw_ry=snap.ry,
                buttons_pressed=buttons_pressed,
            )
        # Fallback pydualsense: HOTFIX-1 corrigiu os atributos, mas em
        # runtime com hid_playstation ativo os valores não atualizam.
        # Sem evdev, botões evdev ficam vazios; apenas `micBtn` (HID-raw) é
        # garantido pelo pydualsense mesmo neste ramo.
        state = ds.state
        l2_raw = int(getattr(state, "L2_value", 0)) & 0xFF
        r2_raw = int(getattr(state, "R2_value", 0)) & 0xFF
        buttons_fallback: frozenset[str] = frozenset()
        try:
            if bool(getattr(state, "micBtn", False)):
                buttons_fallback = frozenset({"mic_btn"})
        except AttributeError:
            logger.debug("ds_state_mic_btn_indisponivel_fallback_path", exc_info=True)
        return ControllerState(
            battery_pct=battery,
            l2_raw=l2_raw,
            r2_raw=r2_raw,
            connected=self.is_connected(),
            transport=self._transport,
            raw_lx=int(state.LX) & 0xFF,
            raw_ly=int(state.LY) & 0xFF,
            raw_rx=int(state.RX) & 0xFF,
            raw_ry=int(state.RY) & 0xFF,
            buttons_pressed=buttons_fallback,
        )

    def set_trigger(self, side: Side, effect: TriggerEffect) -> None:
        if self._ds is None:
            logger.debug("set_trigger offline no-op (side=%s)", side)
            return
        ds = self._ds
        trigger = ds.triggerL if side == "left" else ds.triggerR
        trigger.mode = self._coerce_mode(effect.mode)
        for idx, value in enumerate(effect.forces):
            trigger.setForce(idx, value)

    def set_led(self, color: tuple[int, int, int]) -> None:
        if self._ds is None:
            logger.debug("set_led offline no-op")
            return
        ds = self._ds
        r, g, b = color
        ds.light.setColorI(r, g, b)

    def set_rumble(self, weak: int, strong: int) -> None:
        if self._ds is None:
            logger.debug("set_rumble offline no-op")
            return
        ds = self._ds
        ds.setLeftMotor(strong)
        ds.setRightMotor(weak)

    def set_mic_led(self, muted: bool) -> None:
        """Acende/apaga o LED do microfone do DualSense (INFRA-SET-MIC-LED-01).

        Delega para `ds.audio.setMicrophoneLED(bool)`. A pydualsense cuida da
        diferença USB/BT em `prepareReport` (outReport[9] USB / outReport[10] BT).
        `ds.audio` é garantido pelo `pydualsense.__init__` — não pode ser None
        após `_require()` retornar com sucesso.
        """
        if self._ds is None:
            logger.debug("set_mic_led offline no-op")
            return
        ds = self._ds
        ds.audio.setMicrophoneLED(bool(muted))

    def set_player_leds(self, bits: tuple[bool, bool, bool, bool, bool]) -> None:
        """Aplica bitmask de 5 LEDs de player no hardware.

        `pydualsense.DSLight.playerNumber` é do tipo `PlayerID` (`IntFlag`), que
        aceita qualquer valor inteiro — não apenas os 4 canônicos (4, 10, 21, 27).
        Isso permite combinações arbitrárias de LEDs sem acesso HID bruto.

        O bitmask é montado como:
          bit0 = bits[0] (LED 1, extremo esquerdo)
          bit1 = bits[1] (LED 2)
          bit2 = bits[2] (LED 3, central — o LED do Player 1 canônico)
          bit3 = bits[3] (LED 4)
          bit4 = bits[4] (LED 5, extremo direito)

        Referência: outReport[44] (USB) / outReport[45] (BT) em
        pydualsense/pydualsense.py:572/636 — recebe `self.light.playerNumber.value`.
        """
        from pydualsense.enums import PlayerID

        if self._ds is None:
            logger.debug("set_player_leds offline no-op")
            return
        ds = self._ds
        bitmask = sum(1 << i for i, b in enumerate(bits) if b)
        ds.light.playerNumber = PlayerID(bitmask)
        logger.debug(
            "player_leds_aplicados bits=%s bitmask=%s",
            list(bits),
            bitmask,
        )

    def get_battery(self) -> int:
        if self._ds is None:
            return 0
        return self._read_battery_raw(self._ds)

    def get_transport(self) -> Transport:
        return self._transport

    def _require(self) -> pydualsense:
        if self._ds is None:
            raise RuntimeError("pydualsense não inicializado — chamar connect() antes")
        return self._ds

    @staticmethod
    def _detect_transport(ds: pydualsense) -> Transport:
        con = getattr(ds, "conType", None)
        if con is None:
            return "usb"
        name = str(getattr(con, "name", con)).lower()
        return "usb" if "usb" in name else "bt"

    @staticmethod
    def _read_battery_raw(ds: pydualsense) -> int:
        # HOTFIX-1: battery vive em `ds.battery` (top-level), não em ds.state.
        # DSBattery expõe `Level` (0-100) e `State` (enum BatteryState).
        battery = getattr(ds, "battery", None)
        if battery is None:
            return 0
        level = getattr(battery, "Level", None)
        if level is None:
            return 0
        try:
            value = int(level)
        except (TypeError, ValueError):
            return 0
        return max(0, min(100, value))

    @staticmethod
    def _coerce_mode(mode: int) -> object:
        from pydualsense.enums import TriggerModes
        try:
            return TriggerModes(mode)
        except ValueError:
            logger.warning("mode fora do enum TriggerModes: %s — mantendo raw", mode)
            return mode


__all__ = ["PyDualSenseController"]
