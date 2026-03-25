"""Prompt-to-circuit synthesis using reusable building blocks."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .block_library import (
    CircuitBuilder,
    add_555_timer,
    add_button_input,
    add_comparator_stage,
    add_decoupling_cap,
    add_input_protection,
    add_led_indicator,
    add_linear_regulator,
    add_minimal_mcu,
    add_mosfet_low_side_switch,
    add_opamp_buffer,
    add_output_header,
    add_power_input,
    add_rc_lowpass,
    add_relay_driver,
    add_usb_power_entry,
    add_voltage_divider,
)
from .prompt_parser import DesignIntent, parse_prompt


def synthesize_circuit(
    prompt: str,
    constraints: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Generate a deterministic circuit graph from a parsed prompt."""
    intent = parse_prompt(prompt, constraints)
    builder = CircuitBuilder()

    input_net = _input_net(intent)
    main_supply = _main_supply_net(intent)
    description = intent.title
    simple_led_circuit = _is_simple_led_circuit(intent)
    simple_passive_signal_circuit = _is_simple_passive_signal_circuit(intent)

    if simple_led_circuit:
        add_power_input(builder, net=input_net, label="Battery input")
    elif intent.wants_usb:
        add_usb_power_entry(builder, vbus_net=input_net)
    elif not simple_passive_signal_circuit:
        add_power_input(builder, net=input_net, label="Primary power input")

    if intent.wants_protection and not simple_led_circuit and not simple_passive_signal_circuit:
        protected_net = "VIN_PROTECTED"
        add_input_protection(builder, input_net=input_net, protected_net=protected_net)
        input_net = protected_net
        if not intent.wants_regulator:
            main_supply = protected_net
        add_output_header(builder, signal_net=protected_net, label="Protected output")
        synthesized = True

    synthesized = False

    if simple_led_circuit:
        add_led_indicator(builder, input_net=input_net, label="Indicator LED")
        synthesized = True

    if intent.wants_regulator:
        add_linear_regulator(
            builder,
            input_net=input_net,
            output_net=main_supply,
            output_voltage=_output_voltage_label(intent),
        )
        if any(token in intent.normalized_prompt for token in ("output", "header", "board", "rail")):
            add_output_header(builder, signal_net=main_supply, label="Regulated output")
        synthesized = True

    supply_for_logic = main_supply if intent.wants_regulator else input_net

    if intent.wants_timer:
        add_555_timer(builder, supply_net=supply_for_logic, output_net="TIMER_OUT")
        add_output_header(builder, signal_net="TIMER_OUT", label="Timer output")
        if intent.wants_led or "blink" in intent.normalized_prompt:
            add_led_indicator(builder, input_net="TIMER_OUT", label="Timer-driven LED")
        synthesized = True

    if intent.wants_mcu:
        add_minimal_mcu(builder, supply_net=supply_for_logic, io_net="GPIO_OUT", sensor_net="SENSE_IN")
        if intent.wants_led:
            add_led_indicator(builder, input_net="GPIO_OUT", label="MCU status LED")
        if intent.wants_sensor:
            add_output_header(builder, signal_net="SENSE_IN", label="Sensor input header")
        synthesized = True

    if intent.wants_switch:
        if intent.wants_mcu:
            control_net = "GPIO_OUT"
        elif intent.wants_comparator:
            control_net = "CMP_OUT"
        else:
            control_net = "CTRL"
        if not intent.wants_mcu and not intent.wants_comparator:
            add_output_header(builder, signal_net=control_net, label="Control input")
        load_supply = "12V" if (intent.supply_voltage or 0) >= 9 else supply_for_logic
        if load_supply != input_net:
            add_power_input(builder, net=load_supply, label="Load supply input")
        add_mosfet_low_side_switch(
            builder,
            control_net=control_net,
            supply_net=load_supply,
            switched_net="LOAD_RETURN",
        )
        synthesized = True

    if intent.wants_opamp:
        add_output_header(builder, signal_net="ANALOG_IN", label="Analog input")
        add_opamp_buffer(builder, input_net="ANALOG_IN", output_net="BUFFER_OUT", supply_net=supply_for_logic)
        add_output_header(builder, signal_net="BUFFER_OUT", label="Buffered output")
        synthesized = True

    if intent.wants_comparator:
        add_output_header(builder, signal_net="SENSE_IN", label="Comparator input")
        add_comparator_stage(builder, input_net="SENSE_IN", output_net="CMP_OUT", supply_net=supply_for_logic)
        add_output_header(builder, signal_net="CMP_OUT", label="Comparator output")
        synthesized = True

    if intent.wants_relay:
        if intent.wants_mcu:
            control_net = "GPIO_OUT"
        elif intent.wants_comparator:
            control_net = "CMP_OUT"
        else:
            control_net = "RELAY_CTRL"
        if not intent.wants_mcu and not intent.wants_comparator:
            add_output_header(builder, signal_net=control_net, label="Relay control input")
        add_relay_driver(builder, control_net=control_net, supply_net=("12V" if (intent.supply_voltage or 0) >= 9 else supply_for_logic))
        synthesized = True

    if intent.wants_usb and not any(family in intent.families for family in ("regulator", "mcu", "relay", "switch")):
        add_output_header(builder, signal_net=supply_for_logic, label="USB power output")
        synthesized = True

    if intent.wants_button:
        add_button_input(builder, output_net="BTN_OUT", supply_net=supply_for_logic)
        synthesized = True

    if intent.wants_divider and not intent.wants_comparator:
        divider_input_net = "VIN" if simple_passive_signal_circuit else input_net
        add_output_header(
            builder,
            signal_net=divider_input_net,
            label="Input header" if simple_passive_signal_circuit else "Divider input",
        )
        add_voltage_divider(builder, input_net=divider_input_net, output_net="DIV_OUT")
        add_output_header(
            builder,
            signal_net="DIV_OUT",
            label="Output header" if simple_passive_signal_circuit else "Divider output",
        )
        synthesized = True

    if intent.wants_filter:
        filter_input_net = "VIN" if simple_passive_signal_circuit else input_net
        add_output_header(
            builder,
            signal_net=filter_input_net,
            label="Input header" if simple_passive_signal_circuit else "Filter input",
        )
        add_rc_lowpass(builder, input_net=filter_input_net, output_net="FILTER_OUT")
        add_output_header(builder, signal_net="FILTER_OUT", label="Filtered output")
        synthesized = True

    if intent.wants_led and not simple_led_circuit and not any(family in intent.families for family in ("timer", "mcu")):
        add_led_indicator(builder, input_net=supply_for_logic, label="Power/status LED")
        synthesized = True

    if intent.wants_sensor and not intent.wants_mcu and not intent.wants_opamp and not intent.wants_comparator:
        add_output_header(builder, signal_net="SENSOR_SIG", label="Sensor signal")
        add_rc_lowpass(builder, input_net="SENSOR_SIG", output_net="FILTER_OUT")
        add_output_header(builder, signal_net="FILTER_OUT", label="Filtered sensor output")
        synthesized = True

    if not synthesized:
        add_voltage_divider(builder, input_net=input_net, output_net="NODE_A")
        add_rc_lowpass(builder, input_net="NODE_A", output_net="NODE_B")
        add_led_indicator(builder, input_net="NODE_B", label="Generic activity LED")
        add_output_header(builder, signal_net="NODE_B", label="Signal output")

    if _needs_decoupling(intent):
        add_decoupling_cap(builder, power_net=supply_for_logic, gnd_net="GND")

    return builder.build(
        description=description,
        metadata={
            "generation_mode": "synthesized",
            "intent": intent.as_dict(),
            "source": "block_library_v1",
        },
    )


