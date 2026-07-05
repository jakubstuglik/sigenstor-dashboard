#!/usr/bin/env python3
"""
SigenStor Dashboard - Modern monitoring app for Sigenergy SigenStor
Python + NiceGUI + Plotly + SQLite + pymodbus (read-only)
"""

import asyncio
import json
import logging
import os
import sqlite3
import struct
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from nicegui import ui, app
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

APP_TITLE = "SigenStor Dashboard"
DATA_DIR = Path("data")
LOGS_DIR = Path("logs")
CONFIG_FILE = Path("config.json")
DB_FILE = DATA_DIR / "sigenstor.db"

# Default config
DEFAULT_CONFIG = {
    "ip": "192.168.33.13",
    "port": 502,
    "slave_id": 247,
    "poll_interval": 2,  # seconds - data poll every 2s as requested
    "enabled": True,
    "buy_price_grosze": 75,   # e.g. 75 grosze = 0.75 PLN / kWh
    "sell_price_grosze": 35,  # e.g. 35 grosze = 0.35 PLN / kWh
    "battery_capacity_kwh": 18.0,  # default battery usable capacity in kWh (user's system: 18)
    "power_visible": {"PV": True, "Battery": True, "Grid": True, "Load": True},
    "smoothing": 0,  # 0=none, 3=3-reading avg, 5=5-reading avg
    "auto_refresh_enabled": True,
    "auto_refresh_interval": 10,
}

# Easy-to-extend Modbus register map (plant level, slave 247)
# dtype: uint16, int16, uint32, int32
# scale: multiply raw by this to get final value (e.g. 0.001 for W->kW)
# Addresses based on Sigenergy Modbus Protocol V1.7 / V2.x (verify with your firmware)
REGISTERS: Dict[str, Dict[str, Any]] = {
    "soc": {
        "addr": 30014,
        "count": 1,
        "dtype": "uint16",
        "scale": 0.1,
        "unit": "%",
        "label": "Battery SOC",
    },
    "pv_power": {
        "addr": 30035,
        "count": 2,
        "dtype": "int32",
        "scale": 0.001,
        "unit": "kW",
        "label": "PV Power",
    },
    "battery_power": {
        "addr": 30037,
        "count": 2,
        "dtype": "int32",
        "scale": 0.001,
        "unit": "kW",
        "label": "Battery Power",
    },
    "grid_power": {
        "addr": 30005,
        "count": 2,
        "dtype": "int32",
        "scale": 0.001,
        "unit": "kW",
        "label": "Grid Power",
    },
    "grid_status": {
        "addr": 30009,
        "count": 1,
        "dtype": "uint16",
        "scale": 1,
        "unit": "",
        "label": "Grid Status",
    },
    # Battery limits (verify addresses against your Sigenergy Modbus map V1.7/V2.x)
    "battery_max_charge_power": {
        "addr": 30041,
        "count": 2,
        "dtype": "int32",
        "scale": 0.001,
        "unit": "kW",
        "label": "Battery Max Charge",
    },
    "battery_max_discharge_power": {
        "addr": 30043,
        "count": 2,
        "dtype": "int32",
        "scale": 0.001,
        "unit": "kW",
        "label": "Battery Max Discharge",
    },
    # More useful polls (addresses are examples based on common Sigenergy maps - verify!)
    "battery_voltage": {
        "addr": 30039,
        "count": 2,
        "dtype": "int32",
        "scale": 0.1,
        "unit": "V",
        "label": "Battery Voltage",
    },
    "inverter_temp": {
        "addr": 30011,
        "count": 1,
        "dtype": "int16",
        "scale": 0.1,
        "unit": "°C",
        "label": "Inverter Temp",
    },
    # Add more here easily, e.g. temperatures, phase powers, etc.
    # "plant_active_power": {"addr": 30031, "count": 2, "dtype": "int32", "scale": 0.001, "unit": "kW", ...},
}

# Status mapping
GRID_STATUS_MAP = {
    0: "On Grid",
    1: "Off Grid (auto)",
    2: "Off Grid (manual)",
}

RUNNING_STATE_MAP = {  # 30051 if added later
    0: "Standby",
    1: "Running",
    # extend as needed
}

# =============================================================================
# LOGGING
# =============================================================================

def setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"sigenstor_{datetime.now().strftime('%Y%m%d')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    return logging.getLogger("sigenstor")


logger = setup_logging()

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Reading:
    timestamp: str  # ISO format
    soc: Optional[float] = None
    pv_power: Optional[float] = None
    battery_power: Optional[float] = None
    grid_power: Optional[float] = None
    load_power: Optional[float] = None
    grid_status: Optional[int] = None
    battery_max_charge_power: Optional[float] = None
    battery_max_discharge_power: Optional[float] = None
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d.get("raw") is None:
            d.pop("raw", None)
        return d


# =============================================================================
# CONFIG
# =============================================================================

