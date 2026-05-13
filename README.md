# Axis Switch Manager

A central management dashboard for Axis network switches. Monitor and configure all your switches from a single web interface.

## Features

- **Dashboard** - At-a-glance view of all switches with PoE load, uptime, and port status
- **Network Scan** - Scan a subnet to discover and add multiple switches at once
- **Port Monitoring** - Per-port link state, speed, admin state, and LLDP neighbour info
- **PoE Monitoring** - Visual power meters per port with voltage, current, and class details
- **Traffic Stats** - Rx/Tx packets, bytes, errors, and drops per port
- **Full Configuration** - Per-switch configuration covering:
  - System info (name, location, contact)
  - NTP / time server
  - Port admin state, flow control, MTU, and descriptions
  - PoE per-port enable, priority, and power budget
  - Loop protection (global + per-port action)
  - VLAN port configuration (access/trunk/hybrid, PVID, allowed VLANs)
  - Private VLAN / port isolation
  - Link aggregation status
- **Bulk Configure** - Apply PoE or port settings across multiple switches at once

## Supported Switches

- AXIS T8508 (tested)
- AXIS T8516, T8524 (SM24TAT2SA and similar)
- Other Axis OEM switches using the same web interface firmware

## Running with Docker (recommended)

### Pull and start

```bash
docker compose up -d
```

The app will be available at [http://localhost:8000](http://localhost:8000).

Switch inventory is stored in `backend/switches.json` on the host and mounted into the container, so it persists across updates.

### Build locally instead of pulling

```bash
docker compose up -d --build
```

### Docker image

The image is automatically built and published to the GitHub Container Registry on every push to `main`:

```
ghcr.io/mo3he/axis-switch-manager:latest
```

## Running without Docker

### Requirements

- Python 3.11+

### Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./start.sh
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

## Adding switches

1. Go to **Switches** in the sidebar
2. Click **+ Add Switch** to add one manually, or **Scan Network** to discover switches on a subnet
3. Enter the switch name, IP address, and login credentials
4. Click **Save**

Switch data is stored in `backend/switches.json`. Copy `backend/switches.json.example` as a starting point.

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