def _input_net(intent: DesignIntent) -> str:
    voltage = intent.supply_voltage
    if voltage is None:
        if "battery_powered" in intent.notes:
            return "VBAT"
        return "VCC"
    if abs(voltage - 12.0) < 0.2:
        return "12V"
    if abs(voltage - 9.0) < 0.2:
        return "9V"
    if abs(voltage - 5.0) < 0.2:
        return "5V"
    if abs(voltage - 3.3) < 0.2:
        return "3V3"
    return "VIN"


def _main_supply_net(intent: DesignIntent) -> str:
    if intent.output_voltage is not None:
        if abs(intent.output_voltage - 3.3) < 0.2:
            return "3V3"
        if abs(intent.output_voltage - 5.0) < 0.2:
            return "5V"
        return "VOUT"
    if intent.wants_regulator:
        return "3V3"
    return _input_net(intent)


def _output_voltage_label(intent: DesignIntent) -> str:
    if intent.output_voltage is None:
        return "3.3V"
    return f"{intent.output_voltage:g}V"


def _is_simple_led_circuit(intent: DesignIntent) -> bool:
    prompt = intent.normalized_prompt
    return (
        intent.families == ["led"]
        and ("battery" in prompt or "resistor" in prompt or "current-limiting" in prompt or "current limiting" in prompt)
    )


def _is_simple_passive_signal_circuit(intent: DesignIntent) -> bool:
    families = set(intent.families)
    if not families:
        return False
    return families.issubset({"divider", "filter"})


def _needs_decoupling(intent: DesignIntent) -> bool:
    return any(
        (
            intent.wants_regulator,
            intent.wants_mcu,
            intent.wants_timer,
            intent.wants_opamp,
            intent.wants_comparator,
            intent.wants_switch,
            intent.wants_relay,
            intent.wants_button,
            intent.wants_protection,
            intent.wants_usb,
        )
    )
