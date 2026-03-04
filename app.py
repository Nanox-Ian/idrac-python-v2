import os
import re
import io
import time
import logging
import threading
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, Set
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.auth import HTTPBasicAuth
from flask import Flask, jsonify, render_template, Response

# Optional imports with lazy loading
HAS_MPL = False
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except Exception:
    pass

import smtplib
from email.message import EmailMessage

# =========================
# Fast Config Loading
# =========================
def load_env(dotenv_path: str = ".env"):
    if not os.path.exists(dotenv_path):
        return
    with open(dotenv_path, "r") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            name, value = s.split("=", 1)
            os.environ[name.strip()] = value.strip().strip("'\"")

load_env(".env")

# =========================
# Optimized Config
# =========================
IDRAC_URL = os.getenv("IDRAC_URL", "https://10.129.16.81")
IDRAC_HOST = re.sub(r"^https?://", "", IDRAC_URL).split("/")[0].split(":")[0]
IDRAC_USER = os.getenv("IDRAC_USER", "root")
IDRAC_PASS = os.getenv("IDRAC_PASS", "P@ssw0rd3128!")

WARNING_TEMP = float(os.getenv("WARNING_TEMP", "25"))
CRITICAL_TEMP = float(os.getenv("CRITICAL_TEMP", "30"))
SAMPLE_INTERVAL_SEC = int(os.getenv("SAMPLE_INTERVAL_SEC", "5"))
PERSIST_EMAIL_EVERY_SEC = int(os.getenv("PERSIST_EMAIL_EVERY_SEC", "300"))

# SMTP
MAIL_FROM_ADDRESS = os.getenv("MAIL_FROM_ADDRESS", "noreply@j-display.com")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "iDRAC Monitor")
EMAIL_TO = [a.strip() for a in os.getenv("EMAIL_TO", "").split(",") if a.strip()]
MAIL_HOST = os.getenv("MAIL_HOST", "mrelay.intra.j-display.com")
MAIL_PORT = int(os.getenv("MAIL_PORT", "25"))
MAIL_ENCRYPTION = os.getenv("MAIL_ENCRYPTION", "").lower()
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")

# Force URIs (comma-separated)
FORCE_URIS = [p.strip() for p in os.getenv("FORCE_URIS", "").split(",") if p.strip()]

# =========================
# Fast Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("idrac")

# =========================
# Optimized HTTP Client
# =========================
class FastRedfishClient:
    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self.user = user
        self.password = password
        
        # Optimized session with connection pooling
        self.session = requests.Session()
        self.session.verify = False
        self.session.auth = HTTPBasicAuth(user, password)
        
        # Retry strategy
        retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=retry)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        # Cache
        self.cache = {}
        self.cache_ttl = 2.0
        
        # Quick root detection
        self.root_prefix = self._detect_root_fast()
        
        # Pre-defined critical paths (fast lookup)
        self.critical_paths = [
            "/redfish/v1/Chassis/System/Embedded/Thermal",
            "/redfish/v1/Chassis/1/Thermal",
            "/redfish/v1/Chassis/System/Thermal",
            "/redfish/v1/Chassis/Server/Thermal",
        ] + FORCE_URIS

    def _detect_root_fast(self) -> str:
        """Quick root detection with timeout"""
        for path in ["/redfish/v1", "/"]:
            try:
                r = self.session.get(f"https://{self.host}{path}", timeout=2)
                if r.status_code == 200:
                    return path if path != "/" else ""
            except:
                continue
        return ""

    def _get_cached(self, path: str) -> Optional[Dict]:
        """Fast cached GET"""
        cache_key = f"{self.root_prefix}|{path}"
        now = time.time()
        
        if cache_key in self.cache:
            data, ts = self.cache[cache_key]
            if now - ts < self.cache_ttl:
                return data
        
        try:
            url = f"https://{self.host}{self.root_prefix}{path}"
            r = self.session.get(url, timeout=3)
            if r.status_code == 200:
                data = r.json()
                self.cache[cache_key] = (data, now)
                return data
        except:
            pass
        
        self.cache[cache_key] = (None, now)
        return None

    def get_temperature_fast(self) -> Tuple[bool, Optional[float], Optional[str]]:
        """Fast temperature fetch - tries critical paths first"""
        
        # 1. Try forced URIs first
        for path in self.critical_paths:
            data = self._get_cached(path)
            if data and "Temperatures" in data:
                for sensor in data["Temperatures"]:
                    name = sensor.get("Name", "").lower()
                    if any(n in name for n in ["inlet", "system inlet", "ambient"]):
                        reading = sensor.get("ReadingCelsius") or sensor.get("Reading")
                        if reading:
                            return True, float(reading), path
        
        # 2. Quick crawl limited paths
        quick_paths = ["/Chassis", "/Systems"]
        for base in quick_paths:
            data = self._get_cached(base)
            if data and "Members" in data:
                for member in data.get("Members", []):
                    thermal_path = member.get("@odata.id", "") + "/Thermal"
                    thermal_data = self._get_cached(thermal_path)
                    if thermal_data and "Temperatures" in thermal_data:
                        for sensor in thermal_data["Temperatures"]:
                            if "inlet" in sensor.get("Name", "").lower():
                                reading = sensor.get("ReadingCelsius") or sensor.get("Reading")
                                if reading:
                                    return True, float(reading), thermal_path
        
        return False, None, None

