"""Factories dos 19 presets de trigger conforme DSX Paliverse.

Cada factory produz um `TriggerEffect` (definido em `hefesto_dualsense4unix.core.controller`)
com `mode` low-level (valores do enum `pydualsense.TriggerModes`) e 7 bytes
de `forces` no formato HID. Ver `docs/protocol/trigger-modes.md` para a
tabela canônica e a distinção entre HID e presets.

Todas as factories validam `ranges` e convertem amplitudes nomeadas (0-8)
em bytes HID (0-255) multiplicando por `AMPLITUDE_SCALE`. Uso típico:

    from hefesto_dualsense4unix.core.trigger_effects import galloping, machine
    controller.set_trigger("right", galloping(0, 9, 7, 7, 10))
    controller.set_trigger("left",  machine(0, 9, 3, 3, 50, 8))

`TriggerMode` expõe os valores do enum do pydualsense sem exigir
que o caller faça o import (mantém o backend trocável — ADR-001).
"""
from __future__ import annotations

from enum import IntEnum
from typing import Any

from hefesto_dualsense4unix.core.controller import TriggerEffect

AMPLITUDE_SCALE = 32  # Normaliza 0-8 (DSX) -> 0-255 (HID byte)


class TriggerMode(IntEnum):
    """Modos HID baixos aceitos pelo DualSense (espelha `pydualsense.TriggerModes`)."""

    OFF = 0x00
    RIGID = 0x01
    PULSE = 0x02
    RIGID_A = 0x01 | 0x20
    RIGID_B = 0x01 | 0x04
    RIGID_AB = 0x01 | 0x20 | 0x04
    PULSE_A = 0x02 | 0x20
    PULSE_B = 0x02 | 0x04
    PULSE_AB = 0x02 | 0x20 | 0x04
    CALIBRATION = 0xFC


ZERO7 = (0, 0, 0, 0, 0, 0, 0)


DSX_CUSTOM_MODE_TO_TRIGGER_MODE: dict[str, int] = {
    "OFF": TriggerMode.OFF,
    "Rigid": TriggerMode.RIGID,
    "RigidA": TriggerMode.RIGID_A,
    "RigidB": TriggerMode.RIGID_B,
    "RigidAB": TriggerMode.RIGID_AB,
    "Pulse": TriggerMode.PULSE,
    "PulseA": TriggerMode.PULSE_A,
    "PulseB": TriggerMode.PULSE_B,
    "PulseAB": TriggerMode.PULSE_AB,
    # DSX custom hybrid modes ride on the pulse family; the 7 raw force bytes
    # decide whether the firmware behaves like pulse, vibrate-pulse, or
    # vibrate-resistance. Keeping the raw bytes intact preserves DSX semantics.
    "VibrateResistance": TriggerMode.PULSE,
    "VibrateResistanceA": TriggerMode.PULSE_A,
    "VibrateResistanceB": TriggerMode.PULSE_B,
    "VibrateResistanceAB": TriggerMode.PULSE_AB,
    "VibratePulse": TriggerMode.PULSE,
    "VibratePulseA": TriggerMode.PULSE_A,
    "VibratePulseB": TriggerMode.PULSE_B,
    "VibratePulseAB": TriggerMode.PULSE_AB,
}


def _byte(value: int, *, name: str, lo: int = 0, hi: int = 255) -> int:
    if not (lo <= value <= hi):
        raise ValueError(f"{name} fora do range {lo}-{hi}: {value}")
    return value


def _amp(value: int, *, name: str) -> int:
    """Converte amplitude nomeada (0-8) para byte HID com clamp em 255.

    Fator 32 expande 0-7 para 0-224; 8 satura em 255 (byte máximo).
    """
    _byte(value, name=name, lo=0, hi=8)
    return min(255, value * AMPLITUDE_SCALE)


def _pos(value: int, *, name: str) -> int:
    return _byte(value, name=name, lo=0, hi=9)


