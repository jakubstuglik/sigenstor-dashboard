# SigenStor Dashboard

A modern web application for monitoring the **Sigenergy SigenStor** energy storage system via Modbus TCP (read-only).

## Features

- **Live Dashboard**: cards with current values (SOC, PV, Battery, Grid, Load), Sankey energy flow diagram, system status.
- **Historical Charts**: interactive Plotly charts (power, SOC, energy) with time range selection.
- **Summaries**: today/yesterday/week/month – PV production, battery usage, grid import/export, self-consumption.
- **Raw Data**: table of recent measurements + CSV export.
- **Settings**: IP, port, slave ID, poll interval configuration + connection test.
- **SQLite** for history storage.
- Dark, professional energy/tech theme.
- Full error handling, logging, retry.

## Requirements

- Python 3.10+
- SigenStor with Modbus TCP enabled (in mySigen app: Device → Settings → Modbus TCP Server Enable)
- Device IP accessible from the computer (default port 502, slave 247)

## Installation and Running

```bash
# 1. Clone or download the repo
cd sigenstor-dashboard

# 2. Create venv and install dependencies
python -m venv .venv
.venv\Scripts\activate          # Windows (PowerShell: .\.venv\Scripts\Activate.ps1)
# or source .venv/bin/activate   # Linux/mac

pip install -r requirements.txt

# 3. Run the app
python main.py
```

The app will open in the browser: http://localhost:8080

## Development Setup

```bash
# Install dev dependencies (includes playwright for UI tests)
pip install -r requirements-dev.txt

# Install Playwright browsers
playwright install

# Run the app
python main.py
```

Useful test files (in repo root for now):
- `playwright_summary_test.py` - tests summary page
- `playwright_button_highlight_test.py` etc. for UI verification

See `docker-compose.yml` for containerized dev/prod.

## Default Configuration

- IP: `192.168.33.13`
- Port: `502`
- Slave ID: `247`
- Interval: `15` seconds

Go to the **Settings** tab, enter the correct data and click **Save Config** + **Test Connection**.

## Modbus Structure (basic)

The app reads key registers (compatible with Sigenergy Modbus Protocol ~V1.7/V2.x):

| Parameter      | Address | Type    | Scale   | Unit | Notes                          |
|----------------|---------|---------|---------|------|--------------------------------|
| Battery SOC    | 30014   | uint16  | 0.1     | %    |                                |
| PV Power       | 30035   | int32   | 0.001   | kW   | Plant Photovoltaic power       |
| Battery Power  | 30037   | int32   | 0.001   | kW   | + charge, - discharge          |
| Grid Power     | 30005   | int32   | 0.001   | kW   | + import, - export             |
| Grid Status    | 30009   | uint16  | 1       | -    | 0=OnGrid, 1/2=OffGrid          |

**House Load Power** is calculated: `Load = PV + Grid - Battery`

Registers can be easily extended in the code (see `REGISTERS` in `main.py`).

**Note:** Make sure you use the correct Modbus protocol version for your firmware. Some newer versions (V2.8+) add direct load power registers.

## Running in Background / Production (optional)

- Use `python main.py --host 0.0.0.0 --port 8080` to listen on all interfaces.
- For persistent operation: systemd, docker, or `nohup python main.py &`

## Development / Adding Registers

1. Add an entry in the `REGISTERS` dictionary in `main.py`.
2. Update the DB table (or use existing columns + additional ones).
3. Add cards/charts in the UI.
4. The app is designed read-only.

## Logs and Data

- Database: `data/sigenstor.db`
- Logs: `logs/` (if configured)
- Config: `config.json` (generated on save)

## Security

- Read-only – no write commands are sent.
- Do not expose the app publicly without authorization (nginx, auth, VPN).

## License

MIT – use freely.

---

Created according to the plan in `sigenstor-monitoring-app-prompt.md`. Happy energy monitoring! ☀️🔋