# Initialize client
client = FastRedfishClient(IDRAC_HOST, IDRAC_USER, IDRAC_PASS)

# =========================
# Fast Chart Generation
# =========================
def generate_chart_fast(points: List[Tuple[float, Optional[float]]]) -> bytes:
    """Ultra-fast chart generation"""
    if not points or not HAS_MPL:
        return b""
    
    # Filter last hour
    cutoff = time.time() - 3600
    recent = [(ts, v) for ts, v in points if ts >= cutoff and v is not None]
    
    if len(recent) < 2:
        return b""
    
    # Quick sampling
    step = max(1, len(recent) // 20)
    sampled = recent[::step]
    
    times = [datetime.fromtimestamp(ts) for ts, _ in sampled]
    temps = [v for _, v in sampled]
    
    # Fast plot
    fig, ax = plt.subplots(figsize=(4, 1.5))
    ax.plot(times, temps, color="#3b82f6", linewidth=2)
    ax.set_facecolor("#111827")
    ax.tick_params(colors="#9ca3af", labelsize=8)
    ax.grid(True, alpha=0.2)
    
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()

# =========================
# Fast Email
# =========================
def send_email_fast(subject: str, body: str, chart: bytes = None) -> bool:
    """Fast email sending"""
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM_ADDRESS}>"
        msg["To"] = ", ".join(EMAIL_TO)
        msg.set_content(body)
        
        if chart:
            msg.add_attachment(chart, maintype="image", subtype="png", filename="chart.png")
        
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=5) as server:
            if MAIL_ENCRYPTION == "tls":
                server.starttls()
            server.send_message(msg)
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        return False

# =========================
# Optimized Monitor
# =========================
class FastMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = deque(maxlen=720)  # 1 hour at 5s intervals
        self.last_temp = None
        self.last_status = "UNKNOWN"
        self.last_endpoint = None
        self.last_email_hour = -1
        self.alert_state = None
        self.last_alert = 0
        self.running = True

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.running:
            try:
                # Fast fetch
                success, temp, endpoint = client.get_temperature_fast()
                
                with self.lock:
                    self.last_temp = temp
                    self.last_endpoint = endpoint
                    
                    if temp is not None:
                        self.history.append((time.time(), temp))
                        
                        # Quick status
                        if temp >= CRITICAL_TEMP:
                            self.last_status = "CRITICAL"
                        elif temp >= WARNING_TEMP:
                            self.last_status = "WARNING"
                        else:
                            self.last_status = "NORMAL"
                        
                        # Log to temperature.log (not temp.log)
                        try:
                            os.makedirs("storage", exist_ok=True)
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            with open("storage/temperature.log", "a", encoding="utf-8") as f:
                                f.write(f"{timestamp} | {temp:.1f}°C | {self.last_status} | {endpoint or 'N/A'}\n")
                        except Exception as e:
                            logger.warning(f"Log write failed: {e}")
                
                # Quick emails
                self._check_emails(temp)
                
            except Exception as e:
                logger.error(f"Monitor error: {e}")
            
            time.sleep(SAMPLE_INTERVAL_SEC)

    def _check_emails(self, temp):
        if temp is None:
            return
            
        now = datetime.now()
        
        # Hourly email
        if now.hour != self.last_email_hour:
            chart = generate_chart_fast(list(self.history))
            subject = f"[Hourly Report] {temp:.1f}°C - {IDRAC_HOST}"
            body = f"""Temperature: {temp:.1f}°C
Status: {self.last_status}
Time: {now.strftime('%Y-%m-%d %H:%M:%S')}
Endpoint: {self.last_endpoint or 'N/A'}"""
            
            if send_email_fast(subject, body, chart):
                self.last_email_hour = now.hour
        
        # Alert emails
        if self.last_status in ("WARNING", "CRITICAL"):
            should_send = False
            
            if self.alert_state != self.last_status:
                should_send = True
                alert_type = "New Alert"
            elif (time.time() - self.last_alert) > PERSIST_EMAIL_EVERY_SEC:
                should_send = True
                alert_type = "Persistent"
            
            if should_send:
                chart = generate_chart_fast(list(self.history))
                subject = f"[{alert_type}][{self.last_status}] {temp:.1f}°C - {IDRAC_HOST}"
                body = f"""Temperature Alert!
Temperature: {temp:.1f}°C
Status: {self.last_status}
Thresholds: Warning={WARNING_TEMP}°C, Critical={CRITICAL_TEMP}°C
Time: {now.strftime('%Y-%m-%d %H:%M:%S')}
Endpoint: {self.last_endpoint or 'N/A'}"""
                
                if send_email_fast(subject, body, chart):
                    self.alert_state = self.last_status
                    self.last_alert = time.time()