# ---------------------------------------------------------------------------
# Presets nomeados (19 itens conforme docs/protocol/trigger-modes.md)
# ---------------------------------------------------------------------------


def off() -> TriggerEffect:
    return TriggerEffect(mode=TriggerMode.OFF, forces=ZERO7)


def rigid(position: int, force: int) -> TriggerEffect:
    """Barreira rígida numa posição. `position` 0-9, `force` 0-255."""
    return TriggerEffect(
        mode=TriggerMode.RIGID_B,
        forces=(_pos(position, name="position"), _byte(force, name="force"), 0, 0, 0, 0, 0),
    )


def simple_rigid(strength: int) -> TriggerEffect:
    """Atalho: rigid na base com força em escala 0-8."""
    return TriggerEffect(
        mode=TriggerMode.RIGID_B,
        forces=(0, _amp(strength, name="strength"), 0, 0, 0, 0, 0),
    )


def pulse() -> TriggerEffect:
    return TriggerEffect(mode=TriggerMode.PULSE, forces=ZERO7)


def pulse_a(start: int, end: int, force: int) -> TriggerEffect:
    _check_start_end(start, end)
    s = _pos(start, name="start")
    e = _pos(end, name="end")
    f = _byte(force, name="force")
    return TriggerEffect(mode=TriggerMode.PULSE_A, forces=(s, e, f, 0, 0, 0, 0))


def pulse_b(start: int, end: int, force: int) -> TriggerEffect:
    _check_start_end(start, end)
    s = _pos(start, name="start")
    e = _pos(end, name="end")
    f = _byte(force, name="force")
    return TriggerEffect(mode=TriggerMode.PULSE_B, forces=(s, e, f, 0, 0, 0, 0))


def resistance(start: int, force: int) -> TriggerEffect:
    """Resistência contínua a partir de `start` com força 0-8."""
    return TriggerEffect(
        mode=TriggerMode.RIGID_AB,
        forces=(_pos(start, name="start"), _amp(force, name="force"), 0, 0, 0, 0, 0),
    )


def bow(start: int, end: int, force: int, snap: int) -> TriggerEffect:
    """Simula arco: tensão crescente entre `start` e `end`, `snap` ao soltar."""
    _byte(start, name="start", lo=0, hi=8)
    _byte(end, name="end", lo=1, hi=9)
    if end <= start:
        raise ValueError(f"bow: end ({end}) deve ser > start ({start})")
    return TriggerEffect(
        mode=TriggerMode.PULSE_AB,
        forces=(start, end, _amp(force, name="force"), _amp(snap, name="snap"), 0, 0, 0),
    )


def galloping(
    start: int, end: int, first_foot: int, second_foot: int, frequency: int
) -> TriggerEffect:
    """Cadência de galope (5 params canônicos; HID usa 7 forces)."""
    _byte(start, name="start", lo=0, hi=8)
    _byte(end, name="end", lo=1, hi=9)
    if end <= start:
        raise ValueError(f"galloping: end ({end}) deve ser > start ({start})")
    _byte(first_foot, name="first_foot", lo=0, hi=7)
    _byte(second_foot, name="second_foot", lo=0, hi=7)
    _byte(frequency, name="frequency")
    return TriggerEffect(
        mode=TriggerMode.PULSE_AB,
        forces=(start, end, first_foot, second_foot, frequency, 0, 0),
    )


def semi_auto_gun(start: int, end: int, force: int) -> TriggerEffect:
    _byte(start, name="start", lo=2, hi=7)
    _byte(end, name="end", lo=start + 1, hi=8)
    return TriggerEffect(
        mode=TriggerMode.PULSE_AB,
        forces=(start, end, _amp(force, name="force"), 0, 0, 0, 0),
    )


def auto_gun(start: int, strength: int, frequency: int) -> TriggerEffect:
    return TriggerEffect(
        mode=TriggerMode.PULSE_AB,
        forces=(
            _pos(start, name="start"),
            _amp(strength, name="strength"),
            _byte(frequency, name="frequency"),
            0,
            0,
            0,
            0,
        ),
    )