def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # merge defaults
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception as e:
            logger.warning(f"Failed to load config, using defaults: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        logger.info("Config saved")
    except Exception as e:
        logger.error(f"Failed to save config: {e}")


# =============================================================================
# MODBUS CLIENT
# =============================================================================

class SigenModbusClient:
    def __init__(self, ip: str, port: int, slave_id: int):
        self.ip = ip
        self.port = port
        self.slave_id = slave_id
        self.client: Optional[AsyncModbusTcpClient] = None
        self.connected = False

    async def connect(self) -> bool:
        # Always clean up any previous client before attempting a new connection.
        # This prevents the underlying socket/transport from getting into a
        # half-open or corrupted state after repeated failures (common cause
        # of "stops returning data after a while, restart fixes it").
        await self.close()
        try:
            self.client = AsyncModbusTcpClient(host=self.ip, port=self.port, timeout=5)
            self.connected = await self.client.connect()
            if self.connected:
                logger.info(f"Connected to Modbus TCP at {self.ip}:{self.port}")
            else:
                logger.warning("Modbus connect returned False")
            return self.connected
        except Exception as e:
            logger.error(f"Modbus connect error: {e}")
            self.connected = False
            return False

    async def close(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self.connected = False

    async def read_register(self, reg_def: Dict[str, Any]) -> Optional[float]:
        """Read a single register definition and return scaled value."""
        if not self.client or not self.connected:
            return None
        try:
            addr = reg_def["addr"]
            count = reg_def["count"]
            dtype = reg_def["dtype"]
            scale = reg_def.get("scale", 1.0)

            result = await self.client.read_holding_registers(
                address=addr, count=count, device_id=self.slave_id
            )
            if result.isError():
                logger.debug(f"Modbus error reading {addr}: {result}")
                self.connected = False
                return None

            regs = result.registers
            raw = self._decode(regs, dtype)
            if raw is None:
                return None
            return float(raw) * float(scale)
        except ModbusException as e:
            logger.debug(f"ModbusException: {e}")
            self.connected = False
            return None
        except Exception as e:
            logger.warning(f"Unexpected read error at {reg_def['addr']}: {e}")
            self.connected = False
            return None

    def _decode(self, regs: List[int], dtype: str) -> Optional[int]:
        if not regs:
            return None
        try:
            if dtype == "uint16":
                return regs[0]
            elif dtype == "int16":
                val = regs[0]
                return val if val < 0x8000 else val - 0x10000
            elif dtype in ("uint32", "int32"):
                if len(regs) < 2:
                    return None
                # Big endian word order (common for these devices)
                high, low = regs[0], regs[1]
                raw_bytes = struct.pack(">HH", high, low)
                if dtype == "uint32":
                    return struct.unpack(">I", raw_bytes)[0]
                else:
                    return struct.unpack(">i", raw_bytes)[0]
            else:
                return regs[0]
        except Exception as e:
            logger.debug(f"Decode error ({dtype}): {e}")
            return None

    async def read_all(self) -> Optional[Reading]:
        """Read all defined registers + compute derived values."""
        if not self.connected:
            if not await self.connect():
                self.connected = False
                return None

        data: Dict[str, Any] = {}
        raw_data: Dict[str, Any] = {}

        success = True
        for key, reg_def in REGISTERS.items():
            val = await self.read_register(reg_def)
            data[key] = val
            raw_data[key] = val
            if val is None:
                success = False

        if not success:
            # treat as partial fail; still return what we have or None
            # for demo we keep going with what we got
            pass

        # Critical: if SOC (first/essential register) is missing, do not return a
        # partial/degraded reading. This prevents the UI from entering and getting
        # stuck in the "— / 0.00 kW → / Unknown" state (see diagnosis).
        if data.get("soc") is None:
            logger.debug("SOC read failed or missing; returning None to avoid poisoning latest_reading / UI")
            return None

        # Compute load power: Load = PV + Grid - Battery
        pv = data.get("pv_power") or 0.0
        bat = data.get("battery_power") or 0.0
        grid = data.get("grid_power") or 0.0
        load = pv + grid - bat

        ts = datetime.now(timezone.utc).isoformat()

        reading = Reading(
            timestamp=ts,
            soc=data.get("soc"),
            pv_power=data.get("pv_power"),
            battery_power=data.get("battery_power"),
            grid_power=data.get("grid_power"),
            load_power=round(load, 3),
            grid_status=int(data.get("grid_status")) if data.get("grid_status") is not None else None,
            battery_max_charge_power=data.get("battery_max_charge_power"),
            battery_max_discharge_power=data.get("battery_max_discharge_power"),
            raw=raw_data,
        )
        return reading

    async def test_connection(self) -> Tuple[bool, str]:
        """Quick test: try to read SOC."""
        ok = await self.connect()
        if not ok:
            return False, "Cannot connect to Modbus TCP server"

        try:
            soc_reg = REGISTERS["soc"]
            val = await self.read_register(soc_reg)
            if val is not None:
                return True, f"OK - SOC = {val:.1f}%"
            return False, "Connected but failed to read SOC register"
        except Exception as e:
            return False, f"Test failed: {e}"
        finally:
            await self.close()


# =============================================================================
# DATABASE (aiosqlite)
# =============================================================================

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    soc REAL,
    pv_power REAL,
    battery_power REAL,
    grid_power REAL,
    load_power REAL,
    grid_status INTEGER,
    battery_max_charge_power REAL,
    battery_max_discharge_power REAL
);

CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements(timestamp);

CREATE TABLE IF NOT EXISTS energy_daily (
    date TEXT PRIMARY KEY,
    pv_energy_kwh REAL DEFAULT 0,
    battery_discharge_kwh REAL DEFAULT 0,
    grid_import_kwh REAL DEFAULT 0,
    grid_export_kwh REAL DEFAULT 0,
    load_energy_kwh REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS aggregation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,
    duration_seconds REAL,
    rows_processed INTEGER DEFAULT 0,
    buckets_created INTEGER DEFAULT 0,
    status TEXT DEFAULT 'success',
    error_message TEXT,
    cutoff_time TEXT
);

CREATE INDEX IF NOT EXISTS idx_aggregation_runs_ts ON aggregation_runs(run_timestamp);
"""


async def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript(DB_SCHEMA)
        # Migration for added columns (idempotent)
        try:
            cols = [row[1] async for row in await db.execute("PRAGMA table_info(measurements)")]
            if "battery_max_charge_power" not in cols:
                await db.execute("ALTER TABLE measurements ADD COLUMN battery_max_charge_power REAL")
            if "battery_max_discharge_power" not in cols:
                await db.execute("ALTER TABLE measurements ADD COLUMN battery_max_discharge_power REAL")
            await db.commit()
        except Exception:
            pass  # ignore if already exist or other
    logger.info(f"Database ready: {DB_FILE}")


async def insert_reading(reading: Reading):
    if not reading:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            INSERT INTO measurements (timestamp, soc, pv_power, battery_power, grid_power, load_power, grid_status, battery_max_charge_power, battery_max_discharge_power)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reading.timestamp,
                reading.soc,
                reading.pv_power,
                reading.battery_power,
                reading.grid_power,
                reading.load_power,
                reading.grid_status,
                reading.battery_max_charge_power,
                reading.battery_max_discharge_power,
            ),
        )
        await db.commit()


async def get_latest_reading() -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM measurements ORDER BY timestamp DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
    return None


async def get_readings_since(since: datetime) -> List[Dict[str, Any]]:
    since_str = since.isoformat()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM measurements WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since_str,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_readings_range(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM measurements 
               WHERE timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp ASC""",
            (start.isoformat(), end.isoformat()),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_recent_raw(limit: int = 200) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM measurements ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_aggregation_runs(limit: int = 100) -> List[Dict[str, Any]]:
    """Get recent aggregation run records for UI display."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM aggregation_runs 
               ORDER BY run_timestamp DESC 
               LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# Simple energy integration (trapezoidal) from power series
def compute_energy_kwh(rows: List[Dict], power_key: str) -> float:
    if len(rows) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(rows)):
        t0 = datetime.fromisoformat(rows[i-1]["timestamp"])
        t1 = datetime.fromisoformat(rows[i]["timestamp"])
        dt_h = (t1 - t0).total_seconds() / 3600.0
        p0 = rows[i-1].get(power_key) or 0.0
        p1 = rows[i].get(power_key) or 0.0
        # Trapezoid
        total += ((p0 + p1) / 2.0) * dt_h
    return round(total, 3)


def get_current_prices_pln():
    cfg = load_config()
    return cfg.get("buy_price_grosze", 75) / 100.0, cfg.get("sell_price_grosze", 35) / 100.0


def get_battery_capacity_kwh() -> float:
    cfg = load_config()
    return float(cfg.get("battery_capacity_kwh", 10.0))


async def get_summary(start: datetime, end: datetime) -> Dict[str, float]:
    rows = await get_readings_range(start, end)
    if not rows:
        return {"pv": 0, "battery_discharge": 0, "grid_import": 0, "grid_export": 0, "load": 0, "self_consumption_pct": 0}

    pv = compute_energy_kwh(rows, "pv_power")
    load = compute_energy_kwh(rows, "load_power")

    # Integrate signed flows (positive contributions only)
    bat_discharge = 0.0
    grid_import = 0.0
    grid_export = 0.0
    for i in range(1, len(rows)):
        t0 = datetime.fromisoformat(rows[i-1]["timestamp"])
        t1 = datetime.fromisoformat(rows[i]["timestamp"])
        dt_h = max(0.0, (t1 - t0).total_seconds() / 3600.0)
        b0 = rows[i-1].get("battery_power") or 0.0
        b1 = rows[i].get("battery_power") or 0.0
        g0 = rows[i-1].get("grid_power") or 0.0
        g1 = rows[i].get("grid_power") or 0.0

        # discharge part of battery
        bat_discharge += max(0.0, -((b0 + b1) / 2.0)) * dt_h
        # grid import / export
        grid_import += max(0.0, ((g0 + g1) / 2.0)) * dt_h
        grid_export += max(0.0, -((g0 + g1) / 2.0)) * dt_h

    bat_discharge = round(bat_discharge, 3)
    grid_import = round(grid_import, 3)
    grid_export = round(grid_export, 3)

    self_cons = 0.0
    if pv > 0.001:
        self_cons = round((pv - grid_export) / pv * 100, 1)

    return {
        "pv": pv,
        "battery_discharge": bat_discharge,
        "grid_import": grid_import,
        "grid_export": grid_export,
        "load": load,
        "self_consumption_pct": self_cons,
    }


async def get_multi_period_summaries():
    """Return list of (label, summary_dict) for Today, Yesterday, rolling Week, calendar Month, Year."""
    now = datetime.now(timezone.utc)
    results = []

    # Today (from midnight)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    results.append(("Today", await get_summary(start, now)))

    # Yesterday (full calendar day)
    y_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    y_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    results.append(("Yesterday", await get_summary(y_start, y_end)))

    # Week (rolling last 7 days)
    w_start = now - timedelta(days=7)
    results.append(("Week (rolling)", await get_summary(w_start, now)))

    # Current calendar month
    m_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    results.append(("Month", await get_summary(m_start, now)))

    # Current calendar year
    y_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    results.append(("Year", await get_summary(y_start, now)))

    return results


def create_period_energy_chart(period_data):
    """Stacked/grouped bar chart for cumulative energy flows (PV, Battery, Grid)."""
    if not period_data:
        fig = go.Figure()
        fig.add_annotation(text="No data", x=0.5, y=0.5, showarrow=False)
        return fig

    labels = [item[0] for item in period_data]
    pv = [item[1].get("pv", 0) for item in period_data]
    bat = [item[1].get("battery_discharge", 0) for item in period_data]
    g_in = [item[1].get("grid_import", 0) for item in period_data]
    g_out = [item[1].get("grid_export", 0) for item in period_data]

    fig = go.Figure()

    # Stacked sources (makes total supply visible)
    fig.add_trace(go.Bar(
        x=labels, y=pv, name="☀️ PV", marker_color="#ffc107",
        hovertemplate="PV: %{y:.2f} kWh<extra></extra>"
    ))
    fig.add_trace(go.Bar(
        x=labels, y=bat, name="🔋 Battery discharge", marker_color="#4caf50",
        hovertemplate="Battery: %{y:.2f} kWh<extra></extra>"
    ))
    fig.add_trace(go.Bar(
        x=labels, y=g_in, name="⬇️ Grid import", marker_color="#2196f3",
        hovertemplate="Grid in: %{y:.2f} kWh<extra></extra>"
    ))

    # Grid export as additional visible series (not stacked into supply)
    fig.add_trace(go.Bar(
        x=labels, y=g_out, name="⬆️ Grid export", marker_color="#ef4444",
        hovertemplate="Grid out: %{y:.2f} kWh<extra></extra>"
    ))

    fig.update_layout(
        barmode="group",  # grouped for clear comparison; switch to "stack" if preferred for supply total
        title="Energy by Period (kWh) — PV + Battery + Grid flows",
        yaxis_title="kWh",
        paper_bgcolor="#1e1e1e",
        plot_bgcolor="#1e1e1e",
        font=dict(color="#ddd", size=10, family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
        height=260,
        margin=dict(t=30, b=5, l=40, r=10),
        legend=dict(orientation="h", y=-0.15, x=0.5, xanchor="center"),
        bargap=0.25,
        xaxis=dict(gridcolor="#333"),
        yaxis=dict(gridcolor="#333"),
        hoverlabel=dict(bgcolor="#1f2937", bordercolor="#374151", font=dict(color="#e5e7eb", size=11))
    )
    return fig


async def refresh_period_energy_chart():
    """Top-level refresh for the period energy bar chart (can be called from live update timers)."""
    global period_energy_chart
    try:
        if period_energy_chart is None:
            return
        pdata = await get_multi_period_summaries()
        pfig = create_period_energy_chart(pdata)
        period_energy_chart.update_figure(pfig)
    except Exception:
        pass  # guard against DB issues or UI teardown


# =============================================================================
# BACKGROUND POLLER
# =============================================================================

poller_task: Optional[asyncio.Task] = None
maintenance_task: Optional[asyncio.Task] = None
latest_reading: Optional[Reading] = None
modbus_client: Optional[SigenModbusClient] = None
current_config: Dict[str, Any] = load_config()


async def start_poller():
    global poller_task, maintenance_task, modbus_client, current_config
    if poller_task and not poller_task.done():
        poller_task.cancel()

    current_config = load_config()
    modbus_client = SigenModbusClient(
        current_config["ip"],
        current_config["port"],
        current_config["slave_id"],
    )

    async def _poll_loop():
        global latest_reading
        logger.info("Poller started")
        while True:
            cfg = load_config()
            if not cfg.get("enabled", True):
                await asyncio.sleep(5)
                continue

            try:
                reading = await modbus_client.read_all()
                if reading:
                    latest_reading = reading
                    await insert_reading(reading)
                    logger.debug(f"Polled: SOC={reading.soc} PV={reading.pv_power} Bat={reading.battery_power} Grid={reading.grid_power}")
                    # Best-effort immediate UI refresh for live dashboard
                    try:
                        ui.timer(0, update_live_dashboard, once=True)
                    except Exception:
                        pass  # safe if not on dashboard or no context
                else:
                    logger.warning("Poll returned no data (connection issue?)")
                    # Ensure we clean up the client on every failure path.
                    # Prevents the Modbus client from entering a permanently bad state.
                    if modbus_client:
                        await modbus_client.close()
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
                if modbus_client:
                    await modbus_client.close()

            interval = max(2, int(cfg.get("poll_interval", 2)))
            await asyncio.sleep(interval)

    poller_task = asyncio.create_task(_poll_loop())
    maintenance_task = asyncio.create_task(maintenance_loop())


async def stop_poller():
    global poller_task, maintenance_task, modbus_client
    if poller_task:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
    if maintenance_task:
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
    if modbus_client:
        await modbus_client.close()
    logger.info("Poller stopped")


async def aggregate_old_data() -> int:
    """Aggregate measurements older than 1 day into 30s buckets to save space.
    Always records the run into aggregation_runs table.
    """
    import time as _time  # local to avoid shadowing
    start_ts = datetime.now(timezone.utc)
    start_perf = _time.perf_counter()
    cutoff = (start_ts - timedelta(days=1)).isoformat()
    rows_processed = 0
    buckets_created = 0
    status = 'success'
    error_message = None

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            cur = await db.execute(
                """SELECT timestamp, soc, pv_power, battery_power, grid_power, load_power, grid_status,
                          battery_max_charge_power, battery_max_discharge_power
                   FROM measurements WHERE timestamp < ? ORDER BY timestamp""",
                (cutoff,)
            )
            rows = await cur.fetchall()
            rows_processed = len(rows) if rows else 0

            if not rows:
                # no rows, but will still record the run below
                pass
            else:
                from collections import defaultdict
                buckets = defaultdict(list)
                for row in rows:
                    ts = row[0]
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    # floor to 30s
                    secs = (dt.second // 30) * 30
                    bucket_dt = dt.replace(second=secs, microsecond=0)
                    key = bucket_dt.isoformat()
                    buckets[key].append(row[1:])

                inserts = []
                for key, group in buckets.items():
                    if not group:
                        continue
                    n = len(group)
                    def avg(idx):
                        vals = [g[idx] for g in group if g[idx] is not None]
                        return sum(vals) / len(vals) if vals else None
                    soc = avg(0)
                    pv = avg(1)
                    bat = avg(2)
                    grid = avg(3)
                    load = avg(4)
                    # status: last non-null
                    status_val = None
                    for g in reversed(group):
                        if g[5] is not None:
                            status_val = g[5]
                            break
                    max_ch = avg(6)
                    max_dis = avg(7)
                    inserts.append((key, soc, pv, bat, grid, load, status_val, max_ch, max_dis))

                if inserts:
                    await db.execute("DELETE FROM measurements WHERE timestamp < ?", (cutoff,))
                    await db.executemany(
                        """INSERT INTO measurements (timestamp, soc, pv_power, battery_power, grid_power, load_power,
                                                      grid_status, battery_max_charge_power, battery_max_discharge_power)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        inserts
                    )
                    await db.commit()
                    buckets_created = len(inserts)
                    logger.info(f"Aggregated {rows_processed} rows >1d old into {buckets_created} 30s buckets")

    except Exception as e:
        status = 'error'
        error_message = str(e)
        logger.error(f"Aggregation run error: {e}")

    # Always record the run
    duration = _time.perf_counter() - start_perf
    try:
        await _log_aggregation_run(
            run_timestamp=start_ts.isoformat(),
            duration_seconds=round(duration, 3),
            rows_processed=rows_processed,
            buckets_created=buckets_created,
            status=status,
            error_message=error_message,
            cutoff_time=cutoff
        )
    except Exception as log_err:
        logger.error(f"Failed to log aggregation run: {log_err}")

    return rows_processed


