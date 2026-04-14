# iDRAC Inlet Temperature Monitor

A **production‑ready, offline‑friendly iDRAC temperature monitoring system** with a modern web UI, combined email alerts, spike‑suppression logic, historical graphing, and per‑server log export.

Designed for **datacenter / lab environments** where:

*   Internet access may be restricted
*   Accuracy and alert discipline matter
*   Operators need clear visibility, not noise

***

## Key Features

### Multi‑Server Monitoring

*   Monitor **multiple Dell iDRAC endpoints** at once
*   Same credentials supported across all servers
*   Uses Redfish API over HTTPS

### Real‑Time Dashboard (Web UI)

*   **Instant temperature visibility** on page load
*   Server cards with:
    *   Current inlet temperature
    *   Health status (NORMAL / WARNING / CRITICAL)
    *   Last update timestamp
*   **All primary information fits on ONE desktop screen**
*   No scrolling unless logs are opened

### Historical Graph (Per Server)

*   View **last 1 hour temperature trend**
*   Server‑selectable via dropdown
*   Fixed‑height chart (no stretching / layout issues)
*   Works fully **offline** using a locally hosted Chart.js

### Integrated Logs (User‑Friendly)

*   View recent temperature logs directly in the browser
*   Filter logs by selected server
*   Logs are hidden by default (shown only on demand)
*   Download CSV logs:
    *   Per server
    *   Or all servers combined

### Smart Email Alerting (No Spam)

*   **One single email**, not one per server
*   Clear HTML table with all servers included
*   Two types of emails:
    *   Hourly summary
    *   Sustained alert

### Spike‑Suppression Logic (Critical Feature)

Prevents false alerts caused by short temperature spikes.

**Behavior:**

1.  Sudden spike (e.g. 25 °C → 30 °C)  
    → **No alert**
2.  Temperature remains high for **5 continuous minutes**  
    → **Alert is sent**
3.  Temperature remains high after alert  
    → **No repeated alerts**
4.  After **30 minutes** still high  
    → **Another alert**
5.  Temperature returns to normal  
    → Alert system resets

This mirrors real NOC / datacenter alert practice.

***

## Project Structure

```text
.
├── app.py                     # Flask backend + monitor logic
├── .env                       # Configuration and credentials
├── storage/
│   ├── temperature.log        # Human‑readable log
│   ├── idrac_log.csv           # CSV data for graphs & export
│   └── app.log                # Application log
├── templates/
│   └── dashboard.html         # Web UI
└── static/
    └── chart.umd.min.js       # Chart.js (local, offline‑safe)
```

***

## Configuration (`.env`)

Example:

```env
IDRAC_SERVERS=10.129.16.81,10.129.16.82,10.129.16.84
IDRAC_USER=root
IDRAC_PASS=yourpassword

WARNING_TEMP=25
CRITICAL_TEMP=30

SAMPLE_INTERVAL_SEC=10

MAIL_FROM_ADDRESS=noreply@example.com
MAIL_FROM_NAME="iDRAC Monitor"
EMAIL_TO=admin1@example.com,admin2@example.com
MAIL_HOST=mail.example.com
MAIL_PORT=25
```

***

## Web UI Overview

### Temperature Cards (Top Priority)

*   Shows **current inlet temperature**
*   Color‑coded:
    *   ✅ Green – NORMAL
    *   ⚠️ Orange – WARNING
    *   🔴 Red – CRITICAL
*   This is what operators should look at first

### Graph Section

*   Select a server from dropdown
*   Shows last **1 hour temperature history**
*   Fixed size, stable layout
*   Updated manually (no auto‑reset)

### Logs Section

*   Hidden by default
*   View logs for the selected server
*   Scrollable only when opened
*   Download logs as CSV

***

## API Endpoints (For Developers)

| Endpoint               | Method | Description                     |
| ---------------------- | ------ | ------------------------------- |
| `/`                    | GET    | Dashboard UI                    |
| `/api/state`           | GET    | Live state of all servers       |
| `/api/history?host=IP` | GET    | 1‑hour history for graph        |
| `/api/logs?host=IP`    | GET    | Recent logs for selected server |
| `/api/export?host=IP`  | GET    | Download CSV logs               |

***

## Email Behavior

### Hourly Summary

*   Sent **exactly on HH:00**
*   One email only
*   Includes all servers in a table

### Alert Email

*   Sent only after sustained high temp (≥ 5 minutes)
*   Sent once per incident
*   Re‑sent only if condition persists for 30 minutes
*   Automatically resets when temperature normalizes

***

## Offline / Restricted Network Ready

*   No external CDN usage
*   Chart.js is served locally from `/static`
*   No internet required after setup
*   Suitable for air‑gapped environments

***

## Running the Application

```bash
python app.py
```

Access the dashboard:

    http://localhost:5000

***

## Why This Design Works

*   **User‑first UI**: temperature first, logs second
*   **Alert discipline**: no spam, no noise
*   **Operational realism**: mirrors real datacenter monitoring logic
*   **Maintainable**: clean Flask backend, simple frontend
*   **Safe**: no reliance on external networks

***

## Possible Future Enhancements

*   Threshold lines on graph (warning / critical)
*   Multi‑server combined graph
*   6h / 24h history selector
*   Attach graph image to emails
*   Authentication for dashboard access

***

## License / Usage

Internal tooling / educational use.  
Adapt and extend as needed for your environment.
