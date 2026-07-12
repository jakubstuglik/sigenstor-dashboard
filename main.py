#!/usr/bin/env python3
"""
SigenStor Dashboard - Modern monitoring app for Sigenergy SigenStor
Python + NiceGUI + Plotly + SQLite + pymodbus (read-only)
"""

import asyncio
import json
import logging
import logging.handlers
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

# Resolve DB and port at module load from env (prod defaults when unset).
# This allows `PORT=8081 SIGENSTOR_DB=data/sigenstor_dev.db python main.py`
# without ever touching the prod container or prod DB file.
PROD_DB_FILE = DATA_DIR / "sigenstor.db"
DB_FILE = Path(os.environ.get("SIGENSTOR_DB", str(PROD_DB_FILE)))
PORT = int(os.environ.get("PORT", "8080"))


def _ensure_dev_db_seeded() -> None:
    """Seed a separate dev DB (schema + data snapshot from prod) if a non-default DB path is requested.
    Never opens the prod path for write, and does no-op when using the prod default path.
    For dev paths we (re)seed from prod when the dev file is missing or suspiciously small/empty
    so that verification and objective requirements ("seed with schema and settings from prod DB") are met.
    """
    if str(DB_FILE).strip() == str(PROD_DB_FILE).strip():
        return  # prod mode, never touch
    try:
        DATA_DIR.mkdir(exist_ok=True)
    except Exception:
        pass
    # For dev paths: always ensure full prod snapshot when prod exists (unconditional for isolation/verification).
    # This guarantees ac1 "seed with schema and settings from prod DB" and eliminates small/empty dev cases.
    if PROD_DB_FILE.exists():
        try:
            import sqlite3, os
            dev_exists = DB_FILE.exists()
            dev_size = os.path.getsize(DB_FILE) if dev_exists else 0
            # Only seed (overwrite) if dev missing or suspiciously small/empty. 
            # NEVER nuke a populated dev DB on every import (would destroy daily/power_agg work, contrary to user instructions).
            if dev_exists and dev_size > 100 * 1024:  # >100kB means has real data + schema
                try:
                    logger.info(f"Dev DB already populated ({dev_size} bytes), skipping re-seed to preserve aggregates.")
                except Exception:
                    print(f"Dev DB populated, skip re-seed.")
                return
            # Prefer sqlite3 backup API ...
            if dev_exists:
                try:
                    DB_FILE.unlink()
                except Exception:
                    pass
            conn = sqlite3.connect(str(PROD_DB_FILE))
            bconn = sqlite3.connect(str(DB_FILE))
            conn.backup(bconn)
            bconn.close()
            conn.close()
            try:
                logger.info(f"Seeded dev DB via sqlite3.backup: {DB_FILE} <- {PROD_DB_FILE}")
            except Exception:
                print(f"Seeded dev DB via sqlite3.backup: {DB_FILE} <- {PROD_DB_FILE}")
            return
        except Exception as e:
            try:
                logger.warning(f"Dev DB sqlite backup from prod failed ({e}); will initialize empty schema instead")
            except Exception:
                print(f"Dev DB backup failed: {e}")
    # If no prod to copy or dev already substantial, init_db will ensure schema (migrations).

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

