"""Structured prompt parsing for generalized circuit synthesis."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Dict, List, Optional


_FAMILY_KEYWORDS = {
    "regulator": ["regulator", "ldo", "buck", "boost", "power supply", "step down", "step-up"],
    "mcu": ["mcu", "microcontroller", "atmega", "attiny", "stm32", "esp32", "arduino"],
    "sensor": ["sensor", "thermistor", "ldr", "photoresistor", "probe", "analog input"],
    "led": ["led", "indicator", "status light", "blinker"],
    "switch": ["mosfet", "transistor switch", "switch", "fan driver", "load driver"],
    "relay": ["relay", "relay driver", "spdt relay", "coil driver"],
    "protection": ["reverse polarity", "input protection", "fuse", "tvs", "surge protection", "polarity protection"],
    "usb": ["usb", "usb-c", "micro usb", "vbus", "usb power"],
    "button": ["button", "pushbutton", "tactile switch", "reset switch"],
    "opamp": ["opamp", "op-amp", "buffer", "voltage follower", "amplifier"],
    "comparator": ["comparator", "threshold detector", "window detector", "lm393"],
    "divider": ["divider", "voltage divider", "resistor divider"],
    "filter": ["filter", "rc filter", "low pass", "high pass"],
    "timer": ["555", "timer", "astable", "monostable", "oscillator", "pwm"],
    "connector": ["connector", "header", "terminal", "screw terminal", "input", "output"],
}


@dataclass
class DesignIntent:
    """Normalized intent extracted from a natural-language PCB prompt."""

    raw_prompt: str
    normalized_prompt: str
    title: str
    families: List[str] = field(default_factory=list)
    primary_family: str = "custom"
    supply_voltage: Optional[float] = None
    output_voltage: Optional[float] = None
    frequency_hz: Optional[float] = None
    load_hint: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def wants_regulator(self) -> bool:
        return "regulator" in self.families

    @property
    def wants_mcu(self) -> bool:
        return "mcu" in self.families

    @property
    def wants_sensor(self) -> bool:
        return "sensor" in self.families

    @property
    def wants_led(self) -> bool:
        return "led" in self.families

    @property
    def wants_switch(self) -> bool:
        return "switch" in self.families

    @property
    def wants_opamp(self) -> bool:
        return "opamp" in self.families

    @property
    def wants_comparator(self) -> bool:
        return "comparator" in self.families

    @property
    def wants_relay(self) -> bool:
        return "relay" in self.families

    @property
    def wants_protection(self) -> bool:
        return "protection" in self.families

    @property
    def wants_usb(self) -> bool:
        return "usb" in self.families

    @property
    def wants_button(self) -> bool:
        return "button" in self.families

    @property
    def wants_divider(self) -> bool:
        return "divider" in self.families

    @property
    def wants_filter(self) -> bool:
        return "filter" in self.families

    @property
    def wants_timer(self) -> bool:
        return "timer" in self.families

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_prompt(prompt: str, constraints: Optional[Dict[str, Any]] = None) -> DesignIntent:
    """Parse a free-text prompt into a lightweight structured intent."""
    cleaned = re.sub(r"\s+", " ", (prompt or "").strip())
    normalized = cleaned.lower()
    families: List[str] = []

    for family, keywords in _FAMILY_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            families.append(family)

    if not families:
        families = _infer_fallback_families(normalized)

    title = cleaned[:120] if cleaned else "Generated circuit"
    supply_voltage = _extract_voltage(normalized)
    output_voltage = _extract_output_voltage(normalized)
    frequency_hz = _extract_frequency(normalized)
    load_hint = _extract_load_hint(normalized)
    notes = _build_notes(normalized, constraints or {})

    primary_family = families[0] if families else "custom"
    if "timer" in families:
        primary_family = "timer"
    elif "switch" in families:
        primary_family = "switch"
    elif "mcu" in families:
        primary_family = "mcu"
    elif "regulator" in families:
        primary_family = "regulator"
    elif "opamp" in families:
        primary_family = "opamp"
    elif "comparator" in families:
        primary_family = "comparator"
    elif "relay" in families:
        primary_family = "relay"
    elif "protection" in families:
        primary_family = "protection"
    elif "usb" in families:
        primary_family = "usb"
    elif "button" in families:
        primary_family = "button"

    return DesignIntent(
        raw_prompt=prompt,
        normalized_prompt=normalized,
        title=title,
        families=families,
        primary_family=primary_family,
        supply_voltage=supply_voltage,
        output_voltage=output_voltage,
        frequency_hz=frequency_hz,
        load_hint=load_hint,
        notes=notes,
    )


def _infer_fallback_families(prompt: str) -> List[str]:
    if "power" in prompt or "supply" in prompt:
        return ["regulator"]
    if "analog" in prompt or "buffer" in prompt:
        return ["opamp"]
    if "blink" in prompt:
        return ["timer", "led"]
    return ["connector", "led"]


def _extract_voltage(prompt: str) -> Optional[float]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*v", prompt)
    return float(match.group(1)) if match else None


def _extract_output_voltage(prompt: str) -> Optional[float]:
    match = re.search(r"(?:to|output|outputs?|provides?)\s+(\d+(?:\.\d+)?)\s*v", prompt)
    return float(match.group(1)) if match else None


def _extract_frequency(prompt: str) -> Optional[float]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(hz|khz|mhz)", prompt)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "khz":
        return value * 1_000
    if unit == "mhz":
        return value * 1_000_000
    return value


def _extract_load_hint(prompt: str) -> Optional[str]:
    match = re.search(r"(fan|motor|led strip|pump|relay|sensor|heater|solenoid)", prompt)
    return match.group(1) if match else None


def _contains_keyword(prompt: str, keyword: str) -> bool:
    if len(keyword) <= 3 and keyword.isalpha():
        return re.search(rf"\b{re.escape(keyword)}\b", prompt) is not None
    return keyword in prompt


def _build_notes(prompt: str, constraints: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    if "low noise" in prompt or "analog" in prompt:
        notes.append("prefer_analog_cleanliness")
    if "compact" in prompt:
        notes.append("prefer_compact")
    if "battery" in prompt:
        notes.append("battery_powered")
    if "usb" in prompt:
        notes.append("usb_interface")
    unsupported_patterns = {
        "unsupported_hbridge": ("h-bridge", "h bridge", "full bridge"),
        "unsupported_bms": ("bms", "battery management", "cell balancing"),
        "unsupported_charger": ("charger", "charging circuit", "charge controller"),
        "unsupported_rf": ("rf", "antenna", "matching network"),
        "unsupported_isolation": ("isolated", "isolation", "optocoupler"),
        "unsupported_smps": ("buck converter", "boost converter", "switching regulator", "smps"),
    }
    for note, keywords in unsupported_patterns.items():
        if any(_contains_keyword(prompt, keyword) for keyword in keywords):
            notes.append(note)
    if constraints:
        notes.append("has_constraints")
    return notes
