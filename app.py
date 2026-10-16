"""
ModbusKit - Streamlit front-end (Modbus-Poll-style master with trending and scanning).

    streamlit run app.py

The UI holds no Modbus logic; it only wires widgets to modbuskit/*.
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
import json

import pandas as pd
import streamlit as st

from modbuskit import datatypes as dtypes
from modbuskit import framing as fr
from modbuskit import make_client, scanner
from modbuskit.client import Status
from modbuskit.poller import Poller, tag_signature
from modbuskit.profiles import load_profile, list_profiles, profile_to_yaml
from modbuskit.scanner import COMMON_BAUDS
from modbuskit.tags import Tag, tags_from_range
from modbuskit.transport import list_serial_ports
from modbuskit.settings import SETTINGS_PATH, load_settings, save_settings, delete_settings, tag_to_dicts, dicts_to_tag

APP_DIR = Path(__file__).parent
PROFILE_DIR = APP_DIR / "profiles"
LOG_DIR = APP_DIR / "logs"

st.set_page_config(page_title="ModbusKit", layout="wide")
ss = st.session_state

MODES = {"Modbus RTU (serial)": "rtu", "Modbus RTU over TCP": "rtu_tcp", "Modbus TCP/IP": "tcp"}
ALL_BAUDS = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
DTYPE_LIST = list(dtypes.DTYPES)
STATUS_COLORS = {"OK": "#1a7f37", "EXCEPTION": "#bf8700", "TIMEOUT": "#cf222e", "GARBLED": "#cf222e",
                 "OTHER_UNIT": "#8250df", "ERROR": "#cf222e"}

DEFAULTS = {
    "client": None, "poller": None, "tags": [], "tags_base": None, "tag_ver": 0, "profile": None,
    "conn_error": "", "scan": {},
    "mode": "Modbus TCP/IP", "port_sel": None, "port_manual": "COM5", "baud": 9600, "parity": "N",
    "bytesize": 8, "stopbits": 1, "strip_echo": False, "host": "192.168.1.100", "tcp_port": 502,
    "timeout": 1.0, "retries": 0, "unit": 1, "allow_writes": False,
    "interval": 1.0, "max_gap": 10, "csv_log": False, "refresh": 1.0,
    "trend_win": "5 min", "trend_layout": "Overlay",
}

keys_to_persist =["mode", "port_sel", "port_manual", "baud", "parity", "bytesize", "stopbits", "strip_echo",
                "host", "tcp_port", "timeout", "retries", "unit", "interval", "max_gap", "csv_log", "refresh",
                "profile_name", "trend_sel", "trend_win", "trend_layout"]

VALID_CHOICES = {"mode": list(MODES), "baud": ALL_BAUDS, "parity": ["N", "E", "O"], "bytesize": [7, 8],
                 "stopbits": [1, 2], "trend_win": ["1 min", "5 min", "15 min", "1 h", "All"],
                 "trend_layout": ["Overlay", "Separate"]}

if "_cfg" not in ss:
    ss._cfg = load_settings()
    ss._cfg_saved = json.dumps(ss._cfg, sort_keys=True)
    saved = ss._cfg
    ss.tags = dicts_to_tag(saved.get("tags"))
    if saved.get("profile_path") and Path(saved["profile_path"]).is_file():
        try:
            ss.profile = load_profile(saved["profile_path"])
        except Exception:
            pass

#Documented streamlit work
for k in keys_to_persist:
    if k in ss:
        ss[k] = ss[k]

for k in keys_to_persist:
    if k not in ss and k in ss._cfg:
        if k in VALID_CHOICES and ss._cfg[k] not in VALID_CHOICES[k]:
            continue
        ss[k] = ss._cfg[k]

for k, v in DEFAULTS.items():
    ss.setdefault(k, v)
# ======================================================================
# connection helpers
# ======================================================================

@st.cache_resource
def _registry() -> dict:
    """Process-wide: a refreshed browser tab must not leave a COM port locked by its old session."""
    return {}


def _serial_port() -> str:
    return ss.port_manual if ss.port_sel in (None, "Other...") else ss.port_sel


def _conn_key() -> str:
    return f"serial:{_serial_port()}" if MODES[ss.mode] == "rtu" else f"tcp:{ss.host}:{ss.tcp_port}"


def stop_poller():
    if ss.poller is not None:
        ss.poller.stop()


def connect():
    ss.conn_error = ""
    reg, key = _registry(), _conn_key()
    old = reg.pop(key, None)
    if old:
        try:
            if old.get("poller"):
                old["poller"].stop()
            old["client"].close()
        except Exception:
            pass
    mode = MODES[ss.mode]
    try:
        c = make_client(mode, port=_serial_port(), baudrate=ss.baud, parity=ss.parity, bytesize=ss.bytesize,
                        stopbits=ss.stopbits, strip_echo=ss.strip_echo, host=ss.host, tcp_port=ss.tcp_port,
                        timeout=ss.timeout, retries=ss.retries)
        c.connect()
    except Exception as e:
        ss.conn_error = f"{type(e).__name__}: {e}"
        return
    ss.client = c
    reg[key] = {"client": c, "poller": None}


def disconnect():
    stop_poller()
    if ss.client:
        ss.client.close()
    _registry().pop(_conn_key(), None)
    ss.client = None
    ss.poller = None


def set_tags(tags: list[Tag]):
    ss.tags = tags
    ss.tags_base = tags_to_df(tags)
    ss.tag_ver += 1


def load_profile_cb():
    try:
        if ss.get("profile_upload") is not None:
            prof = load_profile(text=ss.profile_upload.getvalue().decode())
        elif ss.get("profile_name") not in (None, "(none)"):
            prof = load_profile(PROFILE_DIR / ss.profile_name)
        else:
            return
    except Exception as e:
        ss.conn_error = f"Profile error: {e}"
        return
    ss.profile = prof
    if "unit_id" in prof.defaults:
        ss.unit = int(prof.defaults["unit_id"])
    set_tags(prof.tags)


def need_client() -> bool:
    if ss.client is None:
        st.info("Connect first (sidebar).")
        return False
    return True


def progress_cb(bar, label):
    def cb(done, total, msg):
        bar.progress(min(done / max(total, 1), 1.0), text=f"{label}: {msg}")
    return cb


# ======================================================================
# tag table <-> DataFrame
# ======================================================================

def _enum_to_text(e):
    return "; ".join(f"{k}={v}" for k, v in e.items()) if e else ""


def _text_to_enum(s):
    if not isinstance(s, str) or not s.strip():
        return None
    out = {}
    for part in s.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[int(k.strip(), 0)] = v.strip()
    return out or None


TAG_COLS = ["name", "unit_id", "fc", "address", "dtype", "order", "scale", "offset", "units", "length",
            "enum", "description"]


def tags_to_df(tags: list[Tag]) -> pd.DataFrame:
    rows = [{"name": t.name, "unit_id": t.unit_id, "fc": t.fc, "address": t.address, "dtype": t.dtype,
             "order": t.order, "scale": t.scale, "offset": t.offset, "units": t.units, "length": t.length,
             "enum": _enum_to_text(t.enum), "description": t.description} for t in tags]
    df = pd.DataFrame(rows, columns=TAG_COLS)
    return df.astype({"unit_id": "Int64", "fc": "Int64", "address": "Int64", "length": "Int64",
                      "scale": "float", "offset": "float"})


def df_to_tags(df: pd.DataFrame) -> tuple[list[Tag], list[str]]:
    tags, errors, seen = [], [], set()
    for i, r in df.iterrows():
        name = r.get("name")
        if not isinstance(name, str) or not name.strip() or pd.isna(r.get("address")):
            continue
        name = name.strip()
        if name in seen:
            errors.append(f"row {i}: duplicate name '{name}'")
            continue
        try:
            fc = int(r["fc"]) if not pd.isna(r["fc"]) else 3
            if fc not in fr.READ_FCS:
                raise ValueError("fc must be 1-4")
            t = Tag(name=name, address=int(r["address"]), fc=fc,
                    dtype=r["dtype"] if r["dtype"] in dtypes.DTYPES else "uint16",
                    order=r["order"] if r["order"] in dtypes.WORD_ORDERS else "ABCD",
                    scale=1.0 if pd.isna(r["scale"]) else float(r["scale"]),
                    offset=0.0 if pd.isna(r["offset"]) else float(r["offset"]),
                    units="" if pd.isna(r["units"]) else str(r["units"]),
                    length=1 if pd.isna(r["length"]) else int(r["length"]),
                    unit_id=None if pd.isna(r["unit_id"]) else int(r["unit_id"]),
                    enum=_text_to_enum(r["enum"]),
                    description="" if pd.isna(r["description"]) else str(r["description"]))
            if not 0 <= t.address <= 65535:
                raise ValueError("address must be 0..65535")
        except Exception as e:
            errors.append(f"row {i} ({name}): {e}")
            continue
        seen.add(name)
        tags.append(t)
    return tags, errors


def read_definition(k: str, expanded: bool):
    """Modbus-Poll-style read definition: any unit / function / address / quantity / type."""
    with st.expander("Read definition - any device: unit / function / address / quantity / type", expanded=expanded):
        c = st.columns([1, 2, 1, 1, 1, 1, 1])
        unit = c[0].number_input("Unit ID (0 = default)", 0, 255, 0, key=f"{k}_unit")
        fc = c[1].selectbox("Function", fr.READ_FCS, format_func=lambda x: fr.FC_NAMES[x], index=2, key=f"{k}_fc")
        start = c[2].number_input("Start address (0-based)", 0, 65535, 0, key=f"{k}_start")
        qty = c[3].number_input("Quantity (values)", 1, 500, 10, key=f"{k}_qty")
        dtype = c[4].selectbox("Type", [d for d in DTYPE_LIST if d != "string"], key=f"{k}_dtype",
                               disabled=fc in fr.BIT_FCS)
        order = c[5].selectbox("Word order", dtypes.WORD_ORDERS, key=f"{k}_order",
                               disabled=fc in fr.BIT_FCS or dtypes.width(dtype) == 1)
        c[6].markdown(f"<br>= **{Tag('x', int(start), fc).ref}**", unsafe_allow_html=True)
        st.caption("Tip: a manual that lists 40001-style refs -> start = ref - 40001 (FC3). "
                   "Hex addresses like 1010h -> 4112. Use 'Append' to combine several windows / devices.")
        b1, b2, _ = st.columns([1, 1, 4])
        new = lambda: tags_from_range(fc, int(start), int(qty), dtype, order, int(unit) or None)
        if b1.button("Replace tag list", key=f"{k}_replace", type="primary"):
            set_tags(new())
            st.rerun()
        if b2.button("Append", key=f"{k}_append"):
            existing = {t.name for t in ss.tags}
            set_tags(ss.tags + [t for t in new() if t.name not in existing])
            st.rerun()


# ======================================================================
# sidebar
# ======================================================================

with st.sidebar:
    st.title("ModbusKit")
    connected = ss.client is not None
    st.selectbox("Transport", list(MODES), key="mode", disabled=connected)
    mode = MODES[ss.mode]

    if mode == "rtu":
        ports = list_serial_ports()
        opts = [p for p, _ in ports] + ["Other..."]
        desc = dict(ports)
        if ss.port_sel not in opts:
            ss.port_sel = opts[0]
        st.selectbox("Serial port", opts, key="port_sel", disabled=connected,
                     format_func=lambda p: f"{p} - {desc[p]}" if desc.get(p) else p)
        if ss.port_sel == "Other...":
            st.text_input("Port name", key="port_manual", disabled=connected)
        a, b = st.columns(2)
        a.selectbox("Baud", ALL_BAUDS, key="baud", disabled=connected)
        b.selectbox("Parity", ["N", "E", "O"], key="parity", disabled=connected)
        a.selectbox("Data bits", [8, 7], key="bytesize", disabled=connected)
        b.selectbox("Stop bits", [1, 2], key="stopbits", disabled=connected)
        st.checkbox("Strip local echo", key="strip_echo", disabled=connected,
                    help="Tick if your RS-485 converter echoes transmitted bytes back.")
    else:
        st.text_input("Host / IP", key="host", disabled=connected)
        st.number_input("TCP port", key="tcp_port", min_value=1, max_value=65535, disabled=connected,
                        help="502 for Modbus TCP. Serial servers in raw/tunnel mode (e.g. Lantronix) "
                             "often use 10001 for RTU over TCP.")

    a, b = st.columns(2)
    a.number_input("Timeout (s)", key="timeout", min_value=0.05, max_value=10.0, step=0.05, format="%.2f")
    b.number_input("Retries", key="retries", min_value=0, max_value=5)
    st.number_input("Default unit ID", key="unit", min_value=0, max_value=255,
                    help="Used by tags that do not set their own unit_id. TCP devices often use 1 or 255.")
    if ss.client:  # live-editable on an open connection
        ss.client.timeout, ss.client.retries = float(ss.timeout), int(ss.retries)

    if connected:
        st.button("Disconnect", on_click=disconnect, width="stretch")
        st.success(f"Connected: {ss.client.describe()}")
    else:
        st.button("Connect", on_click=connect, type="primary", width="stretch")
    if ss.conn_error:
        st.error(ss.conn_error)

    st.divider()
    st.subheader("Device profile")
    st.selectbox("Profile", ["(none)"] + [p.name for p in list_profiles(PROFILE_DIR)], key="profile_name")
    st.file_uploader("...or upload YAML", type=["yaml", "yml"], key="profile_upload")
    st.button("Load profile tags", on_click=load_profile_cb, width="stretch")
    if ss.profile:
        st.caption(f"Loaded: **{ss.profile.name}** ({len(ss.profile.tags)} tags)"
                   + (f"  \nSource: {ss.profile.source}" if ss.profile.source else ""))

    st.divider()
    def reset_settings():
        delete_settings()
        for k in keys_to_persist + ["_cfg", "_cfg_saved", "tags", "tags_base", "profile"]:
            ss.pop(k, None)
        ss.tag_ver = ss.get("tag_ver", 0) + 1
    st.button("Reset all settings", on_click=reset_settings, type="secondary", width="stretch",
              help=f"Settings and tags are remembered in {SETTINGS_PATH}")
    st.toggle("Enable writes", key="allow_writes",
              help="Off = the tool is strictly read-only. Turn on only when you mean to change a live device.")


# ======================================================================
# tabs (code order != display order: Tags runs first so ss.tags is current)
# ======================================================================

tab_poll, tab_trend, tab_tags, tab_scan, tab_write, tab_traffic, tab_notifications = st.tabs(
    ["Poll", "Trend", "Tags", "Scan", "Write", "Traffic", "Notifications"])


# ---------------------------------------------------------------- Tags
with tab_tags:
    st.subheader("Tag list")
    st.caption("Addresses are 0-based protocol addresses (FC3 address 0 = 40001). "
               "Leave unit_id blank to use the default. Enum format: `0=Off; 1=On`.")
    read_definition("tg", expanded=not ss.tags)

    if ss.tags_base is None:
        ss.tags_base = tags_to_df(ss.tags)
    edited = st.data_editor(
        ss.tags_base, key=f"tag_editor_{ss.tag_ver}", num_rows="dynamic", width="stretch", hide_index=True,
        column_config={
            "fc": st.column_config.SelectboxColumn("fc", options=list(fr.READ_FCS), default=3),
            "dtype": st.column_config.SelectboxColumn("dtype", options=DTYPE_LIST, default="uint16"),
            "order": st.column_config.SelectboxColumn("order", options=list(dtypes.WORD_ORDERS), default="ABCD"),
            "address": st.column_config.NumberColumn("address", min_value=0, max_value=65535, step=1),
            "unit_id": st.column_config.NumberColumn("unit_id", min_value=0, max_value=255, step=1),
            "length": st.column_config.NumberColumn("length", min_value=1, max_value=120, step=1,
                                                    help="registers, for dtype=string"),
        })
    ss.tags, tag_errors = df_to_tags(edited)
    for e in tag_errors:
        st.warning(e)
    c1, c2, c3 = st.columns([1, 1, 4])
    c1.download_button("Export as profile YAML", profile_to_yaml("My device", ss.tags),
                       file_name="device_profile.yaml", mime="text/yaml")
    if c2.button("Clear all tags"):
        set_tags([])
        st.rerun()

# ---------------------------------------------------------------- Poll
with tab_poll:
    if need_client():
        read_definition("pd", expanded=not ss.tags)
        c = st.columns([1, 1, 1, 1, 1, 1])
        c[0].number_input("Scan rate (s)", min_value=0.05, max_value=3600.0, step=0.1, key="interval")
        c[1].number_input("Block merge gap", min_value=0, max_value=100, key="max_gap",
                          help="Merge tags into one read when the address hole between them is <= this. "
                               "0 = only contiguous. Blocks that hit 'illegal address' auto-split.")
        c[2].number_input("UI refresh (s)", min_value=0.25, max_value=10.0, step=0.25, key="refresh")
        c[3].checkbox("Log to CSV", key="csv_log", help=f"Writes to {LOG_DIR}")
        poller = ss.poller
        running = poller is not None and poller.running

        def start_poll(keep_history: bool = False):
            old = ss.poller
            stop_poller()
            csv_path = None
            if ss.csv_log:
                LOG_DIR.mkdir(exist_ok=True)
                csv_path = str(LOG_DIR / f"poll_{dt.datetime.now():%Y%m%d_%H%M%S}.csv")
            p = Poller(ss.client, ss.tags, int(ss.unit), ss.interval, csv_path=csv_path, max_gap=int(ss.max_gap))
            if keep_history and old is not None:
                p.history.extend(old.history)
            p.start()
            ss.poller = p
            _registry().setdefault(_conn_key(), {"client": ss.client})["poller"] = p

        def read_once():
            stop_poller()
            p = Poller(ss.client, ss.tags, int(ss.unit), max_gap=int(ss.max_gap))
            p.cycle()
            ss.poller = p

        if running:
            c[4].button("Stop", on_click=stop_poller, type="primary", width="stretch")
        else:
            c[4].button("Start polling", on_click=start_poll, type="primary", width="stretch",
                        disabled=not ss.tags)
        c[5].button("Read once", on_click=read_once, width="stretch", disabled=not ss.tags or running)

        if not ss.tags:
            st.info("No tags yet - generate a register window or load a profile on the Tags tab.")
        if running and poller.signature != tag_signature(ss.tags, int(ss.unit)):
            if tag_errors:
                st.warning("Tag list has errors (see tags tab)- fix them before polling.")
            else:
                start_poll(keep_history=True)

        @st.fragment(run_every=ss.refresh if running else None)
        def live_table():
            p = ss.poller
            if p is None:
                return
            latest, s = p.snapshot()
            m = st.columns(7)
            m[0].metric("Cycles", s["cycles"])
            m[1].metric("Requests", s["requests"])
            m[2].metric("OK", s["ok"])
            m[3].metric("Timeouts", s["timeouts"])
            m[4].metric("Exceptions", s["exceptions"])
            m[5].metric("Garbled / err", s["garbled"] + s["errors"])
            m[6].metric("Last RTT", f"{s['last_rtt_ms']:.1f} ms")
            if s["last_error"]:
                st.caption(f"Last error: {s['last_error']}")
            now = time.time()
            rows = []
            for t in p.tags:
                v = latest[t.name]
                rows.append({"name": t.name, "unit": t.unit_id if t.unit_id is not None else p.default_unit,
                             "ref": t.ref, "addr": t.address, "hex": f"0x{t.address:04X}", "type": t.dtype,
                             "value": v["text"], "units": t.units,
                             "raw": "" if v["raw"] is None else str(v["raw"]), "status": v["status"],
                             "age (s)": None if v["ts"] is None else round(now - v["ts"], 1),
                             "description": t.description})
            df = pd.DataFrame(rows)
            st.dataframe(df.style.map(lambda x: f"color: {STATUS_COLORS.get(x, 'inherit')}; font-weight: 600",
                                      subset=["status"]),
                         hide_index=True, width="stretch", height=min(38 + 35 * len(df), 700))
            with st.expander("Read plan"):
                st.caption(f"{len(p.blocks)} request(s) per cycle, last cycle {s['last_cycle_ms']:.0f} ms")
                st.dataframe(pd.DataFrame([{"unit": b.unit_id, "fc": b.fc, "start": b.start, "count": b.count,
                                            "tags": ", ".join(t.name for t in b.tags)} for b in p.blocks]),
                             hide_index=True, width="stretch")

        live_table()

# ---------------------------------------------------------------- Trend
with tab_trend:
    p = ss.poller
    numeric = [t.name for t in (p.tags if p else ss.tags) if t.dtype != "string"]
    if not p or not p.history:
        st.info("Start polling to collect trend data.")
    else:
        c = st.columns([4, 1, 1])
        ss.setdefault("trend_sel", numeric[:4])
        ss.trend_sel = [n for n in ss.trend_sel if n in numeric]
        c[0].multiselect("Tags", numeric, key="trend_sel")
        window = c[1].selectbox("Window", ["1 min", "5 min", "15 min", "1 h", "All"], key="trend_win")
        layout = c[2].radio("Layout", ["Overlay", "Separate"], key="trend_layout", horizontal=True)
        seconds = {"1 min": 60, "5 min": 300, "15 min": 900, "1 h": 3600, "All": None}[window]

        @st.fragment(run_every=ss.refresh if p.running else None)
        def trend():
            names = ss.trend_sel
            if not names:
                st.caption("Pick at least one tag.")
                return
            rows = ss.poller.history_rows(names, seconds)
            if not rows:
                st.caption("No samples in this window yet.")
                return
            df = pd.DataFrame(rows)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(
                dt.datetime.now().astimezone().tzinfo)
            df = df.set_index("time")
            if layout == "Overlay":
                st.line_chart(df, height=420)
            else:
                for n in names:
                    st.caption(n)
                    st.line_chart(df[[n]], height=180)
            st.dataframe(pd.DataFrame({"last": df.ffill().iloc[-1], "min": df.min(), "max": df.max(),
                                       "mean": df.mean(), "std": df.std(), "samples": df.count(),
                                       "missing": df.isna().sum()}).round(4), width="stretch")

        trend()
        if st.button("Prepare history CSV"):
            all_rows = p.history_rows(numeric)
            out = pd.DataFrame(all_rows)
            out["time"] = pd.to_datetime(out["time"], unit="s")
            st.download_button("Download", out.to_csv(index=False), file_name="trend_history.csv", mime="text/csv")

# ---------------------------------------------------------------- Scan
with tab_scan:
    if ss.poller is not None and ss.poller.running:
        st.warning("Polling is running - scans share the bus and will be slow. Consider stopping it.")
    probe = (ss.profile.probe if ss.profile else {}) or {}
    s_sweep, s_units, s_regs, s_devid, s_ip = st.tabs(
        ["Serial settings sweep", "Unit ID scan", "Register map", "Device ID (FC43)", "IP scan"])

    with s_sweep:
        st.caption("Finds baud / parity / stop bits / unit ID on an RS-485 port. Read-only. "
                   "Uses the port selected in the sidebar and opens it itself.")
        if MODES[ss.mode] != "rtu":
            st.info("Select 'Modbus RTU (serial)' in the sidebar to choose a port.")
        elif ss.client is not None:
            st.info("Disconnect first - the sweep needs the port to itself.")
        else:
            ser = (ss.profile.serial if ss.profile else {}) or {}
            c = st.columns(3)
            bauds = c[0].multiselect("Bauds (in order)", ALL_BAUDS, default=ser.get("bauds", COMMON_BAUDS[:5]))
            parities = c[1].multiselect("Parities", ["N", "E", "O"], default=ser.get("parities", ["N", "E", "O"]))
            stopbits = c[2].multiselect("Stop bits", [1, 2], default=ser.get("stopbits", [1]))
            c = st.columns(4)
            units_txt = c[0].text_input("Unit IDs to try first", " ".join(
                str(u) for u in ([int(ss.unit)] + ([254, 1] if int(ss.unit) not in (254, 1) else []))))
            full = c[1].checkbox("Then every unit 1-247")
            sw_fc = c[2].selectbox("Probe FC", fr.READ_FCS, index=fr.READ_FCS.index(probe.get("fc", 3)), key="sw_fc")
            sw_addr = c[3].number_input("Probe address", 0, 65535, int(probe.get("address", 0)), key="sw_addr")
            c = st.columns(3)
            sw_to = c[0].number_input("Timeout per try (s)", 0.05, 5.0, 0.3, 0.05, key="sw_to")
            sw_first = c[1].checkbox("Stop at first hit", True)
            try:
                units = [int(u) for u in units_txt.split()]
            except ValueError:
                units = []
                st.error("Unit IDs: space-separated integers")
            if full:
                units += [u for u in range(1, 248) if u not in units]
            n = len(bauds) * len(parities) * len(stopbits) * len(units)
            c[2].caption(f"{n} combinations, worst case ~{n * (sw_to + 0.01) / 60:.1f} min")
            if st.button("Run sweep", type="primary", disabled=n == 0):
                bar = st.progress(0.0)
                try:
                    hits, garbled, log = scanner.sweep_serial(
                        _serial_port(), bauds, parities, stopbits, units, int(ser.get("bytesize", 8)),
                        sw_fc, int(sw_addr), 1, sw_to, ss.strip_echo, sw_first, progress_cb(bar, "Sweep"))
                    ss.scan["sweep"] = (hits, garbled, log)
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")
            if "sweep" in ss.scan:
                hits, garbled, log = ss.scan["sweep"]
                if hits:
                    h = hits[0]
                    st.success(f"Found: {h['baud']} {h['bytesize']}{h['parity']}{h['stopbits']}, unit {h['unit']} "
                               f"({h['status']})")

                    def apply_hit(h=h):
                        ss.baud, ss.parity, ss.stopbits, ss.bytesize, ss.unit = (
                            h["baud"], h["parity"], h["stopbits"], h["bytesize"], h["unit"])
                    st.button("Use these settings", on_click=apply_hit)
                else:
                    st.error("No response on any combination.")
                    if garbled:
                        st.warning("Garbled replies at: " + ", ".join(sorted(garbled))
                                   + " -> baud is probably right; recheck parity / A-B polarity.")
                    st.caption("Next: swap A/B, converter in 2-wire RS-485 mode, device powered, "
                               "try every unit ID, check termination/bias.")
                if log:
                    st.dataframe(pd.DataFrame(log), hide_index=True, width="stretch")

    with s_units:
        if need_client():
            c = st.columns(5)
            u0 = c[0].number_input("From", 0, 255, 1, key="us_from")
            u1 = c[1].number_input("To", 0, 255, 247, key="us_to")
            us_fc = c[2].selectbox("Probe FC", fr.READ_FCS, index=fr.READ_FCS.index(probe.get("fc", 3)), key="us_fc")
            us_addr = c[3].number_input("Probe address", 0, 65535, int(probe.get("address", 0)), key="us_addr")
            us_to = c[4].number_input("Timeout (s)", 0.05, 5.0, 0.2, 0.05, key="us_timeout")
            st.caption("An exception reply still counts as 'present'. Gateway exceptions 0A/0B are treated as absent.")
            if st.button("Scan unit IDs", type="primary"):
                bar = st.progress(0.0)
                ss.scan["units"] = scanner.scan_units(ss.client, range(int(u0), int(u1) + 1), us_fc, int(us_addr),
                                                      1, us_to, progress_cb(bar, "Units"))
            if "units" in ss.scan:
                found = ss.scan["units"]
                st.write(f"**{len(found)}** unit(s) responded")
                if found:
                    st.dataframe(pd.DataFrame(found), hide_index=True, width="stretch")

    with s_regs:
        if need_client():
            c = st.columns(6)
            r_unit = c[0].number_input("Unit ID", 0, 255, int(ss.unit), key="rs_unit")
            r_fc = c[1].selectbox("Function", fr.READ_FCS, index=2, format_func=lambda x: fr.FC_NAMES[x], key="rs_fc")
            r_start = c[2].number_input("Start", 0, 65535, 0, key="rs_start")
            r_end = c[3].number_input("End", 0, 65535, 999, key="rs_end")
            r_chunk = c[4].number_input("Chunk", 1, fr.MAX_READ[r_fc], min(100, fr.MAX_READ[r_fc]), key="rs_chunk")
            r_res = c[5].selectbox("Resolution", [1, 4, 8, 16], key="rs_res",
                                   help="1 = exact map. Larger = far fewer requests over big holes, "
                                        "but isolated registers inside a refused span are missed.")
            if st.button("Scan register map", type="primary", disabled=r_end < r_start):
                bar = st.progress(0.0)
                vals, ranges, nreq = scanner.scan_registers(ss.client, int(r_unit), r_fc, int(r_start), int(r_end),
                                                            int(r_chunk), int(r_res), progress_cb(bar, "Map"))
                ss.scan["regs"] = (int(r_unit), r_fc, vals, ranges, nreq)
            if "regs" in ss.scan:
                unit_, fc_, vals, ranges, nreq = ss.scan["regs"]
                st.write(f"**{len(vals)}** readable address(es) in **{len(ranges)}** range(s), {nreq} requests")
                if ranges:
                    st.dataframe(pd.DataFrame([{"first": a, "last": b, "count": b - a + 1,
                                                "first ref": Tag("x", a, fc_).ref, "last ref": Tag("x", b, fc_).ref}
                                               for a, b in ranges]), hide_index=True, width="stretch")
                    with st.expander("Values"):
                        st.dataframe(pd.DataFrame([{"addr": a, "hex addr": f"0x{a:04X}", "ref": Tag("x", a, fc_).ref,
                                                    "value": int(v), "hex": f"0x{int(v):04X}",
                                                    "int16": dtypes.decode([int(v)], "int16")}
                                                   for a, v in sorted(vals.items())]),
                                     hide_index=True, width="stretch")
                    if st.button("Add readable addresses as tags (max 500)"):
                        prefix = {1: "coil", 2: "di", 3: "hr", 4: "ir"}[fc_]
                        existing = {t.name for t in ss.tags}
                        new = [Tag(f"{prefix}_{a}", a, fc_, unit_id=unit_ if unit_ != int(ss.unit) else None)
                               for a in sorted(vals)[:500] if f"{prefix}_{a}" not in existing]
                        set_tags(ss.tags + new)
                        st.rerun()

    with s_devid:
        if need_client():
            c = st.columns(3)
            d_unit = c[0].number_input("Unit ID", 0, 255, int(ss.unit), key="di_unit")
            d_code = c[1].selectbox("Category", [1, 2, 3], format_func={1: "Basic", 2: "Regular", 3: "Extended"}.get)
            if st.button("Read device identification"):
                r = ss.client.read_device_id(int(d_unit), d_code)
                if r.ok:
                    st.table(pd.DataFrame(list(r.data.items()), columns=["object", "value"]))
                else:
                    st.error(f"{r.status.value}: {r.detail}  (many devices simply do not support FC43)")

    with s_ip:
        st.caption("Finds hosts with the Modbus port open, then probes FC03 address 0 and FC43.")
        c = st.columns(5)
        cidr = c[0].text_input("Subnet (CIDR)", "192.168.1.0/24", key="ip_cidr")
        ip_port = c[1].number_input("Port", 1, 65535, 502, key="ip_port")
        ip_to = c[2].number_input("Connect timeout (s)", 0.05, 5.0, 0.3, 0.05, key="ip_to")
        ip_probe = c[3].checkbox("Probe Modbus", True, key="ip_probe")
        ip_unit = c[4].number_input("Probe unit ID", 0, 255, 1, key="ip_unit")
        if st.button("Scan subnet", type="primary"):
            bar = st.progress(0.0)
            try:
                ss.scan["ip"] = scanner.scan_ip(cidr, int(ip_port), ip_to, ip_probe, int(ip_unit),
                                                progress=progress_cb(bar, "IP scan"))
            except ValueError as e:
                st.error(str(e))
        if "ip" in ss.scan:
            found = ss.scan["ip"]
            st.write(f"**{len(found)}** host(s) with port open")
            if found:
                st.dataframe(pd.DataFrame(found), hide_index=True, width="stretch")
                pick = st.selectbox("Use host", [f["host"] for f in found], key="ip_pick")

                def use_host(h=pick, port=int(ip_port)):
                    disconnect()
                    ss.mode, ss.host, ss.tcp_port = "Modbus TCP/IP", h, port
                st.button("Set as connection", on_click=use_host)

# ---------------------------------------------------------------- Write
with tab_write:
    if not ss.allow_writes:
        st.info("Writes are disabled. Turn on 'Enable writes' in the sidebar to use this tab.")
    elif need_client():
        st.warning("Writes go to a live device. Double-check unit ID and address.")
        c = st.columns(3)
        w_fc = c[0].selectbox("Function", [5, 6, 15, 16], format_func=lambda x: fr.FC_NAMES[x], key="w_fc")
        w_unit = c[1].number_input("Unit ID", 0, 255, int(ss.unit), key="w_unit")
        w_addr = c[2].number_input("Address", 0, 65535, 0, key="w_addr")
        payload, err = None, None
        if w_fc == 5:
            payload = st.toggle("Coil ON", key="w_coil")
        elif w_fc == 6:
            v = st.number_input("Value (-32768..65535)", -32768, 65535, 0, key="w_val")
            payload = int(v) & 0xFFFF
        elif w_fc == 15:
            txt = st.text_input("Bits, comma-separated", "1,0,1", key="w_bits")
            try:
                payload = [bool(int(x)) for x in txt.split(",") if x.strip()]
            except ValueError:
                err = "bits must be 0/1"
        else:
            c = st.columns(3)
            w_dtype = c[0].selectbox("Type", DTYPE_LIST, key="w_dtype")
            w_order = c[1].selectbox("Word order", dtypes.WORD_ORDERS, key="w_order")
            w_len = c[2].number_input("String length (regs)", 1, 60, 4, key="w_len", disabled=w_dtype != "string")
            txt = st.text_input("Value(s), comma-separated" if w_dtype != "string" else "Text", "0", key="w_vals")
            try:
                items = [txt] if w_dtype == "string" else [x.strip() for x in txt.split(",") if x.strip()]
                payload = [r for x in items for r in dtypes.encode(x, w_dtype, w_order, int(w_len))]
            except Exception as e:
                err = f"cannot encode: {e}"
        if err:
            st.error(err)
        elif w_fc in (15, 16):
            st.caption(f"Will write {len(payload)} {'coil(s)' if w_fc == 15 else 'register(s)'}: "
                       + (", ".join(f"0x{r:04X}" for r in payload) if w_fc == 16 else str([int(b) for b in payload])))
        readback = st.checkbox("Read back after write", True, key="w_rb")
        confirm = st.checkbox("I confirm this write", key="w_confirm")
        if st.button("Send write", type="primary", disabled=not confirm or err is not None):
            fn = {5: ss.client.write_coil, 6: ss.client.write_register,
                  15: ss.client.write_coils, 16: ss.client.write_registers}[w_fc]
            r = fn(int(w_unit), int(w_addr), payload)
            (st.success if r.ok else st.error)(f"{r.status.value} {r.detail}  ({r.rtt_ms:.1f} ms)")
            if r.ok and readback:
                n = 1 if w_fc in (5, 6) else len(payload)
                rb = ss.client.read(int(w_unit), 1 if w_fc in (5, 15) else 3, int(w_addr), n)
                st.write("Read back:", rb.data if rb.ok else f"{rb.status.value} {rb.detail}")

# ---------------------------------------------------------------- Traffic
with tab_traffic:
    if need_client():
        c = st.columns([1, 1, 1, 3])
        log_on = c[0].toggle("Capture", value=ss.client.log_traffic, key="traffic_on")
        ss.client.log_traffic = log_on
        n_rows = c[1].selectbox("Show last", [50, 200, 1000], key="traffic_n")
        if c[2].button("Clear"):
            ss.client.traffic.clear()
        running = ss.poller is not None and ss.poller.running

        @st.fragment(run_every=ss.refresh if running else None)
        def traffic():
            items = list(ss.client.traffic)[-n_rows:][::-1]
            if not items:
                st.caption("No traffic captured yet.")
                return
            st.dataframe(pd.DataFrame([{
                "time": dt.datetime.fromtimestamp(e.ts).strftime("%H:%M:%S.%f")[:-3], "dir": e.direction,
                "bytes": len(e.data), "frame": e.data.hex(" ").upper(), "note": e.note} for e in items]),
                hide_index=True, width="stretch", height=600)

        traffic()

with tab_notifications:
    if need_client():
        st.subheader("Notification list")

for k in keys_to_persist:
    if k in ss:
        ss._cfg[k] = ss[k]

ss._cfg["tags"] = tag_to_dicts(ss.tags)
ss._cfg["profile_path"] = ss.profile.path if ss.profile is not None and ss.profile.path else None
_snapshot = json.dumps(ss._cfg, sort_keys=True, default=str)
if _snapshot != ss.get("_cfg_saved"):
    try:
        save_settings(ss._cfg)
        ss._cfg_saved = _snapshot
    except OSError as e:
        st.sidebar.caption(f"Could not save settings: {e}")