# Start monitor
monitor = FastMonitor()
monitor.start()

# =========================
# Flask Routes (Fixed for Dashboard)
# =========================
app = Flask(__name__)

@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/inlet")
def api_inlet():
    """Get current inlet temperature (used by dashboard)"""
    success, temp, endpoint = client.get_temperature_fast()
    
    if success and temp is not None:
        # Format payload like original dashboard expects
        payload = {
            "reading_c": temp,
            "name": "System Inlet Temperature",
            "status": "OK" if temp < WARNING_TEMP else "Warning" if temp < CRITICAL_TEMP else "Critical",
            "endpoint": endpoint or "discovered"
        }
        return jsonify({
            "success": True, 
            "data": payload, 
            "error": None
        })
    else:
        return jsonify({
            "success": False, 
            "data": None, 
            "error": "Could not read temperature"
        })

@app.route("/api/status")
def api_status():
    """Get monitor status with log entries"""
    with monitor.lock:
        # Read last few lines from temperature.log for history
        log_entries = []
        try:
            log_path = "storage/temperature.log"
            if os.path.exists(log_path):
                with open(log_path, "r") as f:
                    lines = f.readlines()[-20:]  # Last 20 entries
                    for line in lines:
                        parts = line.strip().split(" | ")
                        if len(parts) >= 3:
                            log_entries.append({
                                "timestamp": parts[0],
                                "temp": parts[1].replace("°C", ""),
                                "status": parts[2]
                            })
        except Exception as e:
            logger.warning(f"Failed to read log: {e}")
        
        return jsonify({
            "success": True,
            "data": {
                "last_temp": monitor.last_temp,
                "last_status": monitor.last_status,
                "last_endpoint": monitor.last_endpoint,
                "history_len": len(monitor.history),
                "log_entries": log_entries
            }
        })

@app.route("/api/refresh")
def api_refresh():
    """Force immediate refresh"""
    success, temp, endpoint = client.get_temperature_fast()
    
    with monitor.lock:
        if success and temp is not None:
            monitor.last_temp = temp
            monitor.last_endpoint = endpoint
            
            # Determine status
            if temp >= CRITICAL_TEMP:
                status = "CRITICAL"
            elif temp >= WARNING_TEMP:
                status = "WARNING"
            else:
                status = "NORMAL"
            
            monitor.last_status = status
            monitor.history.append((time.time(), temp))
            
            # Log to file
            try:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                os.makedirs("storage", exist_ok=True)
                with open("storage/temperature.log", "a", encoding="utf-8") as f:
                    f.write(f"{timestamp} | {temp:.1f}°C | {status} | {endpoint or 'N/A'}\n")
            except Exception as e:
                logger.warning(f"Log write failed: {e}")
    
    return jsonify({"success": success, "temp": temp})

@app.route("/graph.png")
def graph_png():
    """Generate temperature graph"""
    with monitor.lock:
        chart = generate_chart_fast(list(monitor.history))
    return Response(chart, mimetype="image/png")

@app.route("/health")
def health():
    """Health check endpoint"""
    with monitor.lock:
        return jsonify({
            "status": "healthy" if monitor.last_temp is not None else "degraded",
            "connected": monitor.last_temp is not None,
            "timestamp": datetime.now().isoformat(),
            "temp": monitor.last_temp,
            "state": monitor.last_status
        })

@app.route("/diag")
def diag():
    """Diagnostic endpoint"""
    success, temp, endpoint = client.get_temperature_fast()
    return jsonify({
        "success": success,
        "temp": temp,
        "endpoint": endpoint,
        "forced_uris": FORCE_URIS,
        "root_prefix": client.root_prefix
    })

if __name__ == "__main__":
    os.makedirs("storage", exist_ok=True)
    print("Server starting on http://0.0.0.0:5000")
    print("Browse to http://127.0.0.1:5000 to view dashboard")
    
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000, threads=4)
    except:
        app.run(host="0.0.0.0", port=5000, threaded=True)
