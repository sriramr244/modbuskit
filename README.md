# ModbusKit

A generic Modbus master you can point at any device, like Modbus Poll, with trending, scanning, and a Streamlit UI.
It supports **Modbus RTU (serial)**, **Modbus RTU over TCP** (Lantronix/Moxa raw-socket ports), and **Modbus TCP/IP**.
It's read-only by default; writes need a sidebar toggle plus a confirm checkbox.

## Install and run

```
pip install -r requirements.txt
streamlit run app.py
```

No hardware? Start the bench simulator in a second terminal, then connect to `127.0.0.1:5020`:

```
python -m modbuskit.sim --tcp 5020                    # Modbus TCP
python -m modbuskit.sim --tcp 5021 --framing rtu      # RTU over TCP
```

Load the `simulator_demo.yaml` profile, then Poll → Start, then Trend.

## Reading from any device (no profile needed)

1. Sidebar: pick a transport, enter the port or IP, set the default unit ID, and click Connect.
2. Poll tab → **Read definition**: unit, function (01/02/03/04), start address, quantity, data type, and word order. Click **Replace tag list**.
3. Click **Read once**, or **Start polling**. Use **Append** to stack more windows, including other unit IDs.
4. Tags tab: rename rows and add scale, offset, units, or enums (`0=Off; 1=On`). **Export as profile YAML** saves the map for reuse.

Addresses are **0-based protocol addresses**: FC3 address 0 = 40001. The Ref column shows the PLC-style reference.

## Tabs

| Tab | What it does |
|---|---|
| Poll | Live table of every tag (value, raw, status, age), comm counters, RTT, read plan. Optional CSV log to `logs/`. |
| Trend | Live line chart of any numeric tags (overlay or separate), min/max/mean/std, CSV export. |
| Tags | Editable tag list: unit, FC, address, type (u/int16/32/64, float32/64, hex, binary, string), word order, scale/offset, enum. |
| Scan | **Serial settings sweep** (baud × parity × stop bits × unit), **Unit ID scan**, **Register map** discovery (bisects refused chunks), **Device ID** (FC43/14), **IP subnet scan** for port 502. |
| Write | FC05/06/15/16 with typed encoding (e.g. a float32 in CDAB → 2 registers) and read-back. |
| Traffic | Raw TX/RX hex frames, like Modbus Poll's Communication window. |

Status meanings (same idea as your original sweep script):
`TIMEOUT` = silence (wrong baud, unit ID, or wiring) · `GARBLED` = bytes came back with a bad CRC (baud probably right, check parity or polarity) ·
`EXCEPTION` = the device is there but refused the request · `OTHER_UNIT` = a different unit ID answered.

## How it is structured (and why)

The design idea: **each layer knows one thing**, so you can swap any of them without touching the others.

```
transport.py   moves bytes            SerialTransport | TcpTransport
client.py      Modbus framing         RTU (CRC, silence gaps, echo strip) | TCP (MBAP, transaction IDs)
framing.py     PDUs                   build/parse FC01-06, 15, 16, 43; exceptions
datatypes.py   registers <-> values   int/float/string, ABCD/CDAB/BADC/DCBA
tags.py        named points           Tag + block planner (merges neighbours into few reads)
poller.py      background loop        latest values, history ring buffer, stats, CSV
scanner.py     discovery              serial sweep, unit IDs, register map, IP scan
profiles.py    device knowledge       YAML only; the core never hard-codes a device
app.py / cli.py                       front-ends: UI wiring only
```

The three link types are just **transport × framing** combinations:
RTU serial = Serial + RTU, RTU over TCP = TCP + RTU, Modbus TCP = TCP + TCP.
That's why adding RTU over TCP cost zero new protocol code.

The block planner groups tags by (unit, FC), sorts them by address, and merges two tags when the gap between them is ≤ "Block merge gap".
If one bad address makes a merged block return "illegal address", that block automatically splits into per-tag reads.

## Device profiles

`profiles/*.yaml` hold only device knowledge: the register map, enums, the default unit ID, the register to probe during scans, and likely serial settings.
`ge_multilin_rrtd.yaml` carries the comms setpoints from your RRTD script, with addresses from manual 1601-0104-B7.
It doesn't include the RTD temperature registers yet. Add those from Table 8-1.

## CLI (same core, no browser)

```
python -m modbuskit.cli sweep --port COM5 --profile profiles/ge_multilin_rrtd.yaml
python -m modbuskit.cli read --tcp 10.0.0.50 --unit 1 --fc 3 --addr 0 --count 10
python -m modbuskit.cli read --rtu-tcp 10.0.0.60:10001 --unit 254 --profile profiles/ge_multilin_rrtd.yaml
```

## Notes

- One connection is open at a time. Polling, scans, and writes share it under a lock, so they never collide on the bus.
- The serial sweep opens the port itself, so disconnect first.
- Refreshing the browser tab releases a COM port held by the old session.

## Future Implementation
- Logging 
- Adding a OPC UA implementation layer and converting this into a service to convert modbus into OPC UA data. 
- Adding message based alerts and adding alarms creating an email service 
- Adding error faults 
