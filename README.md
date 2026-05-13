# Axis Switch Manager

A central management dashboard for Axis network switches.

## Features

- **Dashboard** - At-a-glance view of all switches with PoE load, uptime, and status
- **Port Status** - Per-port link state, speed, admin state, and PoE status
- **PoE Monitoring** - Visual power meters per port with voltage/current details
- **Traffic Stats** - Rx/Tx packets and bytes per port
- **Multi-switch** - Manage any number of Axis switches from one interface

## Supported Switches

- AXIS T8508 (tested)
- AXIS T8516, T8524 series (SM24TAT2SA and similar)
- Other Axis OEM switches using the same web interface firmware

## Setup

### Requirements

- Python 3.11+
- pip

### Install dependencies

```bash
cd axis-switches-gui
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi "uvicorn[standard]" httpx
```

### Run

```bash
./start.sh
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

### Add your first switch

1. Go to **Switches** in the sidebar
2. Click **+ Add Switch**
3. Enter the switch name, IP address, and login credentials
4. Click **Save**

The dashboard will immediately begin polling the switch for data.

## Architecture

```
axis-switches-gui/
  backend/
    main.py       # FastAPI app - proxies and parses switch API calls
  frontend/
    index.html    # Single-page application shell
    style.css     # All styles
    app.js        # All client-side logic
  switches.json   # Switch inventory (auto-created)
  start.sh        # Launch script
```

## Security Notes

- Switch credentials are stored in `switches.json` in plaintext. Keep this file protected.
- The backend must have network access to all managed switches.
- The web interface is unauthenticated by default; run it on a trusted network.