def machine(
    start: int, end: int, amp_a: int, amp_b: int, frequency: int, period: int
) -> TriggerEffect:
    """Machine gun style. 6 params nomeados, HID usa 7 forces (última zero)."""
    _check_start_end(start, end)
    return TriggerEffect(
        mode=TriggerMode.PULSE_AB,
        forces=(
            start,
            end,
            _byte(amp_a, name="amp_a"),
            _byte(amp_b, name="amp_b"),
            _byte(frequency, name="frequency"),
            _byte(period, name="period"),
            0,
        ),
    )


def feedback(position: int, strength: int) -> TriggerEffect:
    return TriggerEffect(
        mode=TriggerMode.RIGID_B,
        forces=(_pos(position, name="position"), _amp(strength, name="strength"), 0, 0, 0, 0, 0),
    )


def weapon(start: int, end: int, force: int) -> TriggerEffect:
    _check_start_end(start, end)
    return TriggerEffect(
        mode=TriggerMode.PULSE_B,
        forces=(start, end, _byte(force, name="force"), 0, 0, 0, 0),
    )


def vibration(position: int, amplitude: int, frequency: int) -> TriggerEffect:
    return TriggerEffect(
        mode=TriggerMode.PULSE_A,
        forces=(
            _pos(position, name="position"),
            _amp(amplitude, name="amplitude"),
            _byte(frequency, name="frequency"),
            0,
            0,
            0,
            0,
        ),
    )


def slope_feedback(
    start: int, end: int, start_strength: int, end_strength: int
) -> TriggerEffect:
    _check_start_end(start, end)
    _byte(start_strength, name="start_strength", lo=1, hi=8)
    _byte(end_strength, name="end_strength", lo=1, hi=8)
    return TriggerEffect(
        mode=TriggerMode.RIGID_AB,
        forces=(
            start,
            end,
            min(255, start_strength * AMPLITUDE_SCALE),
            min(255, end_strength * AMPLITUDE_SCALE),
            0,
            0,
            0,
        ),
    )


def multi_position_feedback(strengths: list[int]) -> TriggerEffect:
    """Strength por posição (array de 10). Empacota em bits HID."""
    if len(strengths) != 10:
        raise ValueError(f"multi_position_feedback: precisa 10 strengths, recebeu {len(strengths)}")
    for idx, s in enumerate(strengths):
        _byte(s, name=f"strengths[{idx}]", lo=0, hi=8)
    packed = _pack_strengths_bits(strengths)
    return TriggerEffect(mode=TriggerMode.RIGID_AB, forces=packed)


def multi_position_vibration(frequency: int, strengths: list[int]) -> TriggerEffect:
    if len(strengths) != 10:
        raise ValueError(
            f"multi_position_vibration: precisa 10 strengths, recebeu {len(strengths)}"
        )
    for idx, s in enumerate(strengths):
        _byte(s, name=f"strengths[{idx}]", lo=0, hi=8)
    packed_bits = _pack_strengths_bits(strengths)
    return TriggerEffect(
        mode=TriggerMode.PULSE_A,
        forces=(_byte(frequency, name="frequency"), *packed_bits[:6]),
    )


def _normalize_dsx_custom_mode(mode: str | int) -> int:
    if isinstance(mode, int):
        return _byte(mode, name="mode")
    key = str(mode).replace(" ", "")
    try:
        return int(DSX_CUSTOM_MODE_TO_TRIGGER_MODE[key])
    except KeyError as exc:
        raise ValueError(f"custom: modo DSX desconhecido: {mode}") from exc