async def _log_aggregation_run(
    run_timestamp: str,
    duration_seconds: float,
    rows_processed: int,
    buckets_created: int,
    status: str,
    error_message: Optional[str],
    cutoff_time: str
):
    async with aiosqlite.connect(DB_FILE) as db:
        # Ensure table exists (for running instances before restart)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS aggregation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp TEXT NOT NULL,
                duration_seconds REAL,
                rows_processed INTEGER DEFAULT 0,
                buckets_created INTEGER DEFAULT 0,
                status TEXT DEFAULT 'success',
                error_message TEXT,
                cutoff_time TEXT
            )
        """)
        await db.execute(
            """INSERT INTO aggregation_runs 
               (run_timestamp, duration_seconds, rows_processed, buckets_created, status, error_message, cutoff_time)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_timestamp, duration_seconds, rows_processed, buckets_created, status, error_message, cutoff_time)
        )
        await db.commit()


async def maintenance_loop():
    while True:
        try:
            await aggregate_old_data()
        except Exception as e:
            logger.error(f"Maintenance aggregation error: {e}")
        await asyncio.sleep(3600)  # run hourly


async def monitor_maintenance_task():
    """Monitor the maintenance task and restart it if it stops/crashes."""
    global maintenance_task
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        if maintenance_task is None:
            continue
        if maintenance_task.done():
            try:
                exc = maintenance_task.exception()
                if exc:
                    logger.error(f"Maintenance task crashed: {exc}")
            except asyncio.CancelledError:
                logger.info("Maintenance task was cancelled")
            except Exception as e:
                logger.error(f"Error inspecting maintenance task: {e}")
            logger.warning("Maintenance task stopped unexpectedly. Restarting...")
            try:
                maintenance_task = asyncio.create_task(maintenance_loop())
                logger.info("Maintenance task restarted")
            except Exception as restart_err:
                logger.error(f"Failed to restart maintenance task: {restart_err}")


# =============================================================================
# UI HELPERS
# =============================================================================

def status_badge(status: Optional[int]) -> str:
    if status is None:
        return "❓ Unknown"
    label = GRID_STATUS_MAP.get(status, f"Status {status}")
    color = "🟢" if status == 0 else "🟠"
    return f"{color} {label}"


def power_arrow(power: Optional[float], positive="↓ import", negative="↑ export") -> str:
    if power is None:
        return ""
    if power > 0.05:
        return positive
    if power < -0.05:
        return negative
    return "→"


def battery_arrow(power: Optional[float]) -> str:
    if power is None:
        return ""
    if power > 0.05:
        return "↑ charging"
    if power < -0.05:
        return "↓ discharging"
    return "→ idle"


def format_kw(val: Optional[float]) -> str:
    """Format power value: always kW unit, 0.00 no sign, positive no +, negative with -."""
    if val is None:
        return "—"
    try:
        v = float(val)
        if abs(v) < 0.005:
            return "0.00 kW"
        return f"{v:.2f} kW"
    except:
        return "—"


