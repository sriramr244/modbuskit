# ModbusKit
# DISCLAIMER: UTILIZED AI TO MAKE MULTITHREADED SYSTEM, TO CREATE SIM, TO WRITE README

ModbusKit is a Python tool for reading and writing Modbus devices. The Streamlit interface lets you poll registers, plot values, and scan for devices.

It supports serial RTU, RTU over TCP (including Lantronix and Moxa raw-socket gateways), and Modbus TCP/IP. Writes are disabled by default. To write a value, enable writes in the sidebar and confirm the operation on the Write tab.

## Install and run

```bash
pip install -r requirements.txt
streamlit run app.py
```

### Try it without hardware

Start the simulator in a second terminal:

```bash
python -m modbuskit.sim --tcp 5020                    # Modbus TCP
```

In the sidebar, select **Modbus TCP/IP**, set the host to `127.0.0.1` and the port to `5020`, then click **Connect**. Select `simulator_demo.yaml` under **Device profile** and click **Load profile tags**. On the Poll tab, click **Start polling**, then open Trend to plot the values.

To try RTU over TCP instead, run:

```bash
python -m modbuskit.sim --tcp 5021 --framing rtu      # RTU over TCP
```

Select **Modbus RTU over TCP** in the sidebar and connect to `127.0.0.1:5021`.

## Read registers without a profile

1. In the sidebar, choose a transport, enter the serial port or IP address, set the default unit ID, and click **Connect**.
2. On the Poll tab, expand **Read definition**. Set the unit ID, function (01/02/03/04), start address, quantity, data type, and word order. Click **Replace tag list**.
3. Click **Read once** for a single read or **Start polling** for repeated reads. Use **Append** to add more address ranges or unit IDs.
4. On the Tags tab, rename tags and set their scale, offset, units, or value labels (`0=Off; 1=On`). Click **Export as profile YAML** to save the configuration.

Addresses start at zero: FC3 address `0` corresponds to holding register `40001`. The Ref column shows the PLC-style reference.

## Tabs

| Tab | What it does |
| --- | --- |
| Poll | Shows tag values, raw data, status, sample age, communication counters, round-trip times, and the read plan. Can log readings to CSV in `logs/`. |
| Trend | Plots numeric tags together or separately, shows minimum, maximum, mean, and standard deviation, and exports history to CSV. |
| Tags | Edits tag addresses, unit IDs, function codes, data types, word order, scaling, and value labels. Supports signed and unsigned 16/32/64-bit integers, float32/64, hex, binary, and strings. |
| Scan | Tests serial settings and unit IDs, searches for readable register ranges, reads device identification (FC43/14), and scans an IP subnet for port 502. |
| Write | Sends FC05/06/15/16 requests, encodes values using the selected type and word order, and optionally reads them back. For example, a float32 uses two registers. |
| Traffic | Shows transmitted and received frames in hex. |

## Response statuses

- `TIMEOUT`: No reply arrived before the timeout. Check the connection, serial settings, unit ID, and wiring.
- `GARBLED`: A reply arrived but could not be validated, for example because of a bad CRC. Check serial settings and wiring.
- `EXCEPTION`: The device returned a Modbus exception, such as an unsupported function or an invalid address.
- `OTHER_UNIT`: The reply came from a different unit ID.

## Project layout

Transport, framing, polling, and the UI live in separate modules:

| File | Responsibility |
| --- | --- |
| `modbuskit/transport.py` | Serial ports and TCP sockets. |
| `modbuskit/client.py` | RTU and TCP request handling, including CRC checks, timing, echo removal, and transaction IDs. |
| `modbuskit/framing.py` | Request and response PDUs for FC01–06, 15, 16, and 43, including exceptions. |
| `modbuskit/datatypes.py` | Conversion between registers and values, with ABCD/CDAB/BADC/DCBA ordering. |
| `modbuskit/tags.py` | Tag definitions and grouping nearby tags into read blocks. |
| `modbuskit/poller.py` | Background polling, latest values, history, statistics, and CSV logging. |
| `modbuskit/scanner.py` | Serial settings, unit ID, register map, and IP scans. |
| `modbuskit/profiles.py` | Loading and exporting YAML device profiles. |
| `app.py` / `modbuskit/cli.py` | Streamlit and command-line interfaces. |

Serial RTU uses the serial transport with RTU framing. RTU over TCP uses the TCP transport with RTU framing. Modbus TCP uses the TCP transport with TCP framing.

The read planner groups tags by unit ID and function code, then combines nearby addresses according to **Block merge gap**. If a combined read fails with an illegal address or illegal value exception, the poller retries each tag separately.

## Device profiles

Files in `profiles/` store register maps, value labels, the default unit ID, scan probe addresses, and suggested serial settings. You can load a profile in the sidebar or create one by editing tags and exporting them as YAML.

## Command line

Read ten holding registers starting at address zero:

```bash
python -m modbuskit.cli read --tcp 10.0.0.50 --unit 1 --fc 3 --addr 0 --count 10
```

## Connection notes

- Polling, scans, and writes share the active connection. A lock serializes their requests.
- Disconnect before running a serial settings sweep; the sweep opens the port itself.
- Reconnecting to the same port or TCP endpoint closes the connection held by the previous session.

## Planned work

- Additional logging options beyond the existing CSV polling log.
- An OPC UA service that exposes Modbus data.
- Alarms and notifications, including email alerts.
- Fault reporting.