def custom(mode: str | int, *forces: Any) -> TriggerEffect:
    """Escape hatch: envia mode + forces cru.

    Aceita:
    - `custom(34, 0, 9, 7, 7, 10, 0, 0)`
    - `custom("VibrateResistance", 23, 198, 5, 0, 0, 0, 0)`
    - `custom("PulseB", (10, 255, 0, 0, 0, 0, 0))`
    """
    if len(forces) == 1 and isinstance(forces[0], (tuple, list)):
        raw_forces = tuple(int(v) for v in forces[0])
    else:
        raw_forces = tuple(int(v) for v in forces)
    if len(raw_forces) != 7:
        raise ValueError(f"custom: forces precisa 7 elementos, recebeu {len(raw_forces)}")
    fixed: tuple[int, int, int, int, int, int, int] = (
        _byte(raw_forces[0], name="forces[0]"),
        _byte(raw_forces[1], name="forces[1]"),
        _byte(raw_forces[2], name="forces[2]"),
        _byte(raw_forces[3], name="forces[3]"),
        _byte(raw_forces[4], name="forces[4]"),
        _byte(raw_forces[5], name="forces[5]"),
        _byte(raw_forces[6], name="forces[6]"),
    )
    return TriggerEffect(mode=_normalize_dsx_custom_mode(mode), forces=fixed)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _check_start_end(start: int, end: int) -> None:
    _pos(start, name="start")
    _pos(end, name="end")
    if end <= start:
        raise ValueError(f"end ({end}) deve ser > start ({start})")


def _flatten_multi_position(nested: list[list[int]]) -> list[int]:
    """Achata params aninhado em lista de 10 strengths para multi_position_*.

    Aceita três dimensões canônicas:

    - **10 sublistas** (1:1): cada `[v]` vira uma posição — se a sublista tem
      mais de um valor, usa o primeiro e descarta o restante. Uso típico:
      `[[0],[1],[2],[3],[4],[5],[6],[7],[8],[8]]`.
    - **5 sublistas** (2 posições por zona): cada `[a, b]` expande para
      duas posições consecutivas (a, b). Espera `len(item) == 2` em cada
      sublista; erro se diferente.
    - **2 sublistas** (5 posições por zona): primeira sublista define 5
      posições iniciais, segunda define 5 finais. Espera `len(item) == 5`.

    Qualquer outra dimensão levanta `ValueError`. Os valores finais devem
    caber em 0-8 (range canônico do DualSense para multi-position).
    """
    n = len(nested)
    if n == 10:
        flat: list[int] = []
        for idx, sub in enumerate(nested):
            if not sub:
                raise ValueError(
                    f"_flatten_multi_position(10): sublista [{idx}] vazia"
                )
            flat.append(int(sub[0]))
        return flat
    if n == 5:
        flat = []
        for idx, sub in enumerate(nested):
            if len(sub) != 2:
                raise ValueError(
                    f"_flatten_multi_position(5): sublista [{idx}] "
                    f"precisa exatamente 2 valores, recebeu {len(sub)}"
                )
            flat.extend(int(v) for v in sub)
        return flat
    if n == 2:
        flat = []
        for idx, sub in enumerate(nested):
            if len(sub) != 5:
                raise ValueError(
                    f"_flatten_multi_position(2): sublista [{idx}] "
                    f"precisa exatamente 5 valores, recebeu {len(sub)}"
                )
            flat.extend(int(v) for v in sub)
        return flat
    raise ValueError(
        f"_flatten_multi_position: dimensão {n} não suportada "
        "(esperado 2, 5 ou 10 sublistas)"
    )


def _pack_strengths_bits(
    strengths: list[int],
) -> tuple[int, int, int, int, int, int, int]:
    """Empacota 10 strengths (0-8) em bits HID.

    Cada posição usa 3 bits. 10 * 3 = 30 bits, cabe em 4 bytes. Restantes
    preenchidos com 0. Layout: bytes low-endian sequenciais.
    """
    bits = 0
    for i, s in enumerate(strengths):
        bits |= (s & 0x7) << (i * 3)
    b0 = bits & 0xFF
    b1 = (bits >> 8) & 0xFF
    b2 = (bits >> 16) & 0xFF
    b3 = (bits >> 24) & 0xFF
    return (b0, b1, b2, b3, 0, 0, 0)