def create_sankey_figure(pv: float, battery: float, grid: float, load: float) -> go.Figure:
    """Create energy flow visualization from (smoothed) power values (kW).
    Values passed in are typically the mean of the last 3 readings to reduce noise.
    """
    # Normalize flows (positive only)
    pv_p = max(0, pv or 0)
    bat_d = max(0, -(battery or 0))   # discharge
    bat_c = max(0, battery or 0)      # charge
    grid_i = max(0, grid or 0)        # import
    grid_e = max(0, -(grid or 0))     # export
    load_p = max(0, load or 0)

    # Simple flow model
    # Sources: PV, Battery(disch), Grid(import)
    # Targets: Load, Battery(ch), Grid(export)

    # (Sankey body removed - using clear bar flow below) 
    pv = pv or 0.0
    battery = battery or 0.0
    grid = grid or 0.0
    load = load or 0.0

    fig = go.Figure()
    y_pos = [3, 2, 1, 0]
    base_names = ["PV", "Battery", "House Load", "Grid"]
    vals = [pv, battery, load, grid]
    cols = ["#ffc107", "#4caf50", "#ff5722", "#2196f3"]
    max_abs = max(1.0, max((abs(v) for v in vals), default=1))

    display_names = list(base_names)
    # Differentiate battery charge vs discharge
    if battery > 0.005:
        display_names[1] = "Battery (ch)"
        cols[1] = "#67e8f9"  # cyan for charging
    elif battery < -0.005:
        display_names[1] = "Battery (dis)"
        cols[1] = "#4ade80"  # green for discharging

    for idx, (name, val, y, col) in enumerate(zip(display_names, vals, y_pos, cols)):
        if abs(val) < 0.005:
            t = "0.00 kW"
        else:
            t = f"{val:.2f} kW"
        fig.add_trace(go.Bar(
            x=[abs(val)],
            y=[y],
            orientation="h",
            marker_color=col,
            name=name,
            text=[t],
            textposition="outside",
            textfont=dict(size=15, color="#ddd"),
        ))

    # Use annotations for y labels with explicit gap from the vertical axis
    for name, y in zip(display_names, y_pos):
        fig.add_annotation(
            x=-0.22 * max_abs,
            y=y,
            text=name,
            xanchor="right",
            yanchor="middle",
            showarrow=False,
            font=dict(color="#ddd", size=15, family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
        )

    fig.update_layout(
        title="Energy Flow (mean of last 3 readings, kW) — arrows indicate typical direction",
        barmode="overlay",
        xaxis=dict(title="|kW|", range=[-0.28 * max_abs, max_abs * 1.35], showgrid=True, gridcolor="#333", zeroline=True, zerolinecolor="#444"),
        yaxis=dict(showticklabels=False, autorange="reversed"),
        height=260,
        paper_bgcolor="#1e1e1e",
        plot_bgcolor="#1e1e1e",
        font=dict(color="#ddd", size=12, family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
        showlegend=False,
        margin=dict(l=180, r=20, t=30, b=15),
    )
    return fig
    source = []
    target = []
    value = []
    color = []

    # PV contributions
    if pv_p > 0.01:
        source.append(0)
        target.append(3)  # to load
        value.append(pv_p)
        color.append("rgba(255, 193, 7, 0.8)")

        remaining_pv = pv_p
        if bat_c > 0.01 and remaining_pv > 0:
            v = min(remaining_pv, bat_c)
            source.append(0)
            target.append(4)
            value.append(v)
            remaining_pv -= v
            color.append("rgba(76, 175, 80, 0.6)")
        if grid_e > 0.01 and remaining_pv > 0:
            v = min(remaining_pv, grid_e)
            source.append(0)
            target.append(5)
            value.append(v)
            color.append("rgba(33, 150, 243, 0.6)")

    # Battery discharge
    if bat_d > 0.01:
        source.append(1)
        target.append(3)
        value.append(bat_d)
        color.append("rgba(76, 175, 80, 0.8)")

    # Grid import
    if grid_i > 0.01:
        source.append(2)
        target.append(3)
        value.append(grid_i)
        color.append("rgba(33, 150, 243, 0.8)")

def make_soc_gauge(soc_val: float) -> go.Figure:
    capacity = get_battery_capacity_kwh()
    soc = soc_val or 0
    kwh = soc * capacity / 100.0

    fig = go.Figure(go.Indicator(
        mode="gauge",
        value=soc,
        gauge={
            'axis': {'range': [0, 100], 'tickcolor': "#888"},
            'bar': {'color': "#00bcd4"},
            'steps': [
                {'range': [0, 20], 'color': "#f66"},
                {'range': [20, 50], 'color': "#fa0"},
                {'range': [50, 100], 'color': "#4caf50"},
            ],
            'threshold': {'line': {'color': "white", 'width': 2}, 'thickness': 0.8, 'value': soc}
        },
        # No title here to avoid clipping; the section header provides context
    ))

    # kWh smaller on top, percent bigger at bottom (same order as before)
    fig.add_annotation(
        x=0.5, y=0.28,
        text=f"{kwh:.1f} kWh",
        font=dict(size=18, color="#fff", family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
        showarrow=False,
        xanchor="center",
        yanchor="middle"
    )
    fig.add_annotation(
        x=0.5, y=0.08,
        text=f"{soc:.1f}%",
        font=dict(size=26, color="#ddd", family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
        showarrow=False,
        xanchor="center",
        yanchor="middle"
    )

    fig.update_layout(paper_bgcolor="#1e1e1e", font={'color': "#ddd", 'family': "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"}, height=220, margin=dict(t=30, b=10, l=10, r=10))
    return fig


def make_mix_donut(pv_p, bat_p, grid_p, load_p) -> go.Figure:
    load = max(0.01, load_p or 0)
    pv_c = max(0, min(pv_p or 0, load))
    bat_c = max(0, min( -(bat_p or 0), load - pv_c))
    grid_c = max(0, load - pv_c - bat_c)
    vals = [pv_c, bat_c, grid_c]
    labels = ["PV direct", "Battery discharge", "Grid import"]
    colors = ["#ffc107", "#4caf50", "#2196f3"]
    if sum(vals) < 0.01:
        vals = [0, 0, 100]
    fig = go.Figure(go.Pie(labels=labels, values=vals, hole=.55, marker=dict(colors=colors)))
    fig.update_layout(
        title="Load Supply Mix (current)",
        paper_bgcolor="#1e1e1e",
        font={'color': "#ddd", 'size': 11, 'family': "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"},
        height=220,
        margin=dict(t=30, b=10, l=10, r=10),
        showlegend=True,
        legend=dict(orientation="h", y=-0.1)
    )
    return fig


def create_power_chart(rows: List[Dict], title: str = "Power over time", visible: dict = None) -> go.Figure:
    if not rows:
        fig = go.Figure()
        fig.add_annotation(text="No data", x=0.5, y=0.5, showarrow=False)
        return fig

    if visible is None:
        cfg = load_config()
        visible = cfg.get("power_visible", {"PV": True, "Battery": True, "Grid": True, "Load": True}).copy()

    ts = [datetime.fromisoformat(r["timestamp"]).astimezone() for r in rows]
    pv = [r.get("pv_power") or 0 for r in rows]
    bat = [r.get("battery_power") or 0 for r in rows]
    grid = [r.get("grid_power") or 0 for r in rows]
    load = [r.get("load_power") or 0 for r in rows]

    fig = go.Figure()

    def vis(name):
        return True if visible.get(name, True) else "legendonly"

    fig.add_trace(go.Scatter(x=ts, y=pv, name="PV", line=dict(color="#ffc107", width=2), visible=vis("PV"), hovertemplate="%{y:.3f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=ts, y=bat, name="Battery", line=dict(color="#4caf50", width=2), visible=vis("Battery"), hovertemplate="%{y:.3f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=ts, y=grid, name="Grid", line=dict(color="#2196f3", width=2), visible=vis("Grid"), hovertemplate="%{y:.3f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=ts, y=load, name="Load", line=dict(color="#ff5722", width=2), visible=vis("Load"), hovertemplate="%{y:.3f}<extra></extra>"))

    fig.update_layout(
        title=title,
        xaxis_title="Time",
        yaxis_title="Power (kW)",
        hovermode="x unified",
        paper_bgcolor="#1e1e1e",
        plot_bgcolor="#1e1e1e",
        font=dict(color="#ddd"),
        legend=dict(orientation="h", y=1.1),
        height=420,
        margin=dict(l=50, r=20, t=50, b=40),
    )
    fig.update_xaxes(gridcolor="#333")
    fig.update_yaxes(gridcolor="#333")
    return fig


def create_soc_chart(rows: List[Dict]) -> go.Figure:
    if not rows:
        fig = go.Figure()
        fig.add_annotation(text="No data", x=0.5, y=0.5)
        return fig

    ts = [datetime.fromisoformat(r["timestamp"]).astimezone() for r in rows]
    soc = [r.get("soc") or 0 for r in rows]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ts, y=soc, name="SOC",
        line=dict(color="#00bcd4", width=3),
        mode="lines+markers",
        marker=dict(size=4, color="#00bcd4"),
        hovertemplate="%{y:.1f}%<extra></extra>"
    ))
    # subtle reference lines
    fig.add_hline(y=50, line_dash="dot", line_color="#555", annotation_text="50%", annotation_position="top left")
    fig.add_hline(y=20, line_dash="dot", line_color="#f66", annotation_text="Low", annotation_position="top left")
    fig.update_layout(
        title="Battery SOC",
        yaxis=dict(range=[0, 105], title="%"),
        paper_bgcolor="#1e1e1e",
        plot_bgcolor="#1e1e1e",
        font=dict(color="#ddd"),
        height=320,
        margin=dict(l=40, r=20, t=40, b=30),
    )
    fig.update_xaxes(gridcolor="#333")
    fig.update_yaxes(gridcolor="#333")
    return fig


# =============================================================================
# UI PAGES / VIEWS
# =============================================================================

# Global UI state references (for live updates)
live_cards: Dict[str, Any] = {}
live_sankey = None
live_last_update = None
main_content = None
live_gauge = None
live_mix = None
period_energy_chart = None
recent_flow_values = deque(maxlen=3)  # last 3 (pv, battery, grid, load) for smoothing Energy Flow
on_dashboard = False  # guard for live updates to avoid deleted element warnings
_charts_auto_timer_started = False
chart_refresh_interval = 10  # seconds for charts auto-refresh (user configurable, >=2)
last_chart_refresh_time = 0.0  # monotonic time of last auto chart refresh
charts_auto_refresh_enabled = True
last_period_refresh = 0.0  # for throttling the heavier period energy chart updates
# plots refs for in-place updates to avoid full re-creation flicker
plots = {"power": None, "soc": None, "cost": None}

current_range = {"hours": 1}
smoothing = 0
power_visible = {"PV": True, "Battery": True, "Grid": True, "Load": True}


def build_sidebar():
    with ui.left_drawer(top_corner=True, bottom_corner=True).style('background-color: #111827; border-right: 1px solid #374151'):
        ui.label("SigenStor").classes("text-2xl font-bold q-pa-md text-white")
        ui.separator()

        nav_items = [
            ("Dashboard", "dashboard", "dashboard"),
            ("Charts", "charts", "show_chart"),
            ("Summary", "summary", "summarize"),
            ("Raw Data", "raw", "table_chart"),
            ("Maintenance", "maintenance", "build"),
            ("Settings", "settings", "settings"),
        ]

        async def async_nav_handler(name: str):
            if name == "dashboard":
                show_dashboard()
            elif name == "charts":
                await show_charts()
            elif name == "summary":
                await show_summary()
            elif name == "raw":
                await show_raw_data()
            elif name == "maintenance":
                await show_maintenance()
            elif name == "settings":
                show_settings()

        for label, func_name, icon in nav_items:
            ui.button(label, icon=icon, on_click=lambda n=func_name: asyncio.create_task(async_nav_handler(n))).props("flat").classes(
                "w-full justify-start q-pa-md text-lg"
            ).style("color: #e5e7eb")


def update_live_dashboard():
    """Called by timer to refresh live values."""
    global latest_reading, last_period_refresh, recent_flow_values, on_dashboard

    if not on_dashboard:
        return

    try:
        async def ensure_latest():
            global latest_reading
            row = await get_latest_reading()
            if row and row.get("soc") is not None:
                # Only accept rows that have valid SOC (prevents loading old poisoned partial rows)
                latest_reading = Reading(
                    timestamp=row.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                    soc=row.get("soc"),
                    pv_power=row.get("pv_power"),
                    battery_power=row.get("battery_power"),
                    grid_power=row.get("grid_power"),
                    load_power=row.get("load_power"),
                    grid_status=row.get("grid_status"),
                    battery_max_charge_power=row.get("battery_max_charge_power"),
                    battery_max_discharge_power=row.get("battery_max_discharge_power"),
                    raw=None,
                )

        # Always try to have the freshest from DB
        needs_refresh = not latest_reading
        if latest_reading:
            try:
                ts = latest_reading.timestamp
                if ts.endswith('Z'):
                    ts = ts[:-1] + '+00:00'
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
                if age > 8:
                    needs_refresh = True
            except Exception:
                needs_refresh = True
        if needs_refresh:
            asyncio.create_task(ensure_latest())

        if not latest_reading:
            return

        r = latest_reading

        # Collect recent power values for smoothing the Energy Flow visualization (mean of last 3)
        try:
            pv = float(r.pv_power or 0.0)
            bat = float(r.battery_power or 0.0)
            grd = float(r.grid_power or 0.0)
            lod = float(r.load_power or 0.0)
            recent_flow_values.append((pv, bat, grd, lod))
        except Exception:
            pass
    except Exception:
        # Prevent timer slot errors from crashing the callback
        return

    # Update cards (guarded)
    try:
        if "soc" in live_cards:
            live_cards["soc"].set_text(f"{r.soc:.1f}%" if r.soc is not None else "—")
        if "pv" in live_cards:
            live_cards["pv"].set_text(format_kw(r.pv_power))
        if "battery" in live_cards:
            bp = r.battery_power or 0
            arrow = "↑" if bp > 0.05 else ("↓" if bp < -0.05 else "→")
            bstr = "0.00" if abs(bp) < 0.005 else f"{bp:.2f}"
            live_cards["battery"].set_text(f"{bstr} kW {arrow}")
        if "grid" in live_cards:
            gp = r.grid_power or 0
            arrow = "↓" if gp > 0.05 else ("↑" if gp < -0.05 else "→")
            gstr = "0.00" if abs(gp) < 0.005 else f"{gp:.2f}"
            live_cards["grid"].set_text(f"{gstr} kW {arrow}")
        if "load" in live_cards:
            live_cards["load"].set_text(format_kw(r.load_power))
        if "status" in live_cards:
            live_cards["status"].set_text(status_badge(r.grid_status))

        if live_last_update:
            live_last_update.set_text(f"Last update: {datetime.fromisoformat(r.timestamp).astimezone().strftime('%H:%M:%S')}")
    except Exception:
        pass  # UI elements may have been cleared on navigation

    # Update Sankey / flow  (uses mean of last 3 readings to reduce jitter)
    try:
        if live_sankey is not None:
            if recent_flow_values:
                n = len(recent_flow_values)
                avg_pv = sum(v[0] for v in recent_flow_values) / n
                avg_bat = sum(v[1] for v in recent_flow_values) / n
                avg_grd = sum(v[2] for v in recent_flow_values) / n
                avg_lod = sum(v[3] for v in recent_flow_values) / n
            else:
                avg_pv = r.pv_power or 0
                avg_bat = r.battery_power or 0
                avg_grd = r.grid_power or 0
                avg_lod = r.load_power or 0
            fig = create_sankey_figure(avg_pv, avg_bat, avg_grd, avg_lod)
            live_sankey.update_figure(fig)
    except Exception:
        pass

    # Update gauge + mix donut
    try:
        if 'live_gauge' in globals() and live_gauge is not None:
            gfig = make_soc_gauge(r.soc or 0)
            live_gauge.update_figure(gfig)
        if 'live_mix' in globals() and live_mix is not None:
            mfig = make_mix_donut(r.pv_power, r.battery_power, r.grid_power, r.load_power)
            live_mix.update_figure(mfig)
        if 'live_bat_max_disch' in globals() and live_bat_max_disch is not None:
            md = r.battery_max_discharge_power
            live_bat_max_disch.set_text(f"{md:.2f}" if md is not None else "—")
        if 'live_bat_max_ch' in globals() and live_bat_max_ch is not None:
            mc = r.battery_max_charge_power
            live_bat_max_ch.set_text(f"{mc:.2f}" if mc is not None else "—")
    except Exception:
        pass  # may be stale after nav

    # Throttled refresh for the period energy chart (updates "Today" etc. while on dashboard)
    # Heavier query (full period summaries), so only every ~60s
    try:
        now = time.monotonic()
        if now - last_period_refresh >= 60:
            last_period_refresh = now
            asyncio.create_task(refresh_period_energy_chart())
    except Exception:
        pass


def show_dashboard():
    """Main real-time dashboard."""
    global main_content, live_last_update, live_sankey, period_energy_chart, on_dashboard
    on_dashboard = True
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        with ui.column().classes("w-full p-4 gap-4"):
            ui.label("Real-time Dashboard").classes("text-3xl font-bold mb-2")

            # Top status bar
            with ui.row().classes("w-full items-center gap-4"):
                ui.badge("LIVE", color="green").props("rounded")
                live_last_update = ui.label("Last update: —").classes("text-sm text-gray-400")

            # KPI Cards - uniform size, text fits nicely
            with ui.row().classes("w-full gap-3 flex-wrap"):
                def make_card(title: str, icon: str, key: str, color: str):
                    with ui.card().classes("flex-1 min-w-[138px] max-w-[178px] min-h-[102px] p-2.5 bg-[#1f2937] border border-[#374151] flex flex-col justify-between"):
                        with ui.row().classes("items-center gap-1"):
                            ui.icon(icon).classes(f"text-xl text-{color} flex-none")
                            ui.label(title).classes("text-[10px] text-gray-400 leading-none")
                        val_label = ui.label("—").classes("text-[26px] font-bold leading-none mt-0.5 value-num")
                        live_cards[key] = val_label

                make_card("Battery SOC", "battery_charging_full", "soc", "cyan-400")
                make_card("PV Power", "wb_sunny", "pv", "yellow-400")
                make_card("Battery", "battery_std", "battery", "green-400")
                make_card("Grid", "electrical_services", "grid", "blue-400")
                make_card("House Load", "home", "load", "orange-400")
                make_card("System Status", "power", "status", "gray-400")

            # Energy Flow (bars)
            ui.label("Energy Flow (mean of last 3 readings)").classes("text-xl mt-4 mb-1 font-semibold section-title")
            initial_fig = create_sankey_figure(0, 0, 0, 0)
            live_sankey = ui.plotly(initial_fig).classes("w-full")

            # Battery Gauge and Load Mix side-by-side
            ui.label("Battery Gauge & Current Load Mix").classes("text-lg font-semibold mt-2")
            with ui.row().classes("w-full gap-4"):
                gauge_container = ui.column().classes("flex-1")
                mix_container = ui.column().classes("flex-1")

            global live_gauge, live_mix, live_bat_max_disch, live_bat_max_ch
            with gauge_container:
                live_gauge = ui.plotly(make_soc_gauge(0)).classes("w-full")
                with ui.row().classes("text-[10px] text-gray-400 gap-3 mt-1 justify-center"):
                    ui.label("Max disch").classes("text-[9px]")
                    live_bat_max_disch = ui.label("—").classes("font-mono text-gray-200 text-[11px] font-semibold")
                    ui.label("kW").classes("text-[9px]")
                    ui.label("  Max ch").classes("text-[9px] ml-1")
                    live_bat_max_ch = ui.label("—").classes("font-mono text-gray-200 text-[11px] font-semibold")
                    ui.label("kW").classes("text-[9px]")
            with mix_container:
                live_mix = ui.plotly(make_mix_donut(0,0,0,0)).classes("w-full")

            # Period cumulative energy flows (new stacked/grouped bar chart)
            ui.label("Energy by Period — PV, Battery, Grid (Today / Yesterday / Week / Month / Year)").classes("text-xl mt-4 mb-1 font-semibold section-title")
            period_energy_chart = ui.plotly(create_period_energy_chart([])).classes("w-full")  # placeholder (updated shortly)

            # initial load of period bars (once) - uses top-level refresher
            ui.timer(0.05, lambda: asyncio.create_task(refresh_period_energy_chart()), once=True)

            # Quick note
            ui.label("Data is polled in background and saved to SQLite. Charts update automatically.").classes("text-xs text-gray-500 mt-2")

            # Auto-refresh timer + immediate population from DB
            # Note: timers are created once outside to avoid slot deletion errors on navigation
            pass  # timers created globally after initial build


async def show_charts():
    global main_content, current_range, smoothing, power_visible, auto_status, charts_auto_refresh_enabled, chart_refresh_interval, on_dashboard
    on_dashboard = False
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        with ui.column().classes("w-full p-4"):
            ui.label("Historical Charts").classes("text-3xl font-bold mb-2")

            # Declare containers first (will be created after controls for correct top position)
            ranges = [
                ("Last 1h", 1),
                ("Last 6h", 6),
                ("Last 24h", 24),
                ("Last 7 days", 24*7),
                ("Last 30 days", 24*30),
            ]
            cfg = load_config()
            current_range["hours"] = 1
            power_visible = cfg.get("power_visible", {"PV": True, "Battery": True, "Grid": True, "Load": True}).copy()
            smoothing = cfg.get("smoothing", 0)
            charts_auto_refresh_enabled = bool(cfg.get("auto_refresh_enabled", True))
            chart_refresh_interval = int(cfg.get("auto_refresh_interval", 10))
            chart_container = None
            auto_status = None
            pause_btn = None
            # reset per-visit plot refs
            plots["power"] = plots["soc"] = plots["cost"] = None

            period_buttons = {}
            smoothing_buttons = {}

            def _smooth_rows(rows, window):
                if window < 2 or len(rows) < 2:
                    return rows
                smoothed = [dict(r) for r in rows]
                keys = ["pv_power", "battery_power", "grid_power", "load_power", "soc"]
                for key in keys:
                    for i in range(len(smoothed)):
                        start = max(0, i - window + 1)
                        vals = [smoothed[j].get(key) for j in range(start, i + 1) if smoothed[j].get(key) is not None]
                        if vals:
                            smoothed[i][key] = sum(vals) / len(vals)
                return smoothed

            # Define load first (closures will resolve names at runtime)
            async def load_and_render(hours: int, from_auto: bool = False):
                global current_range, smoothing, power_visible, auto_status
                try:
                    since = datetime.now(timezone.utc) - timedelta(hours=hours)
                    rows = await get_readings_since(since)

                    if smoothing > 1:
                        rows = _smooth_rows(rows, smoothing)

                    # Always build fresh figures
                    fig1 = create_power_chart(rows, f"Power (last {hours}h)", visible=power_visible)
                    fig2 = create_soc_chart(rows)

                    # Price-based net cost/revenue chart (fixed to account for exports)
                    cfig = None
                    try:
                        buy, sell = get_current_prices_pln()
                        cum = 0.0
                        cts, cvals = [], []
                        pt = None
                        for r in rows:
                            t = datetime.fromisoformat(r["timestamp"]).astimezone()
                            gp = r.get("grid_power") or 0
                            if pt is not None:
                                dt = max(0.0, (t - pt).total_seconds() / 3600.0)
                                if gp > 0.001:
                                    cum += gp * dt * buy
                                elif gp < -0.001:
                                    # Export: revenue reduces net cumulative cost
                                    cum -= (-gp) * dt * sell
                            cts.append(t)
                            cvals.append(round(cum, 4))
                            pt = t
                        if cvals:
                            grosze_vals = [int(round(v * 100)) for v in cvals]
                            cfig = go.Figure()
                            hover_text = [f"Net {v:.2f} PLN ({g:+d} gr)" for v, g in zip(cvals, grosze_vals)]
                            cfig.add_trace(go.Scatter(
                                x=cts, y=grosze_vals,
                                name="Net cumulative (grosze)",
                                line=dict(color="#f66", width=2.5),
                                hovertemplate="%{text}<extra></extra>",
                                text=hover_text
                            ))
                            gmin = min(grosze_vals) if grosze_vals else 0
                            gmax = max(grosze_vals) if grosze_vals else 1
                            y_range = [min(gmin - 5, 0), max(gmax + 5, 1)] if grosze_vals else [0, 1]
                            cfig.update_layout(
                                title=f"Est. Cumulative Net Grid Cost (buy {int(buy*100)} / sell {int(sell*100)} gr/kWh)",
                                height=200,
                                paper_bgcolor="#1e1e1e",
                                plot_bgcolor="#1e1e1e",
                                font=dict(color="#ddd", size=10, family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
                                margin=dict(t=25, b=5, l=40, r=10),
                                yaxis=dict(title="grosze (net)", gridcolor="#333", range=y_range),
                                xaxis=dict(gridcolor="#333"),
                                hoverlabel=dict(
                                    bgcolor="#1f2937",
                                    bordercolor="#374151",
                                    font=dict(color="#e5e7eb", size=11, family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif")
                                )
                            )
                    except Exception:
                        pass

                    # Create plots only once (first load), then update in place to avoid flicker
                    if plots["power"] is None:
                        chart_container.clear()
                        with chart_container:
                            plots["power"] = ui.plotly(fig1).classes("w-full")
                            plots["soc"] = ui.plotly(fig2).classes("w-full")
                            if cfig:
                                plots["cost"] = ui.plotly(cfig).classes("w-full")
                    else:
                        plots["power"].update_figure(fig1)
                        plots["soc"].update_figure(fig2)
                        if cfig and plots.get("cost"):
                            plots["cost"].update_figure(cfig)
                        elif cfig and not plots.get("cost"):
                            # rare case if cost appeared later
                            with chart_container:
                                plots["cost"] = ui.plotly(cfig).classes("w-full")

                    if not from_auto and auto_status:
                        auto_status.set_text(f"Auto-refreshing every {chart_refresh_interval}s — preset period active")
                except Exception:
                    # Guard against stale containers after navigation or timing
                    pass

            # TOP CONTROLS: period selectors + refresh period + apply + refresh
            with ui.row().classes("gap-2 mb-4 items-center flex-wrap"):
                for label, hours in ranges:
                    def make_handler(h=hours):
                        async def hnd():
                            current_range["hours"] = h
                            await load_and_render(h)
                            update_active_button(h)
                            if auto_status:
                                if charts_auto_refresh_enabled:
                                    auto_status.set_text(f"Auto-refreshing every {chart_refresh_interval}s — preset active")
                                else:
                                    auto_status.set_text("Auto-refresh PAUSED — use Refresh or range buttons to update")
                        return hnd
                    btn = ui.button(label, on_click=make_handler()).props("size=sm")
                    period_buttons[hours] = btn

                ui.separator().props("vertical").classes("mx-1 h-6")

                refresh_input = ui.select(
                    options=[2, 5, 10, 15, 25, 30, 60, 120],
                    value=chart_refresh_interval,
                    label="Auto-refresh (s)"
                ).props("dense outlined").classes("w-36")

                def apply_refresh_period():
                    global chart_refresh_interval
                    try:
                        val = int(refresh_input.value)
                        if val < 2:
                            val = 2
                        chart_refresh_interval = val
                        # persist the interval too
                        try:
                            c = load_config()
                            c["auto_refresh_interval"] = chart_refresh_interval
                            save_config(c)
                        except Exception:
                            pass
                        if auto_status:
                            auto_status.set_text(f"Auto-refreshing every {val}s — will take effect on next tick")
                    except Exception:
                        pass

                ui.button("Apply", on_click=apply_refresh_period).props("size=sm")

                def toggle_auto_refresh():
                    global charts_auto_refresh_enabled, last_chart_refresh_time
                    charts_auto_refresh_enabled = not charts_auto_refresh_enabled
                    # persist auto-refresh enabled state
                    try:
                        c = load_config()
                        c["auto_refresh_enabled"] = charts_auto_refresh_enabled
                        save_config(c)
                    except Exception:
                        pass
                    if pause_btn:
                        pause_btn.set_text("▶ Resume Auto-Refresh" if not charts_auto_refresh_enabled else "⏸ Pause Auto-Refresh")
                    if auto_status:
                        if charts_auto_refresh_enabled:
                            last_chart_refresh_time = time.monotonic()  # avoid immediate refresh on resume
                            auto_status.set_text(f"Auto-refreshing every {chart_refresh_interval}s — preset period active")
                        else:
                            auto_status.set_text("Auto-refresh PAUSED — use Refresh or range buttons to update")

                initial_pause_text = "▶ Resume Auto-Refresh" if not charts_auto_refresh_enabled else "⏸ Pause Auto-Refresh"
                pause_btn = ui.button(initial_pause_text, on_click=toggle_auto_refresh).props("size=sm outline color=primary")

                async def do_refresh():
                    await load_and_render(current_range["hours"])
                    if auto_status:
                        if charts_auto_refresh_enabled:
                            auto_status.set_text(f"Auto-refreshing every {chart_refresh_interval}s — preset active")
                        else:
                            auto_status.set_text("Auto-refresh PAUSED — use Refresh or range buttons to update")
                ui.button("Refresh Charts", on_click=do_refresh, icon="refresh").props("size=sm")

            def update_active_button(active_hours):
                for h, btn in period_buttons.items():
                    if h == active_hours:
                        btn.props("color=primary size=sm")  # filled blue like APPLY/RESUME
                    else:
                        btn.props("outline color=primary size=sm")
                        btn.classes("border-primary text-primary")  # blue border + blue text for unselected like PAUSE

            update_active_button(current_range["hours"])

            def update_smoothing_buttons(active_smoothing):
                for s, btn in smoothing_buttons.items():
                    if s == active_smoothing:
                        btn.props("color=primary size=sm")  # filled blue like APPLY/RESUME
                    else:
                        btn.props("outline color=primary size=sm")
                        btn.classes("border-primary text-primary")  # blue border + blue text for unselected like PAUSE

            # Status immediately after controls (still top area)
            auto_status = ui.label(f"Auto-refreshing every {chart_refresh_interval}s").classes("text-xs text-green-400 mb-2")

            # Power series toggles + smoothing buttons (on right)
            with ui.row().classes("w-full gap-2 mb-2 items-center flex-wrap"):
                ui.label("Power:").classes("text-xs text-gray-400 mr-1")
                for name, color in [("PV", "#ffc107"), ("Battery", "#4caf50"), ("Grid", "#2196f3"), ("Load", "#ff5722")]:
                    def make_toggle(n=name, c=color):
                        async def on_change(e):
                            power_visible[n] = e.value
                            cfg = load_config()
                            cfg["power_visible"] = power_visible
                            save_config(cfg)
                            await load_and_render(current_range["hours"])
                        ui.html(f'<span style="color:{c}; font-weight:bold; margin-right:1px;">●</span>')
                        ui.switch(n, value=power_visible.get(n, True), on_change=on_change).props("dense size=sm")
                    make_toggle()

                # Smoothing buttons on the right
                with ui.row().classes("ml-auto gap-1"):
                    smoothing_options = [
                        ("No smoothing", 0),
                        ("3 last", 3),
                        ("5 last", 5),
                    ]
                    for label, s in smoothing_options:
                        def make_s_handler(sm=s):
                            async def hnd():
                                global smoothing
                                smoothing = sm
                                cfg = load_config()
                                cfg["smoothing"] = smoothing
                                save_config(cfg)
                                await load_and_render(current_range["hours"])
                                update_smoothing_buttons(sm)
                            return hnd
                        btn = ui.button(label, on_click=make_s_handler()).props("size=sm")
                        smoothing_buttons[s] = btn

            update_smoothing_buttons(smoothing)

            # chart_container created here so plots appear below the top controls+status
            chart_container = ui.column().classes("w-full gap-6")

            # Auto refresh timer (base tick every 2s so we support down to 2s refresh)
            # Created only once
            global _charts_auto_timer_started
            if not _charts_auto_timer_started:
                async def charts_auto_tick():
                    global last_chart_refresh_time, current_range, auto_status
                    now = time.monotonic()
                    if charts_auto_refresh_enabled and (now - last_chart_refresh_time >= chart_refresh_interval):
                        last_chart_refresh_time = now
                        try:
                            await load_and_render(current_range["hours"], from_auto=True)
                            if auto_status and charts_auto_refresh_enabled:
                                auto_status.set_text(f"Auto-refreshing every {chart_refresh_interval}s • updated {datetime.now().strftime('%H:%M:%S')}")
                        except Exception:
                            pass  # protect against deleted slots on nav
                ui.timer(2, charts_auto_tick)  # check base every 2s
                _charts_auto_timer_started = True
                last_chart_refresh_time = time.monotonic()

            # initial load
            await load_and_render(current_range["hours"])


async def show_summary():
    global main_content, on_dashboard
    on_dashboard = False
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        with ui.column().classes("w-full p-4 gap-6"):
            ui.label("Energy Summaries").classes("text-3xl font-bold")

            periods = [
                ("Today", 0),
                ("Yesterday", 1),
                ("This Week", 7),
                ("This Month", 30),
            ]

            summary_container = ui.column().classes("w-full gap-4")

            async def load_all():
                now = datetime.now(timezone.utc)
                summaries = []
                for name, days in periods:
                    if days == 0:
                        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    elif days == 1:
                        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    else:
                        start = now - timedelta(days=days)
                        end = now

                    if days == 1:
                        summary = await get_summary(start, end)
                    else:
                        summary = await get_summary(start, now)
                    summaries.append((name, summary))

                buy_pln, sell_pln = get_current_prices_pln()
                summary_container.clear()
                with summary_container:
                    for name, summary in summaries:
                        with ui.card().classes("w-full p-4 bg-[#1f2937]"):
                            ui.label(name).classes("text-lg font-semibold mb-2 text-cyan-400")
                            with ui.row().classes("gap-6 flex-wrap text-sm"):
                                ui.label(f"☀️ PV: {summary['pv']:.2f} kWh")
                                ui.label(f"🔋 Bat used: {summary['battery_discharge']:.2f} kWh")
                                ui.label(f"⬇️ Grid in: {summary['grid_import']:.2f} kWh")
                                ui.label(f"⬆️ Grid out: {summary['grid_export']:.2f} kWh")
                                ui.label(f"🏠 Load: {summary['load']:.2f} kWh")
                                ui.label(f"♻️ Self: {summary['self_consumption_pct']:.0f}%")
                            cost = round(summary['grid_import'] * buy_pln, 2)
                            revenue = round(summary['grid_export'] * sell_pln, 2)
                            net = round(cost - revenue, 2)
                            ui.label(f"💰 Est. cost {cost:.2f} PLN • revenue {revenue:.2f} PLN • net {net:+.2f} PLN").classes("text-xs text-gray-400 mt-1")

            await load_all()


async def show_raw_data():
    global main_content, on_dashboard
    on_dashboard = False
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        with ui.column().classes("w-full p-4"):
            ui.label("Raw Measurements").classes("text-3xl font-bold mb-2")

            table_container = ui.column().classes("w-full")

            async def load_table():
                rows = await get_recent_raw(300)
                table_container.clear()
                if not rows:
                    with table_container:
                        ui.label("No data yet.").classes("text-gray-400")
                    return

                columns = [
                    {"name": "timestamp", "label": "Timestamp", "field": "timestamp", "sortable": True},
                    {"name": "soc", "label": "SOC %", "field": "soc"},
                    {"name": "pv_power", "label": "PV kW", "field": "pv_power"},
                    {"name": "battery_power", "label": "Battery kW", "field": "battery_power"},
                    {"name": "grid_power", "label": "Grid kW", "field": "grid_power"},
                    {"name": "load_power", "label": "Load kW", "field": "load_power"},
                    {"name": "grid_status", "label": "Status", "field": "grid_status"},
                ]

                formatted = []
                for r in rows:
                    fr = {k: r.get(k) for k in ["timestamp", "soc", "pv_power", "battery_power", "grid_power", "load_power", "grid_status"]}
                    if fr.get("timestamp"):
                        try:
                            fr["timestamp"] = datetime.fromisoformat(fr["timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass
                    formatted.append(fr)

                with table_container:
                    ui.table(columns=columns, rows=formatted, row_key="timestamp", pagination=25).classes("w-full")

            async def do_export_csv():
                rows = await get_recent_raw(10000)
                if not rows:
                    ui.notify("No data to export", type="warning")
                    return
                import csv
                from io import StringIO
                output = StringIO()
                writer = csv.DictWriter(output, fieldnames=["timestamp", "soc", "pv_power", "battery_power", "grid_power", "load_power", "grid_status"])
                writer.writeheader()
                for r in rows:
                    writer.writerow({k: r.get(k) for k in writer.fieldnames})
                csv_data = output.getvalue()
                ui.download(csv_data.encode("utf-8"), "sigenstor_data.csv", "text/csv")
                ui.notify("CSV exported", type="positive")

            # button uses the async directly (registered in async show context)
            # (the previous button line already references export_csv - we will override below if needed)

            with ui.row().classes("gap-2 mb-3"):
                ui.button("Export CSV", on_click=do_export_csv, icon="download").props("color=primary")
                ui.button("Refresh", on_click=lambda: asyncio.create_task(load_table()), icon="refresh")

            await load_table()


async def show_maintenance():
    """UI section for aggregation task history, status, and monitoring."""
    global main_content, on_dashboard
    on_dashboard = False
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        with ui.column().classes("w-full p-4 gap-6"):
            ui.label("Maintenance & Aggregation History").classes("text-3xl font-bold")

            # Status header
            status_container = ui.row().classes("gap-4 items-center flex-wrap")
            runs_container = ui.column().classes("w-full")
            chart_container = ui.column().classes("w-full")

            async def load_maintenance_data():
                runs = await get_aggregation_runs(100)
                # Status
                status_container.clear()
                with status_container:
                    if runs:
                        last = runs[0]
                        last_time = last.get("run_timestamp", "")
                        try:
                            last_dt = datetime.fromisoformat(last_time.replace("Z", "+00:00")).astimezone()
                            last_str = last_dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            last_str = last_time
                        duration = last.get("duration_seconds", 0) or 0
                        processed = last.get("rows_processed", 0) or 0
                        buckets = last.get("buckets_created", 0) or 0
                        stat = last.get("status", "success")

                        ui.badge(f"Last run: {last_str}", color="primary")
                        ui.badge(f"Duration: {duration:.2f}s", color="secondary")
                        ui.badge(f"Rows: {processed}", color="info")
                        ui.badge(f"Buckets: {buckets}", color="info")
                        color = "positive" if stat == "success" else "negative"
                        ui.badge(f"Status: {stat}", color=color)

                        # Simple health check: last run < 2 hours ago?
                        try:
                            last_dt2 = datetime.fromisoformat(last_time.replace("Z", "+00:00")).astimezone()
                            age_h = (datetime.now(timezone.utc) - last_dt2).total_seconds() / 3600
                            health = "Healthy" if age_h < 2 else "Stale"
                            hcolor = "positive" if age_h < 2 else "warning"
                            ui.badge(f"Health: {health} (age {age_h:.1f}h)", color=hcolor)
                        except Exception:
                            pass
                    else:
                        ui.badge("No runs recorded yet", color="warning")

                # Table
                runs_container.clear()
                with runs_container:
                    ui.label("Recent Aggregation Runs").classes("text-xl font-semibold mt-4")
                    if not runs:
                        ui.label("No aggregation runs logged yet.").classes("text-gray-400")
                        return

                    columns = [
                        {"name": "run_timestamp", "label": "Run Time", "field": "run_timestamp", "sortable": True},
                        {"name": "duration_seconds", "label": "Duration (s)", "field": "duration_seconds"},
                        {"name": "rows_processed", "label": "Rows Processed", "field": "rows_processed"},
                        {"name": "buckets_created", "label": "30s Buckets", "field": "buckets_created"},
                        {"name": "status", "label": "Status", "field": "status"},
                        {"name": "error_message", "label": "Error", "field": "error_message"},
                    ]

                    formatted = []
                    for r in runs:
                        fr = {
                            "run_timestamp": r.get("run_timestamp"),
                            "duration_seconds": r.get("duration_seconds"),
                            "rows_processed": r.get("rows_processed"),
                            "buckets_created": r.get("buckets_created"),
                            "status": r.get("status"),
                            "error_message": r.get("error_message") or "",
                        }
                        if fr.get("run_timestamp"):
                            try:
                                dt = datetime.fromisoformat(fr["run_timestamp"].replace("Z", "+00:00")).astimezone()
                                fr["run_timestamp"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                            except Exception:
                                pass
                        formatted.append(fr)

                    ui.table(columns=columns, rows=formatted, row_key="run_timestamp", pagination=20).classes("w-full")

                # Chart
                chart_container.clear()
                with chart_container:
                    ui.label("Aggregation Activity Over Time").classes("text-xl font-semibold mt-4")
                    if len(runs) < 2:
                        ui.label("Need more runs for a chart.").classes("text-gray-400")
                        return

                    # reverse for chronological
                    runs_sorted = sorted(runs, key=lambda x: x.get("run_timestamp", ""))
                    try:
                        times = []
                        rows_p = []
                        buckets_c = []
                        for r in runs_sorted:
                            ts = r.get("run_timestamp")
                            if ts:
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                                times.append(dt)
                            else:
                                times.append(None)
                            rows_p.append(r.get("rows_processed") or 0)
                            buckets_c.append(r.get("buckets_created") or 0)

                        fig = go.Figure()
                        fig.add_trace(go.Scatter(
                            x=times, y=rows_p, name="Rows Processed",
                            mode="lines+markers", line=dict(color="#2196f3")
                        ))
                        fig.add_trace(go.Scatter(
                            x=times, y=buckets_c, name="Buckets Created",
                            mode="lines+markers", line=dict(color="#4caf50")
                        ))
                        fig.update_layout(
                            title="Aggregation Runs (rows & buckets)",
                            xaxis_title="Time",
                            yaxis_title="Count",
                            height=280,
                            paper_bgcolor="#1e1e1e",
                            plot_bgcolor="#1e1e1e",
                            font=dict(color="#ddd", size=11),
                            margin=dict(t=30, b=10, l=50, r=10),
                            legend=dict(orientation="h", y=-0.2)
                        )
                        ui.plotly(fig).classes("w-full")
                    except Exception as chart_err:
                        ui.label(f"Chart error: {chart_err}").classes("text-red-400")

            # Controls
            with ui.row().classes("gap-2 mt-2"):
                ui.button("Refresh", on_click=lambda: asyncio.create_task(load_maintenance_data()), icon="refresh").props("size=sm")
                async def force_run():
                    ui.notify("Forcing aggregation run...", type="info")
                    try:
                        n = await aggregate_old_data()
                        ui.notify(f"Aggregation completed. Processed {n} rows.", type="positive")
                        await load_maintenance_data()
                    except Exception as e:
                        ui.notify(f"Force run failed: {e}", type="negative")
                ui.button("Force Run Now", on_click=force_run, icon="play_arrow").props("size=sm outline")

            await load_maintenance_data()


def show_settings():
    global main_content, on_dashboard
    on_dashboard = False
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        with ui.column().classes("w-full p-4 max-w-[520px]"):
            ui.label("Settings").classes("text-3xl font-bold mb-4")

            cfg = load_config()

            ip = ui.input("SigenStor IP", value=cfg["ip"]).props("filled")
            port = ui.number("Port", value=cfg["port"], min=1, max=65535).props("filled")
            slave = ui.number("Slave ID", value=cfg["slave_id"], min=1, max=247).props("filled")
            interval = ui.number("Poll interval (seconds)", value=cfg["poll_interval"], min=2, max=300).props("filled")

            buy_price = ui.number("Buy price (grosze/kWh)", value=cfg.get("buy_price_grosze", 75), min=0, step=1).props("filled")
            sell_price = ui.number("Sell price (grosze/kWh)", value=cfg.get("sell_price_grosze", 35), min=0, step=1).props("filled")
            bat_cap = ui.number("Battery capacity (kWh)", value=cfg.get("battery_capacity_kwh", 18.0), min=0, step=0.1).props("filled")

            async def test_conn():
                test_client = SigenModbusClient(ip.value, int(port.value), int(slave.value))
                ok, msg = await test_client.test_connection()
                if ok:
                    ui.notify(msg, type="positive", position="top")
                else:
                    ui.notify(msg, type="negative", position="top")

            async def save_and_apply():
                cfg = load_config()
                cfg["ip"] = ip.value.strip()
                cfg["port"] = int(port.value)
                cfg["slave_id"] = int(slave.value)
                cfg["poll_interval"] = int(interval.value)
                cfg["buy_price_grosze"] = int(buy_price.value)
                cfg["sell_price_grosze"] = int(sell_price.value)
                cfg["battery_capacity_kwh"] = float(bat_cap.value)
                cfg["enabled"] = True
                save_config(cfg)
                ui.notify("Config saved. Restarting poller...", type="info")
                await stop_poller()
                await start_poller()
                ui.notify("Poller restarted with new settings", type="positive")

            with ui.row().classes("gap-2 mt-4"):
                ui.button("Test Connection", on_click=test_conn, icon="network_check").props("color=secondary")
                ui.button("Save Config", on_click=save_and_apply, icon="save").props("color=primary")

            ui.separator().classes("my-6")

            ui.label("Modbus Register Map (read-only, extendable in code)").classes("font-semibold")
            with ui.element("pre").classes("text-xs bg-[#111827] p-3 rounded overflow-auto"):
                ui.html("<code>" + json.dumps(REGISTERS, indent=2) + "</code>")

            ui.label("Tip: Edit REGISTERS dict in main.py to add more values (PV strings, temps, etc).").classes("text-xs text-gray-400 mt-1")


# =============================================================================
# APP STARTUP
# =============================================================================

@app.on_startup
async def on_startup():
    logger.info("Starting SigenStor Dashboard...")
    await init_db()
    await start_poller()
    asyncio.create_task(monitor_maintenance_task())

    # Seed a demo reading if DB empty (helpful for first run without hardware)
    latest = await get_latest_reading()
    if not latest:
        demo = Reading(
            timestamp=datetime.now(timezone.utc).isoformat(),
            soc=87.3,
            pv_power=4.82,
            battery_power=-1.35,
            grid_power=-0.72,
            load_power=3.19,
            grid_status=0,
        )
        await insert_reading(demo)
        global latest_reading
        latest_reading = demo
        logger.info("Seeded demo data (will be overwritten on real poll)")


@app.on_shutdown
async def on_shutdown():
    await stop_poller()
    logger.info("Shutdown complete")


# =============================================================================
# MAIN
# =============================================================================

if __name__ in {"__main__", "__mp_main__"}:
    # Global nicer fonts (modern sans, similar to premium UIs)
    ui.add_head_html('''
    <style>
    :root {
      --app-font: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    body, .q-card, .q-field, .q-item, .nicegui-plotly, .q-btn, .q-tab {
      font-family: var(--app-font) !important;
    }
    .value-num {
      font-feature-settings: "tnum" !important;
      letter-spacing: -0.02em;
    }
    .section-title {
      font-weight: 600;
      letter-spacing: -0.01em;
    }
    </style>
    ''')

    # Build initial UI at top-level script execution time (required for correct NiceGUI slot context)
    build_sidebar()
    main_content = ui.column().classes("w-full")
    show_dashboard()

    # Create live update timers only once at startup (prevents "parent slot deleted" errors
    # when navigating between pages, as timers created inside show_dashboard would be
    # children of main_content and get deleted on clear()).
    ui.timer(3.0, update_live_dashboard)
    ui.timer(0.6, update_live_dashboard, once=True)

    # Run the NiceGUI app
    ui.run(
        title=APP_TITLE,
        dark=True,
        host="0.0.0.0",
        port=8080,
        reload=False,   # set True during development if desired
        show=True,
        favicon="🔋",
    )