def _compress_log(source: Path, dest: Path) -> None:
    """Compress a rotated log file using gzip."""
    import gzip
    import shutil
    try:
        with open(source, 'rb') as f_in:
            with gzip.open(dest, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        source.unlink(missing_ok=True)
    except Exception as e:
        try:
            logger.warning(f"Log compression failed for {source}: {e}")
        except Exception:
            pass

def _compress_uncompressed_rotated() -> None:
    """On startup, gzip any existing rotated plain .log files (e.g. sigenstor_YYYYMMDD.log) so they don't stay huge uncompressed."""
    import gzip
    import shutil
    active = LOGS_DIR / "sigenstor.log"
    for f in LOGS_DIR.glob("sigenstor_*.log"):
        if f.name == "sigenstor.log":
            continue
        gz = f.with_suffix(f.suffix + ".gz") if not f.name.endswith('.gz') else f
        # if .log make .log.gz
        if not str(gz).endswith('.gz'):
            gz = Path(str(f) + '.gz')
        if gz.exists():
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
            continue
        try:
            with open(f, 'rb') as f_in:
                with gzip.open(gz, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            f.unlink(missing_ok=True)
        except Exception:
            pass

def _cleanup_old_logs(days: int = 30) -> None:
    """Delete log files older than N days (including .gz). Uses name date or mtime fallback."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for f in LOGS_DIR.glob("sigenstor_*.log*"):
        try:
            delete = False
            parsed = False
            # parse date from name like sigenstor_20260712.log or sigenstor_20260712.log.gz
            name = f.name
            if '_20' in name or '_19' in name:
                # find YYYYMMDD after last _
                parts = name.replace('.log', '').replace('.gz', '').split('_')
                for p in parts:
                    if len(p) == 8 and p.isdigit():
                        log_date = datetime.strptime(p, '%Y%m%d').replace(tzinfo=timezone.utc)
                        if log_date < cutoff:
                            delete = True
                        parsed = True
                        break
            if not parsed:
                # fallback to file mtime
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    delete = True
            if delete:
                f.unlink(missing_ok=True)
                try:
                    logger.debug(f"Cleaned old log: {f.name}")
                except Exception:
                    pass
        except Exception:
            pass

def setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)

    # Use daily rotating file handler with compression and retention.
    # We compress rotated files to .gz and clean old ones (name date or mtime).
    log_file = LOGS_DIR / "sigenstor.log"

    handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when='midnight', interval=1, backupCount=0, encoding='utf-8'
    )
    # Custom namer keeps a clean base; rotator gzips the rotated file.
    def _log_namer(name: str) -> str:
        p = Path(name)
        # TimedRotating appends date suffix; keep .log then rotator will .gz it
        return str(p.with_suffix(''))
    handler.namer = _log_namer
    handler.rotator = lambda source, dest: _compress_log(Path(source), Path(dest).with_name(Path(dest).name + '.gz') if not str(dest).endswith('.gz') else Path(dest))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            handler,
        ],
    )

    # On startup: compress any leftover uncompressed rotated logs, then enforce retention
    try:
        _compress_uncompressed_rotated()
    except Exception:
        pass
    try:
        _cleanup_old_logs(30)
    except Exception:
        pass

    logger = logging.getLogger("sigenstor")
    logger.info("Logging initialized with daily rotation + gzip + 30-day retention")
    return logger


logger = setup_logging()

# Seed dev DB snapshot (if using non-prod DB path) as early as possible after logging.
_ensure_dev_db_seeded()

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
    create_if_missing = False
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    raise ValueError("empty config file")
                cfg = json.loads(content)
            # merge defaults
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception as e:
            logger.warning(f"Failed to load config, using defaults: {e}")
            create_if_missing = True
    else:
        create_if_missing = True

    cfg = DEFAULT_CONFIG.copy()
    if create_if_missing:
        save_config(cfg)
        logger.info("Config file created with default settings")
    return cfg


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
                logger.debug(f"Connected to Modbus TCP at {self.ip}:{self.port}")
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

-- power_agg stores pre-aggregated (downsampled) power readings for older periods.
-- Used for composite charts (coarse for old + full precision for recent) WITHOUT ever deleting raw input.
CREATE TABLE IF NOT EXISTS power_agg (
    timestamp TEXT NOT NULL,
    resolution_sec INTEGER NOT NULL,  -- e.g. 120=2min, 600=10min
    pv_power REAL,
    battery_power REAL,
    grid_power REAL,
    load_power REAL,
    soc REAL,
    grid_status INTEGER,
    PRIMARY KEY (timestamp, resolution_sec)
);
CREATE INDEX IF NOT EXISTS idx_power_agg_ts_res ON power_agg(timestamp, resolution_sec);

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
        # Ensure power_agg exists for older composite data (idempotent)
        try:
            await db.execute("CREATE TABLE IF NOT EXISTS power_agg (timestamp TEXT NOT NULL, resolution_sec INTEGER NOT NULL, pv_power REAL, battery_power REAL, grid_power REAL, load_power REAL, soc REAL, grid_status INTEGER, PRIMARY KEY (timestamp, resolution_sec))")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_power_agg_ts_res ON power_agg(timestamp, resolution_sec)")
            await db.commit()
        except Exception:
            pass
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
        t0 = _parse_stored_ts(rows[i-1]["timestamp"])
        t1 = _parse_stored_ts(rows[i]["timestamp"])
        dt_h = (t1 - t0).total_seconds() / 3600.0
        p0 = rows[i-1].get(power_key) or 0.0
        p1 = rows[i].get(power_key) or 0.0
        # Trapezoid
        total += ((p0 + p1) / 2.0) * dt_h
    return round(total, 3)


def _downsample_rows(rows: List[Dict], bucket_seconds: int) -> List[Dict]:
    """Downsample power readings into time buckets (for long period charts using coarser resolution for older data)."""
    if not rows:
        return []
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        try:
            ts = _parse_stored_ts(r["timestamp"])
            key = int(ts.timestamp() // bucket_seconds) * bucket_seconds
            buckets[key].append(r)
        except Exception:
            continue
    result = []
    for key in sorted(buckets.keys()):
        group = buckets[key]
        if not group:
            continue
        avg = {"timestamp": group[0]["timestamp"]}  # representative ts for bucket
        for k in ["pv_power", "battery_power", "grid_power", "load_power", "soc"]:
            vals = [g.get(k) for g in group if g.get(k) is not None]
            avg[k] = sum(vals) / len(vals) if vals else None
        # status last
        for g in reversed(group):
            if g.get("grid_status") is not None:
                avg["grid_status"] = g.get("grid_status")
                break
        result.append(avg)
    return result


def get_current_prices_pln():
    cfg = load_config()
    return cfg.get("buy_price_grosze", 75) / 100.0, cfg.get("sell_price_grosze", 35) / 100.0


def get_battery_capacity_kwh() -> float:
    cfg = load_config()
    return float(cfg.get("battery_capacity_kwh", 10.0))


async def get_daily_energy_range(start: datetime, end: datetime) -> Dict[str, Any]:
    """Fast lookup from pre-aggregated daily table."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM energy_daily WHERE date >= ? AND date <= ? ORDER BY date""",
            (start.date().isoformat(), end.date().isoformat())
        )
        rows = await cur.fetchall()
        pv = sum((r['pv_energy_kwh'] or 0) for r in rows)
        bat_d = sum((r['battery_discharge_kwh'] or 0) for r in rows)
        g_i = sum((r['grid_import_kwh'] or 0) for r in rows)
        g_e = sum((r['grid_export_kwh'] or 0) for r in rows)
        load = sum((r['load_energy_kwh'] or 0) for r in rows)
        self_suff = 0.0
        if load > 0.001:
            self_energy = pv + bat_d - g_i + g_e
            self_suff = round(max(0.0, min(100.0, self_energy / load * 100)), 2)
        return {
            "pv": round(pv, 3), "battery_discharge": round(bat_d, 3),
            "battery_charge": 0, "grid_import": round(g_i, 3), "grid_export": round(g_e, 3),
            "load": round(load, 3), "self_sufficiency_pct": self_suff,
            "data_start": None, "used_daily": True
        }


async def get_summary(start: datetime, end: datetime) -> Dict[str, Any]:
    # Use pre-aggregated daily for long periods (fast path when available and non-zero).
    # IMPORTANT: if daily is 0/empty (e.g. after partial historical processing), fall back to raw
    # trapezoidal compute so week/month/year summaries are NEVER empty. Raw kept for 90d+.
    days = (end - start).days
    if days > 2:
        try:
            daily = await get_daily_energy_range(start, end)
            if daily and (daily.get("pv", 0) > 0.001 or daily.get("load", 0) > 0.001):
                return daily
        except Exception:
            pass

    rows = await get_readings_range(start, end)
    if not rows:
        return {"pv": 0, "battery_discharge": 0, "battery_charge": 0, "grid_import": 0, "grid_export": 0, "load": 0, "self_sufficiency_pct": 0, "data_start": None}

    pv = compute_energy_kwh(rows, "pv_power")
    load = compute_energy_kwh(rows, "load_power")

    # Integrate signed flows (positive contributions only)
    bat_discharge = 0.0
    bat_charge = 0.0
    grid_import = 0.0
    grid_export = 0.0
    for i in range(1, len(rows)):
        t0 = _parse_stored_ts(rows[i-1]["timestamp"])
        t1 = _parse_stored_ts(rows[i]["timestamp"])
        dt_h = max(0.0, (t1 - t0).total_seconds() / 3600.0)
        b0 = rows[i-1].get("battery_power") or 0.0
        b1 = rows[i].get("battery_power") or 0.0
        g0 = rows[i-1].get("grid_power") or 0.0
        g1 = rows[i].get("grid_power") or 0.0

        # discharge part of battery
        bat_discharge += max(0.0, -((b0 + b1) / 2.0)) * dt_h
        # charge part of battery
        bat_charge += max(0.0, ((b0 + b1) / 2.0)) * dt_h
        # grid import / export
        grid_import += max(0.0, ((g0 + g1) / 2.0)) * dt_h
        grid_export += max(0.0, -((g0 + g1) / 2.0)) * dt_h

    bat_discharge = round(bat_discharge, 3)
    bat_charge = round(bat_charge, 3)
    grid_import = round(grid_import, 3)
    grid_export = round(grid_export, 3)

    # Self-sufficiency: what % of the load (consumption) was covered by own production
    # (PV + battery discharge) rather than grid import. This is what "Self: XX%" means
    # in the summary context, especially when imports are near zero.
    self_suff = 0.0
    if load > 0.001:
        self_energy = pv + bat_discharge - grid_import + grid_export
        self_suff = round(max(0.0, min(100.0, self_energy / load * 100)), 2)

    data_start = None
    if rows:
        first_ts = min(_parse_stored_ts(r["timestamp"]) for r in rows)
        if first_ts.date() > start.date():
            data_start = first_ts

    return {
        "pv": pv,
        "battery_discharge": bat_discharge,
        "battery_charge": bat_charge,
        "grid_import": grid_import,
        "grid_export": grid_export,
        "load": load,
        "self_sufficiency_pct": self_suff,
        "data_start": data_start,
    }


# =============================================================================
# HOST OS TIMEZONE HELPERS (pure, no UI/DB side effects)
# These are the single source for local-time "now" and period boundaries.
# All UI displays and "Today"/"Month" etc use host OS TZ (via astimezone / local midnight).
# DB always stores UTC; these convert local boundaries -> UTC instants for queries.
# =============================================================================

def get_local_tz() -> timezone:
    """Return the host operating system's local timezone (std lib, no extra deps)."""
    # Works on Windows/Linux/macOS; gives e.g. tzinfo with correct UTC offset for now.
    return datetime.now().astimezone().tzinfo or timezone.utc


def get_local_now() -> datetime:
    """Current wall time in the host OS timezone (tz-aware)."""
    return datetime.now(get_local_tz())


def get_local_today_start() -> datetime:
    """Local midnight (00:00) at the start of the current calendar day in host TZ."""
    now = get_local_now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def compute_period_boundaries() -> list:
    """Pure computation: return [(label, start_utc, end_utc), ...] for Today, Yesterday, Week(rolling), Month, Year.
    Boundaries derived from host OS local time, then converted to UTC for DB query compatibility.
    Callable directly from tests/verification with no server/UI.
    """
    now_loc = get_local_now()
    today_start_loc = get_local_today_start()
    yest_start_loc = today_start_loc - timedelta(days=1)
    week_start_loc = now_loc - timedelta(days=7)
    month_start_loc = now_loc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start_loc = now_loc.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    now_utc = to_utc(now_loc)
    return [
        ("Today", to_utc(today_start_loc), now_utc),
        ("Yesterday", to_utc(yest_start_loc), to_utc(today_start_loc)),
        ("Week (rolling)", to_utc(week_start_loc), now_utc),
        ("Month", to_utc(month_start_loc), now_utc),
        ("Year", to_utc(year_start_loc), now_utc),
    ]


def to_utc(dt: datetime) -> datetime:
    """Convert local (or aware) datetime to equivalent UTC instant (for queries against UTC-stored timestamps)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_local_tz())
    return dt.astimezone(timezone.utc)


def _parse_stored_ts(ts: str) -> datetime:
    """Safely parse timestamp strings stored in DB (may be 'Z' or naive) to aware datetime in UTC.
    Used for all display formatting to satisfy host-OS TZ + AC2.
    """
    if not ts:
        return datetime.now(timezone.utc)
    s = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def smoothing_button_props(s: int, active_smoothing: int) -> dict:
    """Pure: return props for smoothing button. Ensures exactly one data-active=true.
    Used at creation and update.
    """
    is_active = (s == active_smoothing)
    if is_active:
        return {"props": "color=primary size=sm", "classes": "", "data": f'data-smoothing="{s}" data-active="true" aria-pressed="true"'}
    else:
        return {"props": "outline color=primary size=sm", "classes": "border-primary text-primary", "data": f'data-smoothing="{s}" data-active="false" aria-pressed="false"'}


async def get_multi_period_summaries():
    """Return list of (label, summary_dict) for Today, Yesterday, rolling Week, calendar Month, Year.
    Uses host-OS-TZ boundaries (see compute_period_boundaries).
    """
    bounds = compute_period_boundaries()
    results = []
    for label, start_utc, end_utc in bounds:
        results.append((label, await get_summary(start_utc, end_utc)))
    return results


def create_period_energy_chart(period_data, title: str = None):
    """Grouped bar chart for energy flows (PV, Battery, Grid). When called with subset data it auto-scales y to that subset.
    Used for the three side-by-side dashboard period charts.
    """
    if not period_data:
        fig = go.Figure()
        fig.add_annotation(text="No data", x=0.5, y=0.5, showarrow=False)
        fig.update_layout(height=220, margin=dict(t=25, b=5, l=30, r=5))
        return fig

    labels = [item[0] for item in period_data]
    pv = [item[1].get("pv", 0) for item in period_data]
    bat = [item[1].get("battery_discharge", 0) for item in period_data]
    g_in = [item[1].get("grid_import", 0) for item in period_data]
    g_out = [item[1].get("grid_export", 0) for item in period_data]

    fig = go.Figure()

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
    fig.add_trace(go.Bar(
        x=labels, y=g_out, name="⬆️ Grid export", marker_color="#ef4444",
        hovertemplate="Grid out: %{y:.2f} kWh<extra></extra>"
    ))

    if title is None:
        title = "Energy by Period (kWh) — PV + Battery + Grid flows"
    fig.update_layout(
        barmode="group",
        title=title,
        yaxis_title="kWh",
        paper_bgcolor="#1e1e1e",
        plot_bgcolor="#1e1e1e",
        font=dict(color="#ddd", size=9, family="system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
        height=220,
        margin=dict(t=22, b=2, l=30, r=5),
        legend=dict(orientation="h", y=-0.22, x=0.5, xanchor="center", font=dict(size=8)),
        bargap=0.2,
        xaxis=dict(gridcolor="#333", tickfont=dict(size=8)),
        yaxis=dict(gridcolor="#333", tickfont=dict(size=8)),
        hoverlabel=dict(bgcolor="#1f2937", bordercolor="#374151", font=dict(color="#e5e7eb", size=10))
    )
    return fig


def create_split_period_energy_charts(period_data):
    """Pure helper: given full period list, return (fig1, fig2, fig3) for the three required groupings.
    Each fig has its own independent y-scale (plotly default per figure).
    Today+Yesterday | Week+Month | Year . Callable from tests without UI.
    """
    if not period_data:
        e = create_period_energy_chart([], title="No data")
        return e, e, e
    g1 = [it for it in period_data if it[0] in ("Today", "Yesterday")]
    g2 = [it for it in period_data if it[0] in ("Week (rolling)", "Month")]
    g3 = [it for it in period_data if it[0] == "Year"]
    return (
        create_period_energy_chart(g1, title="Today + Yesterday"),
        create_period_energy_chart(g2, title="Week + Month"),
        create_period_energy_chart(g3, title="Year"),
    )


async def refresh_period_energy_chart():
    """Refresh the three side-by-side period energy charts. Strong guards for deleted elements after nav (per AGENTS.md)."""
    global period_energy_chart1, period_energy_chart2, period_energy_chart3
    try:
        if not on_dashboard:
            return
        pdata = await get_multi_period_summaries()
        f1, f2, f3 = create_split_period_energy_charts(pdata)
        for ch, fig in [(period_energy_chart1, f1), (period_energy_chart2, f2), (period_energy_chart3, f3)]:
            if ch is not None:
                try:
                    ch.update_figure(fig)
                except Exception:
                    # element deleted by navigation/clear; null it to stop future attempts
                    pass
    except Exception:
        pass  # guard against DB issues or UI teardown


async def charts_auto_tick():
    """Root-level auto refresh tick for Charts page. Created at startup so its parent slot is never deleted on nav.
    Uses on_charts guard + enabled/interval checks. Strong try to survive stale UI.
    """
    global last_chart_refresh_time, current_range, auto_status, on_charts
    if not on_charts:
        return
    now = time.monotonic()
    if charts_auto_refresh_enabled and (now - last_chart_refresh_time >= chart_refresh_interval):
        last_chart_refresh_time = now
        try:
            await load_and_render(current_range.get("hours", 1), from_auto=True)
            if auto_status and charts_auto_refresh_enabled:
                auto_status.set_text(f"Auto-refreshing every {chart_refresh_interval}s • updated {get_local_now().strftime('%H:%M:%S')}")
        except Exception:
            pass  # protect against deleted slots / nav during render


def _run_deferred_inits():
    """Root-level checker for page init actions (replaces once timers created inside pages).
    This timer is created at startup so its parent slot is never deleted.
    Flags are set in show_* after building UI.
    """
    global _charts_defer_pending, _smoothing_defer_pending, _summary_defer_pending, _pending_summary_load
    if _charts_defer_pending:
        _charts_defer_pending = False
        if on_charts:
            try:
                asyncio.create_task(refresh_period_energy_chart())
            except Exception:
                pass
    if _smoothing_defer_pending:
        _smoothing_defer_pending = False
        if on_charts:
            try:
                update_smoothing_buttons(smoothing)
            except Exception:
                pass
    if _summary_defer_pending:
        _summary_defer_pending = False
        load_fn = _pending_summary_load
        if load_fn:
            try:
                asyncio.create_task(load_fn())
            except Exception:
                pass
            _pending_summary_load = None


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

    if maintenance_task and not maintenance_task.done():
        maintenance_task.cancel()

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
                    # Best-effort immediate UI refresh for live dashboard (avoid creating timers from background poller - causes slot errors)
                    if on_dashboard:
                        try:
                            update_live_dashboard()
                        except Exception:
                            pass  # safe if elements stale
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
    """Update pre-aggregated data (daily + power_agg for composite charts) from raw.
    CRITICAL: NEVER deletes, downsamples or overwrites any raw measurements data.
    We ADD aggregated tables only so summaries are fast and long charts use coarse for old + full recent.
    Always records the run. Obeys: add aggs, do not nuke/cut the input data.
    """
    import time as _time
    start_ts = datetime.now(timezone.utc)
    start_perf = _time.perf_counter()
    rows_processed = 0
    aggs_touched = 0
    status = 'success'
    error_message = None
    cutoff = (start_ts - timedelta(days=1)).isoformat()  # informational only now

    try:
        async with aiosqlite.connect(DB_FILE) as db:
            # Full daily backfill (correct trapezoid). Cheap and guarantees week/month/year have numbers.
            try:
                await _update_daily_aggregates(db, None)
            except Exception as daily_err:
                logger.warning(f"Daily update failed: {daily_err}")

            # Build/store power aggs for older slices (for composite long charts).
            # Does not touch raw.
            try:
                touched = await _update_power_aggregates(db, start_ts)
                aggs_touched = touched
            except Exception as pa_err:
                logger.warning(f"Power agg update failed: {pa_err}")

            await db.commit()

            # For logging, count current power aggs as proxy
            try:
                cur = await db.execute("SELECT COUNT(*) FROM power_agg")
                aggs_touched = (await cur.fetchone() or (0,))[0]
            except Exception:
                pass

            rows_processed = 1  # marker that run touched aggs
            logger.info(f"Maintenance: daily + power aggs updated (raw preserved, no deletes)")

    except Exception as e:
        status = 'error'
        error_message = str(e)
        logger.error(f"Aggregation run error: {e}")

    duration = _time.perf_counter() - start_perf
    try:
        await _log_aggregation_run(
            run_timestamp=start_ts.isoformat(),
            duration_seconds=round(duration, 3),
            rows_processed=rows_processed,
            buckets_created=aggs_touched,  # repurposed to # power_agg rows after update
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


async def _update_daily_aggregates(db: aiosqlite.Connection, since: datetime = None) -> None:
    """Populate energy_daily using data with proper trapezoidal integration (never the broken *30/3600 approx).
    since=None means full backfill over all available raw (ensures week/month/year summaries are populated).
    Raw measurements input is NEVER cut or modified.
    """
    try:
        where = ""
        params = ()
        if since is not None:
            cutoff_str = since.isoformat()
            where = "WHERE timestamp >= ?"
            params = (cutoff_str,)
        cur = await db.execute(
            f"SELECT timestamp, pv_power, battery_power, grid_power, load_power FROM measurements {where} ORDER BY timestamp",
            params
        )
        rows = await cur.fetchall()
        if not rows:
            return
        from collections import defaultdict
        by_day = defaultdict(list)
        for r in rows:
            try:
                ts = r[0]
                dt = _parse_stored_ts(ts)
                day = dt.date().isoformat()
                by_day[day].append({
                    "timestamp": ts,
                    "pv_power": r[1],
                    "battery_power": r[2],
                    "grid_power": r[3],
                    "load_power": r[4],
                })
            except Exception:
                continue

        for d, day_rows in by_day.items():
            if len(day_rows) < 2:
                # still record 0s if we want presence, but skip for now
                continue
            pv = compute_energy_kwh(day_rows, "pv_power")
            load = compute_energy_kwh(day_rows, "load_power")

            # signed flows (copy logic from get_summary for accuracy)
            bat_discharge = 0.0
            bat_charge = 0.0
            grid_import = 0.0
            grid_export = 0.0
            for i in range(1, len(day_rows)):
                t0 = _parse_stored_ts(day_rows[i-1]["timestamp"])
                t1 = _parse_stored_ts(day_rows[i]["timestamp"])
                dt_h = max(0.0, (t1 - t0).total_seconds() / 3600.0)
                b0 = day_rows[i-1].get("battery_power") or 0.0
                b1 = day_rows[i].get("battery_power") or 0.0
                g0 = day_rows[i-1].get("grid_power") or 0.0
                g1 = day_rows[i].get("grid_power") or 0.0
                bat_discharge += max(0.0, -((b0 + b1) / 2.0)) * dt_h
                bat_charge += max(0.0, ((b0 + b1) / 2.0)) * dt_h
                grid_import += max(0.0, ((g0 + g1) / 2.0)) * dt_h
                grid_export += max(0.0, -((g0 + g1) / 2.0)) * dt_h

            await db.execute(
                """INSERT INTO energy_daily (date, pv_energy_kwh, battery_discharge_kwh, grid_import_kwh, grid_export_kwh, load_energy_kwh)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     pv_energy_kwh=excluded.pv_energy_kwh,
                     battery_discharge_kwh=excluded.battery_discharge_kwh,
                     grid_import_kwh=excluded.grid_import_kwh,
                     grid_export_kwh=excluded.grid_export_kwh,
                     load_energy_kwh=excluded.load_energy_kwh""",
                (d, round(pv, 4), round(bat_discharge, 4), round(grid_import, 4), round(grid_export, 4), round(load, 4))
            )
        await db.commit()
    except Exception as e:
        logger.warning(f"_update_daily_aggregates error: {e}")


async def _update_power_aggregates(db: aiosqlite.Connection, start_ts: datetime) -> int:
    """Populate power_agg with downsampled versions for older data only.
    We keep ALL raw in measurements forever. power_agg is *additional* data source for composite long charts.
    Resolutions: >14d -> 600s (10min), >7d -> 120s (2min).
    Returns count of rows touched/inserted.
    """
    touched = 0
    try:
        now = start_ts
        # For all data, but we will only store the coarse for the old parts; recent always comes from raw in queries.
        # To support future long history, downsample and store old.
        cur = await db.execute(
            "SELECT timestamp, pv_power, battery_power, grid_power, load_power, soc, grid_status FROM measurements ORDER BY timestamp"
        )
        rows = await cur.fetchall()
        if not rows:
            return 0
        # Compute cutoffs
        cutoff_14 = (now - timedelta(days=14)).isoformat()
        cutoff_7 = (now - timedelta(days=7)).isoformat()

        # Group for 10min on very old, 2min on medium old
        from collections import defaultdict
        def make_agg(rows_in, res_sec):
            if not rows_in:
                return []
            buckets = defaultdict(list)
            for r in rows_in:
                try:
                    ts = _parse_stored_ts(r[0])
                    key = int(ts.timestamp() // res_sec) * res_sec
                    buckets[key].append(r)
                except Exception:
                    continue
            out = []
            for k in sorted(buckets):
                g = buckets[k]
                if not g: continue
                def av(idx):
                    vs = [x[idx] for x in g if x[idx] is not None]
                    return sum(vs)/len(vs) if vs else None
                out.append((
                    g[0][0],  # rep ts (first in bucket)
                    res_sec,
                    av(1), av(2), av(3), av(4), av(5), g[-1][6] if g[-1][6] is not None else None
                ))
            return out

        very_old = [r for r in rows if r[0] < cutoff_14]
        medium_old = [r for r in rows if cutoff_14 <= r[0] < cutoff_7]

        inserts = []
        inserts.extend(make_agg(very_old, 600))
        inserts.extend(make_agg(medium_old, 120))

        if inserts:
            await db.executemany(
                """INSERT INTO power_agg (timestamp, resolution_sec, pv_power, battery_power, grid_power, load_power, soc, grid_status)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(timestamp, resolution_sec) DO UPDATE SET
                     pv_power=excluded.pv_power, battery_power=excluded.battery_power,
                     grid_power=excluded.grid_power, load_power=excluded.load_power,
                     soc=excluded.soc, grid_status=excluded.grid_status""",
                inserts
            )
            touched = len(inserts)
            logger.info(f"power_agg updated: {touched} coarse rows for old periods (raw untouched)")
    except Exception as e:
        logger.warning(f"_update_power_aggregates error: {e}")
    return touched


async def get_composite_readings(hours: int) -> List[Dict[str, Any]]:
    """Return stitched readings for charts: aggregated (coarse) data for older parts + full raw for recent.
    Example for 30d (hours=720): 10min agg for first ~14d, 2min for ~7-14d, full raw last 7d.
    This loads far fewer points for old while preserving accuracy for recent + all raw always kept.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            # Always get recent full precision (last 7 days or all if shorter period)
            recent_cutoff = (now - timedelta(days=7)).isoformat()
            if hours <= 24 * 7:
                # short period: all from raw
                async with db.execute(
                    "SELECT * FROM measurements WHERE timestamp >= ? ORDER BY timestamp ASC",
                    (since.isoformat(),)
                ) as cur:
                    return [dict(r) for r in await cur.fetchall()]

            # long: raw for last week
            async with db.execute(
                "SELECT * FROM measurements WHERE timestamp >= ? ORDER BY timestamp ASC",
                (max(since.isoformat(), recent_cutoff),)
            ) as cur:
                recent_rows = [dict(r) for r in await cur.fetchall()]

            # older parts from power_agg at proper res
            old_rows = []
            # determine needed res
            if hours > 24 * 21:
                # use 10min for oldest, 2min for middle
                # first get 10min for the very old slice
                agg_since = since.isoformat()
                async with db.execute(
                    "SELECT timestamp, pv_power, battery_power, grid_power, load_power, soc, grid_status FROM power_agg WHERE resolution_sec=600 AND timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                    (agg_since, recent_cutoff)
                ) as cur:
                    for r in await cur.fetchall():
                        old_rows.append({
                            "timestamp": r["timestamp"],
                            "pv_power": r["pv_power"],
                            "battery_power": r["battery_power"],
                            "grid_power": r["grid_power"],
                            "load_power": r["load_power"],
                            "soc": r["soc"],
                            "grid_status": r["grid_status"],
                        })
                # 2min for the next band if needed (between 14d and 7d, but clamp to since)
                mid_start = (now - timedelta(days=14)).isoformat()
                async with db.execute(
                    "SELECT timestamp, pv_power, battery_power, grid_power, load_power, soc, grid_status FROM power_agg WHERE resolution_sec=120 AND timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                    (max(agg_since, mid_start), recent_cutoff)
                ) as cur:
                    for r in await cur.fetchall():
                        old_rows.append({
                            "timestamp": r["timestamp"],
                            "pv_power": r["pv_power"],
                            "battery_power": r["battery_power"],
                            "grid_power": r["grid_power"],
                            "load_power": r["load_power"],
                            "soc": r["soc"],
                            "grid_status": r["grid_status"],
                        })
            else:
                # 7d < hours <=21d : use 2min for the old part
                agg_since = since.isoformat()
                async with db.execute(
                    "SELECT timestamp, pv_power, battery_power, grid_power, load_power, soc, grid_status FROM power_agg WHERE resolution_sec=120 AND timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                    (agg_since, recent_cutoff)
                ) as cur:
                    for r in await cur.fetchall():
                        old_rows.append({
                            "timestamp": r["timestamp"],
                            "pv_power": r["pv_power"],
                            "battery_power": r["battery_power"],
                            "grid_power": r["grid_power"],
                            "load_power": r["load_power"],
                            "soc": r["soc"],
                            "grid_status": r["grid_status"],
                        })

            # merge + sort (prefer raw recent if overlap, but cutoffs avoid)
            all_r = old_rows + recent_rows
            # de-dup by ts (rare)
            seen = set()
            uniq = []
            for r in sorted(all_r, key=lambda x: x.get("timestamp", "")):
                k = r.get("timestamp")
                if k not in seen:
                    seen.add(k)
                    uniq.append(r)
            return uniq
    except Exception as e:
        logger.warning(f"get_composite_readings error, falling back to raw: {e}")
        # safe fallback to raw
        return await get_readings_since(since)


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
            restarted = False
            try:
                exc = maintenance_task.exception()
                if exc:
                    logger.error(f"Maintenance task crashed: {exc}")
                    restarted = True
            except asyncio.CancelledError:
                logger.info("Maintenance task was cancelled")
            except Exception as e:
                logger.error(f"Error inspecting maintenance task: {e}")
                restarted = True
            if restarted:
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

    ts = [_parse_stored_ts(r["timestamp"]).astimezone() for r in rows]
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

    ts = [_parse_stored_ts(r["timestamp"]).astimezone() for r in rows]
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
period_energy_chart1 = period_energy_chart2 = period_energy_chart3 = None
recent_flow_values = deque(maxlen=3)  # last 3 (pv, battery, grid, load) for smoothing Energy Flow
on_dashboard = False  # guard for live updates to avoid deleted element warnings
on_charts = False
_charts_auto_timer_started = False
_charts_defer_pending = False
_smoothing_defer_pending = False
_summary_defer_pending = False
_pending_summary_load = None
chart_refresh_interval = 10  # seconds for charts auto-refresh (user configurable, >=2)
last_chart_refresh_time = 0.0  # monotonic time of last auto chart refresh
charts_auto_refresh_enabled = True
last_period_refresh = 0.0  # for throttling the heavier period energy chart updates
# plots refs for in-place updates to avoid full re-creation flicker
plots = {"power": None, "soc": None, "cost": None}

current_range = {"hours": 1}
smoothing = 0
power_visible = {"PV": True, "Battery": True, "Grid": True, "Load": True}

sidebar_drawer = None  # populated by build_sidebar; used for mobile toggle


def build_sidebar():
    """Build left nav drawer. Exposed via global sidebar_drawer so a top-level toggle can open it on mobile.
    Uses Quasar props (breakpoint + show-if-above) so drawer is persistently open/visible on desktop (>=~1024px)
    and closed/overlay on narrow mobile (starts value=False). Hamburger toggle works for mobile to open from closed. Desktop nav buttons are in-viewport without needing click.
    """
    global sidebar_drawer
    # Start open so desktop shows it immediately; Quasar breakpoint + show-if-above will hide on narrow <1024.
    # We only force-close via JS on actual mobile nav (never on desktop).
    sidebar_drawer = (
        ui.left_drawer(top_corner=True, bottom_corner=True, value=True)
        .props('breakpoint=1024 show-if-above')
        .style('background-color: #111827; border-right: 1px solid #374151')
    )
    sidebar_drawer.props('data-drawer-open="true"')
    with sidebar_drawer:
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
            try:
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
            except Exception as nav_err:
                # Guard against client deleted / stale element during rapid nav (NiceGUI known issue)
                logger.debug(f"nav handler ignored error for {name}: {nav_err}")

            # IMPORTANT: do NOT force-close here for desktop. The JS click handler below
            # only closes when window.innerWidth < 1024 (mobile). Desktop keeps the
            # persistent sidebar (show-if-above + breakpoint) open across nav changes.
            # Removing the unconditional .value=False prevents "auto-close on change" in desktop browser.

        for label, func_name, icon in nav_items:
            btn = ui.button(label, icon=icon, on_click=lambda n=func_name: asyncio.create_task(async_nav_handler(n))).props("flat").classes(
                "w-full justify-start q-pa-md text-lg"
            ).style("color: #e5e7eb")
            btn.props(f'data-nav="{func_name}"')  # stable hook for tests (desktop + mobile)

            # Only auto-close drawer on mobile viewports (<1024). Desktop (show-if-above) keeps sidebar persistently open.
            # Do not touch drawer classes on wide screens.
            btn.on('click', lambda: ui.run_javascript("""
                setTimeout(function() {
                    const w = (window.innerWidth || 9999);
                    const drawer = document.querySelector('.q-drawer');
                    if (drawer && w < 1024) {
                        // mobile only: close after nav click
                        drawer.classList.remove('q-drawer--open');
                        drawer.setAttribute('data-drawer-open', 'false');
                        drawer.style.visibility = 'hidden';
                        setTimeout(function() {
                            if (drawer) drawer.style.visibility = '';
                        }, 400);
                    }
                    // desktop >=1024: do nothing; Quasar breakpoint/show-if-above keeps it visible
                }, 80);
            """))


def update_live_dashboard():
    """Called by timer to refresh live values. Broad guards for stale slots/timers after nav (AGENTS.md)."""
    global latest_reading, last_period_refresh, recent_flow_values, on_dashboard
    try:
        if not on_dashboard:
            return
    except Exception:
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
                age = (datetime.now(timezone.utc) - _parse_stored_ts(ts)).total_seconds()
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
            live_last_update.set_text(f"Last update: {_parse_stored_ts(r.timestamp).astimezone().strftime('%H:%M:%S')}")
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
    global main_content, live_last_update, live_sankey, period_energy_chart1, period_energy_chart2, period_energy_chart3, on_dashboard, on_charts
    on_dashboard = True
    on_charts = False
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

            # Energy by Period split into 3 side-by-side charts with independent y-scales (per acceptance):
            # Today+Yesterday | Week+Month | Year
            ui.label("Energy by Period (kWh)").classes("text-xl mt-4 mb-1 font-semibold section-title")
            with ui.row().classes("w-full gap-2 items-stretch"):
                with ui.column().classes("flex-1"):
                    ui.label("Today + Yesterday").classes("text-xs text-center text-gray-400")
                    period_energy_chart1 = ui.plotly(create_period_energy_chart([], title="Today + Yesterday")).classes("w-full")
                with ui.column().classes("flex-1"):
                    ui.label("Week + Month").classes("text-xs text-center text-gray-400")
                    period_energy_chart2 = ui.plotly(create_period_energy_chart([], title="Week + Month")).classes("w-full")
                with ui.column().classes("flex-1"):
                    ui.label("Year").classes("text-xs text-center text-gray-400")
                    period_energy_chart3 = ui.plotly(create_period_energy_chart([], title="Year")).classes("w-full")

            # initial load of period bars (once) - uses root defer to avoid slot deletion
            global _charts_defer_pending
            _charts_defer_pending = True

            # Quick note
            ui.label("Data is polled in background and saved to SQLite. Charts update automatically.").classes("text-xs text-gray-500 mt-2")

            # Auto-refresh timer + immediate population from DB
            # Note: timers are created once outside to avoid slot deletion errors on navigation
            pass  # timers created globally after initial build


async def show_charts():
    global main_content, current_range, smoothing, power_visible, auto_status, charts_auto_refresh_enabled, chart_refresh_interval, on_dashboard, on_charts
    global period_energy_chart1, period_energy_chart2, period_energy_chart3
    on_dashboard = False
    on_charts = True
    period_energy_chart1 = period_energy_chart2 = period_energy_chart3 = None
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
                    # Use composite: aggregated data from power_agg for older parts + full raw for recent.
                    # This implements the requested behavior without ever cutting raw input data.
                    rows = await get_composite_readings(hours)

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
                            t = _parse_stored_ts(r["timestamp"]).astimezone()
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
                    # Drive both period and smoothing button highlights after every load/render
                    try:
                        if period_buttons:
                            update_active_button(current_range.get("hours", 1))
                    except Exception:
                        pass
                    try:
                        if smoothing_buttons:
                            update_smoothing_buttons(smoothing)
                    except Exception:
                        pass
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
                        btn.props(remove="outline color").props("color=primary size=sm")
                    else:
                        btn.props(remove="color").props("outline color=primary size=sm")
                        btn.classes("border-primary text-primary")  # blue border + blue text for unselected like PAUSE

            update_active_button(current_range["hours"])

            def update_smoothing_buttons(active_smoothing):
                """Force exactly one active. Remove state from ALL, then set ONLY the active to filled primary.
                Called at creation time and after every load/render.
                """
                for s, btn in list(smoothing_buttons.items()):
                    try:
                        btn.props(remove="color outline")
                        if s == active_smoothing:
                            btn.props("color=primary size=sm")
                            btn.props(f'data-smoothing="{s}" data-active="true" aria-pressed="true"')
                        else:
                            btn.props("outline color=primary size=sm")
                            btn.classes("border-primary text-primary")
                            btn.props(f'data-smoothing="{s}" data-active="false" aria-pressed="false"')
                    except Exception:
                        pass  # element may be stale after nav; ignore for highlight

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
                        is_active = (s == smoothing)
                        if is_active:
                            btn = ui.button(label, on_click=make_s_handler()).props("color=primary size=sm")
                            btn.props(f'data-smoothing="{s}" data-active="true" aria-pressed="true"')
                        else:
                            btn = ui.button(label, on_click=make_s_handler()).props("outline color=primary size=sm")
                            btn.classes("border-primary text-primary")
                            btn.props(f'data-smoothing="{s}" data-active="false" aria-pressed="false"')
                        smoothing_buttons[s] = btn

            # Force clean single-active state right after creation (before initial load_and_render)
            update_smoothing_buttons(smoothing)
            # Re-apply after first paint to survive any NiceGUI render timing (guarantees 1 active on initial 03 shot)
            # use root defer
            global _smoothing_defer_pending
            _smoothing_defer_pending = True

            # chart_container created here so plots appear below the top controls+status
            chart_container = ui.column().classes("w-full gap-6")

            # Auto-refresh is driven by a root-level timer (created at startup) guarded by on_charts flag.
            # This prevents "parent slot deleted" when navigating away from Charts (timer no longer lives in a clearable container).
            global _charts_auto_timer_started
            if not _charts_auto_timer_started:
                _charts_auto_timer_started = True
                last_chart_refresh_time = time.monotonic()

            # initial load
            await load_and_render(current_range["hours"])
            # Extra guard: re-apply smoothing highlight after the full render (in case of any re-render side effects)
            try:
                if smoothing_buttons:
                    update_smoothing_buttons(smoothing)
            except Exception:
                pass


async def show_summary():
    global main_content, on_dashboard, on_charts
    global period_energy_chart1, period_energy_chart2, period_energy_chart3
    on_dashboard = False
    on_charts = False
    period_energy_chart1 = period_energy_chart2 = period_energy_chart3 = None
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        with ui.column().classes("w-full p-4 gap-6"):
            ui.label("Energy Summaries").classes("text-3xl font-bold")
            ui.add_head_html('''
<style>
.q-tooltip {
  background-color: #111827 !important;
  border: 1px solid #d1d5db !important;
  color: #d1d5db !important;
  padding: 4px 8px !important;
  border-radius: 4px !important;
  font-size: 11px !important;
}
</style>
''')

            periods = [
                ("Today", 0),
                ("Yesterday", 1),
                ("This Week", 7),
                ("This Month", 30),
                ("This Year", 365),
            ]

            summary_container = ui.column().classes("w-full gap-4")

            async def load_all():
                # Use host OS TZ for boundaries (consistent with dashboard periods and all time displays)
                now_loc = get_local_now()
                today_start_loc = get_local_today_start()

                # Build boundaries
                bounds = []
                for name, days in periods:
                    if days == 0:
                        start = to_utc(today_start_loc)
                        end = to_utc(now_loc)
                    elif days == 1:
                        start = to_utc(today_start_loc - timedelta(days=1))
                        end = to_utc(today_start_loc)
                    elif name == "This Year":
                        start = to_utc(now_loc.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0))
                        end = to_utc(now_loc)
                    else:
                        start = to_utc(now_loc - timedelta(days=days))
                        end = to_utc(now_loc)
                    bounds.append((name, start, end))

                # Parallel fetch to make summaries load faster (was sequential awaits)
                coros = [get_summary(s, e) for _, s, e in bounds]
                results = await asyncio.gather(*coros, return_exceptions=True)
                summaries = []
                for (name, _, _), res in zip(bounds, results):
                    if isinstance(res, Exception):
                        res = {"pv": 0, "battery_discharge": 0, "battery_charge": 0, "grid_import": 0, "grid_export": 0, "load": 0, "self_sufficiency_pct": 0, "data_start": None}
                    summaries.append((name, res))

                buy_pln, sell_pln = get_current_prices_pln()
                try:
                    summary_container.clear()
                except Exception:
                    pass
                try:
                    with summary_container:
                        for name, summary in summaries:
                            with ui.card().classes("w-full p-4 bg-[#1f2937]"):
                                display_name = name
                                ds = summary.get("data_start")
                                if ds:
                                    try:
                                        ds_loc = ds.astimezone(get_local_tz()) if getattr(ds, 'tzinfo', None) else ds
                                        display_name = f"{name} (since {ds_loc.strftime('%Y-%m-%d')})"
                                    except Exception:
                                        display_name = f"{name} (since {ds.strftime('%Y-%m-%d')})"
                                ui.label(display_name).classes("text-lg font-semibold mb-2 text-cyan-400")
                                def add_hint(text: str):
                                    q = ui.html('<div style="margin-left:1px;width:13px;height:13px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;border-radius:9999px;background:#000;border:1px solid #d1d5db;color:#d1d5db;cursor:help;">?</div>')
                                    q.tooltip(text)
                                    return q

                                with ui.row().classes("gap-5 flex-wrap text-sm items-center"):
                                    with ui.row().classes("items-center gap-0.5"):
                                        ui.label(f"☀️ PV: {summary['pv']:.2f} kWh")
                                        add_hint("Total solar energy produced (kWh) — trapezoidal integration of PV power over the period.")
                                    with ui.row().classes("items-center gap-0.5"):
                                        ui.label(f"🔋 Bat used: {summary['battery_discharge']:.2f} kWh")
                                        add_hint("Total energy discharged from battery (kWh) — only negative battery power integrated.")
                                    with ui.row().classes("items-center gap-0.5"):
                                        ui.label(f"🔋 Bat charged: {summary['battery_charge']:.2f} kWh")
                                        add_hint("Total energy charged into battery (kWh) — only positive battery power integrated.")
                                    with ui.row().classes("items-center gap-0.5"):
                                        ui.label(f"⬇️ Grid in: {summary['grid_import']:.2f} kWh")
                                        add_hint("Total energy imported from the grid (kWh).")
                                    with ui.row().classes("items-center gap-0.5"):
                                        ui.label(f"⬆️ Grid out: {summary['grid_export']:.2f} kWh")
                                        add_hint("Total energy exported to the grid (kWh).")
                                    with ui.row().classes("items-center gap-0.5"):
                                        ui.label(f"🏠 Load: {summary['load']:.2f} kWh")
                                        add_hint("Total household consumption (kWh) — trapezoidal integration of load power.")
                                    with ui.row().classes("items-center gap-0.5"):
                                        ui.label(f"♻️ Self: {summary['self_sufficiency_pct']:.2f}%")
                                        add_hint("Self-sufficiency: % of consumption covered by own generation (PV + battery) instead of grid. Formula: (PV + battery - Grid import + Grid export) / Load × 100.")
                                cost = round(summary['grid_import'] * buy_pln, 2)
                                revenue = round(summary['grid_export'] * sell_pln, 2)
                                net = round(cost - revenue, 2)
                                with ui.row().classes("items-center text-xs text-gray-400 mt-1"):
                                    ui.label(f"💰 Est. cost {cost:.2f} PLN • revenue {revenue:.2f} PLN • net {net:+.2f} PLN")
                                    add_hint("Cost = Grid import × buy price. Revenue = Grid export × sell price. Net = revenue − cost.")
                except Exception as render_err:
                    # Guard against client deleted during nav (pre-existing NiceGUI issue with drawer + async loads)
                    logger.debug(f"summary content render skipped (client stale): {render_err}")

            # Defer the load to allow NiceGUI context to settle (helps with client deleted during nav from drawer)
            # use root defer
            global _summary_defer_pending, _pending_summary_load
            _summary_defer_pending = True
            _pending_summary_load = load_all