# ---------------------------------------------------------------------------
# Registry de presets por nome (CLI e perfis JSON referenciam por string).
# ---------------------------------------------------------------------------


PRESET_FACTORIES = {
    "Off": off,
    "Rigid": rigid,
    "SimpleRigid": simple_rigid,
    "Pulse": pulse,
    "PulseA": pulse_a,
    "PulseB": pulse_b,
    "Resistance": resistance,
    "Bow": bow,
    "Galloping": galloping,
    "SemiAutoGun": semi_auto_gun,
    "AutoGun": auto_gun,
    "Machine": machine,
    "Feedback": feedback,
    "Weapon": weapon,
    "Vibration": vibration,
    "SlopeFeedback": slope_feedback,
    "MultiPositionFeedback": multi_position_feedback,
    "MultiPositionVibration": multi_position_vibration,
    "Custom": custom,
    # DSX custom hybrid modes — RacingDSX sends these with 7 raw force bytes.
    # The `custom` factory already handles the mode name → TriggerMode mapping
    # via DSX_CUSTOM_MODE_TO_TRIGGER_MODE and passes the 7 forces through.
    "VibrateResistance": custom,
    "VibrateResistanceA": custom,
    "VibrateResistanceB": custom,
    "VibrateResistanceAB": custom,
    "VibratePulse": custom,
    "VibratePulseA": custom,
    "VibratePulseB": custom,
    "VibratePulseAB": custom,
}


def build_from_name(
    name: str,
    params: list[Any] | list[list[int]] | dict[str, Any],
) -> TriggerEffect:
    """Resolve preset por nome + params. Aceita posicional (list), nomeado (dict) ou aninhado.

    Formato aninhado (`list[list[int]]`) é canônico para os modos
    `MultiPositionFeedback` e `MultiPositionVibration`: permite expressar
    os 10 strengths em JSON com agrupamento por zona (2/5/10 sublistas).
    Ver `_flatten_multi_position` para a expansão.

    Para `MultiPositionVibration`, `frequency` é implicitamente 0 quando
    o formato aninhado é usado — ajuste fino de frequency exige formato
    dict (`{"frequency": N, "strengths": [...]}`) ou posicional.
    """
    from typing import Any, cast
    factory = cast("Any", PRESET_FACTORIES.get(name))
    if factory is None:
        raise ValueError(f"preset desconhecido: {name}")

    # Detecta formato aninhado e expande para a assinatura correta da factory.
    if (
        isinstance(params, list)
        and params
        and isinstance(params[0], list)
    ):
        nested = cast("list[list[int]]", params)
        if name == "MultiPositionFeedback":
            strengths = _flatten_multi_position(nested)
            result = factory(strengths)
        elif name == "MultiPositionVibration":
            strengths = _flatten_multi_position(nested)
            result = factory(0, strengths)
        else:
            raise ValueError(
                f"params aninhado só é aceito para MultiPositionFeedback "
                f"ou MultiPositionVibration; recebido: {name}"
            )
    elif isinstance(params, dict):
        result = factory(**params)
    else:
        result = factory(*params)
    assert isinstance(result, TriggerEffect)
    return result


__all__ = [
    "AMPLITUDE_SCALE",
    "DSX_CUSTOM_MODE_TO_TRIGGER_MODE",
    "PRESET_FACTORIES",
    "TriggerMode",
    "auto_gun",
    "bow",
    "build_from_name",
    "custom",
    "feedback",
    "galloping",
    "machine",
    "multi_position_feedback",
    "multi_position_vibration",
    "off",
    "pulse",
    "pulse_a",
    "pulse_b",
    "resistance",
    "rigid",
    "semi_auto_gun",
    "simple_rigid",
    "slope_feedback",
    "vibration",
    "weapon",
]
