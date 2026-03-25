"""Reusable circuit building blocks for synthesized PCB generation."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _pins(*pairs: tuple[str, str]) -> List[Dict[str, str]]:
    return [{"number": number, "name": name} for number, name in pairs]


class CircuitBuilder:
    """Utility for assembling circuit graphs from reusable blocks."""

    def __init__(self) -> None:
        self.components: List[Dict[str, Any]] = []
        self.connections: List[Dict[str, Any]] = []
        self._refs: Dict[str, int] = {}

    def next_ref(self, prefix: str) -> str:
        self._refs[prefix] = self._refs.get(prefix, 0) + 1
        return f"{prefix}{self._refs[prefix]}"

    def add_component(
        self,
        prefix: str,
        lib: str,
        part: str,
        value: str,
        footprint: str,
        description: str,
        pins: List[Dict[str, str]],
    ) -> str:
        ref = self.next_ref(prefix)
        self.components.append(
            {
                "ref": ref,
                "lib": lib,
                "part": part,
                "value": value,
                "footprint": footprint,
                "description": description[:120],
                "pins": pins,
            }
        )
        return ref

    def connect(self, net: str, *pins: str, properties: Optional[Dict[str, Any]] = None) -> None:
        clean = []
        for pin in pins:
            if pin and pin not in clean:
                clean.append(pin)
        if not clean:
            return
        for conn in self.connections:
            if conn["net"] == net:
                for pin in clean:
                    if pin not in conn["pins"]:
                        conn["pins"].append(pin)
                return
        payload: Dict[str, Any] = {"net": net, "pins": clean}
        if properties:
            payload["properties"] = properties
        self.connections.append(payload)

    def build(self, description: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "description": description[:120],
            "components": self.components,
            "connections": [c for c in self.connections if len(c.get("pins", [])) >= 2],
        }
        if metadata:
            data["metadata"] = metadata
        return data


def add_usb_power_entry(builder: CircuitBuilder, vbus_net: str = "5V", gnd_net: str = "GND") -> str:
    ref = builder.add_component(
        "J",
        "Connector_Generic",
        "Conn_01x04",
        "USB power",
        "Connector_USB:USB_C_Receptacle_USB2.0_16P",
        "USB power entry",
        _pins(("1", "VBUS"), ("2", "D-"), ("3", "D+"), ("4", gnd_net)),
    )
    builder.connect(vbus_net, f"{ref}.1")
    builder.connect(gnd_net, f"{ref}.4")
    return ref


def add_input_protection(
    builder: CircuitBuilder,
    input_net: str = "VIN",
    protected_net: str = "VIN_PROTECTED",
    gnd_net: str = "GND",
) -> Dict[str, str]:
    fuse = builder.add_component(
        "F",
        "Device",
        "Fuse",
        "500mA",
        "Fuse:Fuse_1206_3216Metric",
        "Input fuse",
        _pins(("1", "1"), ("2", "2")),
    )
    series_diode = builder.add_component(
        "D",
        "Device",
        "D",
        "SS14",
        "Diode_SMD:D_SMA",
        "Reverse polarity protection diode",
        _pins(("1", "A"), ("2", "K")),
    )
    tvs = builder.add_component(
        "D",
        "Device",
        "D_TVS",
        "SMBJ15A",
        "Diode_SMD:D_SMB",
        "Input TVS protection diode",
        _pins(("1", "A"), ("2", "K")),
    )
    builder.connect(input_net, f"{fuse}.1")
    builder.connect("FUSED_IN", f"{fuse}.2", f"{series_diode}.1")
    builder.connect(protected_net, f"{series_diode}.2", f"{tvs}.2")
    builder.connect(gnd_net, f"{tvs}.1")
    return {"fuse": fuse, "series_diode": series_diode, "tvs": tvs}


def add_button_input(
    builder: CircuitBuilder,
    output_net: str = "BTN_OUT",
    supply_net: str = "VCC",
    gnd_net: str = "GND",
    label: str = "Push button",
) -> Dict[str, str]:
    switch = builder.add_component(
        "SW",
        "Switch",
        "SW_Push",
        label,
        "Button_Switch_SMD:SW_Push_6mm",
        "Momentary push button",
        _pins(("1", output_net), ("2", gnd_net)),
    )
    pullup = builder.add_component(
        "R",
        "Device",
        "R",
        "10k",
        "Resistor_SMD:R_0805_2012Metric",
        "Button pull-up resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    header = builder.add_component(
        "J",
        "Connector_Generic",
        "Conn_01x02",
        "Button output",
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        "Button output header",
        _pins(("1", output_net), ("2", gnd_net)),
    )
    builder.connect(supply_net, f"{pullup}.1")
    builder.connect(output_net, f"{pullup}.2", f"{switch}.1", f"{header}.1")
    builder.connect(gnd_net, f"{switch}.2", f"{header}.2")
    return {"switch": switch, "pullup": pullup, "header": header}


def add_power_input(builder: CircuitBuilder, net: str = "VCC", gnd_net: str = "GND", label: str = "Power input") -> str:
    ref = builder.add_component(
        "J",
        "Connector_Generic",
        "Conn_01x02",
        label,
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        label,
        _pins(("1", net), ("2", gnd_net)),
    )
    builder.connect(net, f"{ref}.1")
    builder.connect(gnd_net, f"{ref}.2")
    return ref


def add_output_header(
    builder: CircuitBuilder,
    signal_net: str,
    gnd_net: str = "GND",
    label: str = "Output header",
) -> str:
    ref = builder.add_component(
        "J",
        "Connector_Generic",
        "Conn_01x02",
        label,
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        label,
        _pins(("1", signal_net), ("2", gnd_net)),
    )
    builder.connect(signal_net, f"{ref}.1")
    builder.connect(gnd_net, f"{ref}.2")
    return ref


def add_decoupling_cap(
    builder: CircuitBuilder,
    power_net: str = "VCC",
    gnd_net: str = "GND",
    value: str = "100nF",
    description: str = "Bypass capacitor",
) -> str:
    ref = builder.add_component(
        "C",
        "Device",
        "C",
        value,
        "Capacitor_SMD:C_0402_1005Metric",
        description,
        _pins(("1", power_net), ("2", gnd_net)),
    )
    builder.connect(power_net, f"{ref}.1")
    builder.connect(gnd_net, f"{ref}.2")
    return ref


def add_led_indicator(
    builder: CircuitBuilder,
    input_net: str = "VCC",
    gnd_net: str = "GND",
    resistor_value: str = "330",
    led_value: str = "Green",
    label: str = "Status LED",
) -> Dict[str, str]:
    resistor = builder.add_component(
        "R",
        "Device",
        "R",
        resistor_value,
        "Resistor_SMD:R_0805_2012Metric",
        "Current-limiting resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    led = builder.add_component(
        "D",
        "Device",
        "LED",
        led_value,
        "LED_SMD:LED_0805_2012Metric",
        label,
        _pins(("1", "A"), ("2", "K")),
    )
    mid_net = f"{led}_ANODE"
    builder.connect(input_net, f"{resistor}.1")
    builder.connect(mid_net, f"{resistor}.2", f"{led}.1")
    builder.connect(gnd_net, f"{led}.2")
    return {"resistor": resistor, "led": led}


def add_voltage_divider(
    builder: CircuitBuilder,
    input_net: str = "VIN",
    output_net: str = "VOUT",
    gnd_net: str = "GND",
    top_value: str = "10k",
    bottom_value: str = "10k",
) -> Dict[str, str]:
    r_top = builder.add_component(
        "R",
        "Device",
        "R",
        top_value,
        "Resistor_SMD:R_0805_2012Metric",
        "Divider top resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    r_bottom = builder.add_component(
        "R",
        "Device",
        "R",
        bottom_value,
        "Resistor_SMD:R_0805_2012Metric",
        "Divider bottom resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    builder.connect(input_net, f"{r_top}.1")
    builder.connect(output_net, f"{r_top}.2", f"{r_bottom}.1")
    builder.connect(gnd_net, f"{r_bottom}.2")
    return {"top": r_top, "bottom": r_bottom}


def add_rc_lowpass(
    builder: CircuitBuilder,
    input_net: str = "VIN",
    output_net: str = "FILTER_OUT",
    gnd_net: str = "GND",
    resistor_value: str = "1k",
    cap_value: str = "100nF",
) -> Dict[str, str]:
    resistor = builder.add_component(
        "R",
        "Device",
        "R",
        resistor_value,
        "Resistor_SMD:R_0805_2012Metric",
        "Low-pass series resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    capacitor = builder.add_component(
        "C",
        "Device",
        "C",
        cap_value,
        "Capacitor_SMD:C_0402_1005Metric",
        "Low-pass shunt capacitor",
        _pins(("1", output_net), ("2", gnd_net)),
    )
    builder.connect(input_net, f"{resistor}.1")
    builder.connect(output_net, f"{resistor}.2", f"{capacitor}.1")
    builder.connect(gnd_net, f"{capacitor}.2")
    return {"resistor": resistor, "capacitor": capacitor}


def add_mosfet_low_side_switch(
    builder: CircuitBuilder,
    control_net: str = "CTRL",
    supply_net: str = "VLOAD",
    switched_net: str = "LOAD_RETURN",
    gnd_net: str = "GND",
) -> Dict[str, str]:
    q_ref = builder.add_component(
        "Q",
        "Device",
        "Q_NMOS_DGS",
        "AO3400A",
        "Package_TO_SOT_SMD:SOT-23",
        "Low-side MOSFET switch",
        _pins(("1", "G"), ("2", "S"), ("3", "D")),
    )
    gate_res = builder.add_component(
        "R",
        "Device",
        "R",
        "100",
        "Resistor_SMD:R_0805_2012Metric",
        "Gate resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    pull_down = builder.add_component(
        "R",
        "Device",
        "R",
        "100k",
        "Resistor_SMD:R_0805_2012Metric",
        "Gate pull-down resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    load_header = builder.add_component(
        "J",
        "Connector_Generic",
        "Conn_01x02",
        "Load",
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        "Switched load connector",
        _pins(("1", supply_net), ("2", switched_net)),
    )
    builder.connect(control_net, f"{gate_res}.1")
    builder.connect("GATE_DRIVE", f"{gate_res}.2", f"{q_ref}.1", f"{pull_down}.1")
    builder.connect(gnd_net, f"{q_ref}.2", f"{pull_down}.2")
    builder.connect(switched_net, f"{q_ref}.3", f"{load_header}.2")
    builder.connect(supply_net, f"{load_header}.1")
    return {"mosfet": q_ref, "gate_resistor": gate_res, "pulldown": pull_down, "load_header": load_header}


def add_linear_regulator(
    builder: CircuitBuilder,
    input_net: str = "VIN",
    output_net: str = "3V3",
    gnd_net: str = "GND",
    output_voltage: str = "3.3V",
) -> Dict[str, str]:
    part = "AMS1117-3.3" if output_voltage.startswith("3.3") else "L7805"
    footprint = "Package_TO_SOT_SMD:SOT-223-3_TabPin2" if part == "AMS1117-3.3" else "Package_TO_SOT_THT:TO-220-3_Vertical"
    reg = builder.add_component(
        "U",
        "Regulator_Linear",
        part,
        output_voltage,
        footprint,
        "Linear regulator stage",
        _pins(("1", "GND"), ("2", "VOUT"), ("3", "VIN")),
    )
    c_in = builder.add_component(
        "C",
        "Device",
        "C",
        "10uF",
        "Capacitor_SMD:C_0805_2012Metric",
        "Input bulk capacitor",
        _pins(("1", input_net), ("2", gnd_net)),
    )
    c_out = builder.add_component(
        "C",
        "Device",
        "C",
        "10uF",
        "Capacitor_SMD:C_0805_2012Metric",
        "Output bulk capacitor",
        _pins(("1", output_net), ("2", gnd_net)),
    )
    builder.connect(input_net, f"{reg}.3", f"{c_in}.1")
    builder.connect(output_net, f"{reg}.2", f"{c_out}.1")
    builder.connect(gnd_net, f"{reg}.1", f"{c_in}.2", f"{c_out}.2")
    add_decoupling_cap(builder, power_net=output_net, gnd_net=gnd_net)
    return {"regulator": reg, "input_cap": c_in, "output_cap": c_out}


def add_opamp_buffer(
    builder: CircuitBuilder,
    input_net: str = "VIN",
    output_net: str = "VOUT",
    supply_net: str = "VCC",
    gnd_net: str = "GND",
) -> Dict[str, str]:
    opamp = builder.add_component(
        "U",
        "Amplifier_Operational",
        "LM358",
        "LM358",
        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        "Unity-gain op-amp buffer",
        _pins(("1", "OUTA"), ("2", "-INA"), ("3", "+INA"), ("4", "GND"), ("8", "VCC")),
    )
    builder.connect(input_net, f"{opamp}.3")
    builder.connect(output_net, f"{opamp}.1", f"{opamp}.2")
    builder.connect(supply_net, f"{opamp}.8")
    builder.connect(gnd_net, f"{opamp}.4")
    add_decoupling_cap(builder, power_net=supply_net, gnd_net=gnd_net)
    return {"opamp": opamp}


def add_comparator_stage(
    builder: CircuitBuilder,
    input_net: str = "SENSE_IN",
    output_net: str = "CMP_OUT",
    supply_net: str = "VCC",
    gnd_net: str = "GND",
) -> Dict[str, str]:
    comparator = builder.add_component(
        "U",
        "Comparator",
        "LM393",
        "LM393",
        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        "Single-threshold comparator stage",
        _pins(("1", "OUT"), ("2", "IN-"), ("3", "IN+"), ("4", "GND"), ("8", "VCC")),
    )
    pullup = builder.add_component(
        "R",
        "Device",
        "R",
        "10k",
        "Resistor_SMD:R_0805_2012Metric",
        "Comparator output pull-up resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    refs = add_voltage_divider(builder, input_net=supply_net, output_net="CMP_REF", gnd_net=gnd_net, top_value="47k", bottom_value="10k")
    builder.connect(input_net, f"{comparator}.3")
    builder.connect("CMP_REF", f"{comparator}.2")
    builder.connect(output_net, f"{comparator}.1", f"{pullup}.2")
    builder.connect(supply_net, f"{comparator}.8", f"{pullup}.1")
    builder.connect(gnd_net, f"{comparator}.4")
    add_decoupling_cap(builder, power_net=supply_net, gnd_net=gnd_net)
    return {"comparator": comparator, "pullup": pullup, **refs}


def add_relay_driver(
    builder: CircuitBuilder,
    control_net: str = "CTRL",
    supply_net: str = "12V",
    gnd_net: str = "GND",
) -> Dict[str, str]:
    relay = builder.add_component(
        "K",
        "Relay",
        "Relay_SPDT",
        "Relay",
        "Relay_THT:Relay_SPDT_Songle_SRD_Series_Form_C",
        "SPDT relay",
        _pins(("1", "COIL_A"), ("2", "COIL_B"), ("3", "COM"), ("4", "NO"), ("5", "NC")),
    )
    mosfet = builder.add_component(
        "Q",
        "Device",
        "Q_NMOS_DGS",
        "AO3400A",
        "Package_TO_SOT_SMD:SOT-23",
        "Relay low-side driver MOSFET",
        _pins(("1", "G"), ("2", "S"), ("3", "D")),
    )
    gate_res = builder.add_component(
        "R",
        "Device",
        "R",
        "100",
        "Resistor_SMD:R_0805_2012Metric",
        "Relay gate resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    pull_down = builder.add_component(
        "R",
        "Device",
        "R",
        "100k",
        "Resistor_SMD:R_0805_2012Metric",
        "Relay gate pull-down resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    flyback = builder.add_component(
        "D",
        "Device",
        "D",
        "1N4148",
        "Diode_SMD:D_SOD-123",
        "Relay flyback diode",
        _pins(("1", "A"), ("2", "K")),
    )
    contact = builder.add_component(
        "J",
        "Connector_Generic",
        "Conn_01x03",
        "Relay contacts",
        "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
        "Relay contact header",
        _pins(("1", "COM"), ("2", "NO"), ("3", "NC")),
    )
    builder.connect(control_net, f"{gate_res}.1")
    builder.connect("RELAY_GATE", f"{gate_res}.2", f"{mosfet}.1", f"{pull_down}.1")
    builder.connect(gnd_net, f"{mosfet}.2", f"{pull_down}.2")
    builder.connect(supply_net, f"{relay}.1", f"{flyback}.2")
    builder.connect("RELAY_COIL_RETURN", f"{relay}.2", f"{mosfet}.3", f"{flyback}.1")
    builder.connect("COM", f"{relay}.3", f"{contact}.1")
    builder.connect("NO", f"{relay}.4", f"{contact}.2")
    builder.connect("NC", f"{relay}.5", f"{contact}.3")
    return {
        "relay": relay,
        "mosfet": mosfet,
        "gate_resistor": gate_res,
        "pulldown": pull_down,
        "flyback": flyback,
        "contact_header": contact,
    }


def add_555_timer(
    builder: CircuitBuilder,
    supply_net: str = "VCC",
    gnd_net: str = "GND",
    output_net: str = "OUT",
) -> Dict[str, str]:
    timer = builder.add_component(
        "U",
        "Timer",
        "NE555",
        "NE555",
        "Package_DIP:DIP-8_W7.62mm",
        "555 timer in astable configuration",
        _pins(("1", "GND"), ("2", "TRIG"), ("3", "OUT"), ("4", "RESET"), ("5", "CTRL"), ("6", "THR"), ("7", "DIS"), ("8", "VCC")),
    )
    r_a = builder.add_component(
        "R",
        "Device",
        "R",
        "10k",
        "Resistor_SMD:R_0805_2012Metric",
        "Timing resistor A",
        _pins(("1", "1"), ("2", "2")),
    )
    r_b = builder.add_component(
        "R",
        "Device",
        "R",
        "100k",
        "Resistor_SMD:R_0805_2012Metric",
        "Timing resistor B",
        _pins(("1", "1"), ("2", "2")),
    )
    c_t = builder.add_component(
        "C",
        "Device",
        "C",
        "10uF",
        "Capacitor_SMD:C_0805_2012Metric",
        "Timing capacitor",
        _pins(("1", "1"), ("2", "2")),
    )
    c_ctrl = builder.add_component(
        "C",
        "Device",
        "C",
        "10nF",
        "Capacitor_SMD:C_0402_1005Metric",
        "Control voltage bypass capacitor",
        _pins(("1", "1"), ("2", "2")),
    )
    builder.connect(supply_net, f"{timer}.8", f"{timer}.4", f"{r_a}.1")
    builder.connect("RA_RB", f"{r_a}.2", f"{r_b}.1", f"{timer}.7")
    builder.connect("TIMING", f"{r_b}.2", f"{timer}.2", f"{timer}.6", f"{c_t}.1")
    builder.connect(output_net, f"{timer}.3")
    builder.connect("CTRL", f"{timer}.5", f"{c_ctrl}.1")
    builder.connect(gnd_net, f"{timer}.1", f"{c_t}.2", f"{c_ctrl}.2")
    add_decoupling_cap(builder, power_net=supply_net, gnd_net=gnd_net)
    return {"timer": timer}


def add_minimal_mcu(
    builder: CircuitBuilder,
    supply_net: str = "VCC",
    gnd_net: str = "GND",
    io_net: str = "GPIO_OUT",
    sensor_net: str = "SENSE_IN",
) -> Dict[str, str]:
    mcu = builder.add_component(
        "U",
        "MCU_Microchip_ATmega",
        "ATmega328P-AU",
        "ATmega328P-AU",
        "Package_QFP:TQFP-32_7x7mm_P0.8mm",
        "Minimal microcontroller core",
        _pins(("4", "VCC"), ("6", "GND"), ("18", "AVCC"), ("21", "AREF"), ("29", "PC6_RESET"), ("15", "PB1"), ("23", "PC0"), ("3", "GND")),
    )
    reset_pullup = builder.add_component(
        "R",
        "Device",
        "R",
        "10k",
        "Resistor_SMD:R_0805_2012Metric",
        "Reset pull-up resistor",
        _pins(("1", "1"), ("2", "2")),
    )
    io_header = builder.add_component(
        "J",
        "Connector_Generic",
        "Conn_01x04",
        "IO",
        "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
        "MCU IO header",
        _pins(("1", supply_net), ("2", gnd_net), ("3", io_net), ("4", sensor_net)),
    )
    builder.connect(supply_net, f"{mcu}.4", f"{mcu}.18", f"{reset_pullup}.1", f"{io_header}.1")
    builder.connect(gnd_net, f"{mcu}.3", f"{mcu}.6", f"{io_header}.2")
    builder.connect("RESET", f"{mcu}.29", f"{reset_pullup}.2")
    builder.connect(io_net, f"{mcu}.15", f"{io_header}.3")
    builder.connect(sensor_net, f"{mcu}.23", f"{io_header}.4")
    builder.connect("AREF", f"{mcu}.21")
    add_decoupling_cap(builder, power_net=supply_net, gnd_net=gnd_net)
    return {"mcu": mcu, "reset_pullup": reset_pullup, "io_header": io_header}