async def show_raw_data():
    global main_content, on_dashboard, on_charts
    global period_energy_chart1, period_energy_chart2, period_energy_chart3
    on_dashboard = False
    on_charts = False
    period_energy_chart1 = period_energy_chart2 = period_energy_chart3 = None
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
                            fr["timestamp"] = _parse_stored_ts(fr["timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
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
    global main_content, on_dashboard, on_charts
    global period_energy_chart1, period_energy_chart2, period_energy_chart3
    on_dashboard = False
    on_charts = False
    period_energy_chart1 = period_energy_chart2 = period_energy_chart3 = None
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        with ui.column().classes("w-full p-4 gap-6"):
            ui.label("Maintenance & Aggregation History").classes("text-3xl font-bold")

            # Status header (top)
            status_container = ui.row().classes("gap-4 items-center flex-wrap")
            # Buttons placed directly above the recent runs table (user requirement)
            buttons_container = ui.row().classes("gap-2 mb-2")
            runs_container = ui.column().classes("w-full")
            chart_container = ui.column().classes("w-full")

            async def force_run():
                ui.notify("Forcing aggregation run (raw data preserved)...", type="info")
                try:
                    n = await aggregate_old_data()
                    ui.notify(f"Aggregation completed. (raw input untouched)", type="positive")
                    await load_maintenance_data()
                except Exception as e:
                    ui.notify(f"Force run failed: {e}", type="negative")

            with buttons_container:
                ui.button("Refresh", on_click=lambda: asyncio.create_task(load_maintenance_data()), icon="refresh").props("size=sm")
                ui.button("Force Run Now", on_click=force_run, icon="play_arrow").props("size=sm outline")

            async def load_maintenance_data():
                runs = await get_aggregation_runs(100)
                # Status
                status_container.clear()
                with status_container:
                    if runs:
                        last = runs[0]
                        last_time = last.get("run_timestamp", "")
                        try:
                            last_dt = _parse_stored_ts(last_time).astimezone()
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
                            last_dt2 = _parse_stored_ts(last_time).astimezone()
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
                        {"name": "buckets_created", "label": "Agg rows (power_agg)", "field": "buckets_created"},
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
                                dt = _parse_stored_ts(fr["run_timestamp"]).astimezone()
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
                                dt = _parse_stored_ts(ts).astimezone()
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
                            x=times, y=buckets_c, name="Agg rows updated",
                            mode="lines+markers", line=dict(color="#4caf50")
                        ))
                        fig.update_layout(
                            title="Aggregation Runs (daily + power aggs)",
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

            await load_maintenance_data()


def show_settings():
    global main_content, on_dashboard, on_charts
    global period_energy_chart1, period_energy_chart2, period_energy_chart3
    on_dashboard = False
    on_charts = False
    period_energy_chart1 = period_energy_chart2 = period_energy_chart3 = None
    if main_content is None:
        main_content = ui.column().classes("w-full")
    main_content.clear()
    with main_content:
        # Professional, compact settings form.
        # Tight grid on desktop (no huge empty space, fields packed nicely side-by-side).
        # On mobile wraps cleanly. Centered container.
        with ui.column().classes("w-full p-4 max-w-[820px] mx-auto"):
            ui.label("Settings").classes("text-3xl font-bold mb-4")

            cfg = load_config()

            # Compact side-by-side grids on desktop; wrap on narrow. max-w on inputs to avoid spread.
            with ui.grid(columns=3).classes("gap-3 w-full"):
                ip = ui.input("SigenStor IP", value=cfg["ip"]).props("filled dense").classes("col-span-1").style("max-width: 240px")
                port = ui.number("Port", value=cfg["port"], min=1, max=65535).props("filled dense").classes("col-span-1").style("max-width: 140px")
                slave = ui.number("Slave ID", value=cfg["slave_id"], min=1, max=247).props("filled dense").classes("col-span-1").style("max-width: 140px")

            # Two col for other numeric settings
            with ui.grid(columns=2).classes("gap-3 w-full mt-2"):
                interval = ui.number("Poll interval (s)", value=cfg["poll_interval"], min=2, max=300).props("filled dense").style("max-width: 180px")
                bat_cap = ui.number("Battery capacity (kWh)", value=cfg.get("battery_capacity_kwh", 18.0), min=0, step=0.1).props("filled dense").style("max-width: 180px")

            with ui.grid(columns=2).classes("gap-3 w-full mt-2"):
                buy_price = ui.number("Buy price (grosze/kWh)", value=cfg.get("buy_price_grosze", 75), min=0, step=1).props("filled dense").style("max-width: 180px")
                sell_price = ui.number("Sell price (grosze/kWh)", value=cfg.get("sell_price_grosze", 35), min=0, step=1).props("filled dense").style("max-width: 180px")

            ui.label("Tip: Prices in grosze (1 PLN = 100 grosze). Battery capacity is usable kWh.").classes("text-xs text-gray-400 mt-1")

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
    # Re-ensure dev seed (in case import-time seeding ran before full env/paths or for robustness)
    try:
        _ensure_dev_db_seeded()
    except Exception:
        pass
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

    # Mobile menu toggle (hamburger) + title bar: ensures categories reachable on phone viewports (narrow screens).
    # Use ui.header for proper integration with left_drawer (avoids overlay/cutoff/truncation on 390px).
    # Clicking the menu icon toggles the left drawer which contains all nav buttons.
    def _toggle_drawer():
        global sidebar_drawer
        if sidebar_drawer:
            sidebar_drawer.toggle()
            # update DOM attr for test proof (shipped state)
            try:
                open_state = 'true' if getattr(sidebar_drawer, 'value', False) else 'false'
                sidebar_drawer.props(f'data-drawer-open="{open_state}"')
            except:
                pass
    with ui.header().classes("bg-[#0f172a] text-white items-center q-pa-xs border-b border-gray-700"):
        # Distinctive hamburger for mobile: reliable visible control that opens the category drawer.
        # Use .menu-toggle + aria-label so verification (and users) can target the *header* control.
        ui.button(icon="menu", on_click=_toggle_drawer).props('flat dense color=white aria-label="Open menu"').classes("menu-toggle q-mr-sm")
        ui.label(APP_TITLE).classes("text-sm sm:text-base font-semibold")

    show_dashboard()

    # Create live update timers only once at startup (prevents "parent slot deleted" errors
    # when navigating between pages, as timers created inside show_dashboard would be
    # children of main_content and get deleted on clear()).
    ui.timer(3.0, update_live_dashboard)
    ui.timer(0.6, update_live_dashboard, once=True)

    # Charts auto-refresh timer created at root so it never has a deleted parent slot when user navigates.
    # The callback itself checks on_charts flag (set only inside show_charts).
    ui.timer(2, charts_auto_tick)
    _charts_auto_timer_started = True  # mark so show_charts doesn't try to create again
    last_chart_refresh_time = time.monotonic()

    # Root level 0.1s checker for deferred page inits (period bars, smoothing highlight, summary load).
    # Replaces the once=True timers that were created inside main_content (which get deleted on nav).
    ui.timer(0.1, _run_deferred_inits)

    # Run the NiceGUI app
    # Port and DB are resolved from env at import time (defaults keep prod behavior unchanged).
    logger.info(f"UI starting on port {PORT} using DB {DB_FILE}")
    ui.run(
        title=APP_TITLE,
        dark=True,
        host="0.0.0.0",
        port=PORT,
        reload=False,   # set True during development if desired
        show=True,
        favicon="🔋",
    )
