import os
import re
import io
import time
import logging
import threading
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, Set

import requests
from requests.auth import HTTPBasicAuth
from flask import Flask, jsonify, render_template, Response

# Optional plotting libs (for email graph)
HAS_MPL = False
HAS_PIL = False
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except Exception:
    HAS_MPL = False

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except Exception:
    HAS_PIL = False

import smtplib
from email.message import EmailMessage

# =========================
# .env Loader (PHP-like)
# =========================
def load_env(dotenv_path: str = ".env"):
    if not os.path.exists(dotenv_path):
        return
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            name, value = s.split("=", 1)
            name, value = name.strip(), value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[name] = value

load_env(".env")

def _host_from_url(url: str) -> str:
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url.strip(), flags=re.I)
    url = url.split("/", 1)[0]
    url = url.split(":", 1)[0]
    return url

# =========================
# Config
# =========================
IDRAC_URL  = os.getenv("IDRAC_URL", "https://10.129.16.81")
IDRAC_HOST = os.getenv("IDRAC_HOST", _host_from_url(IDRAC_URL))
IDRAC_USER = os.getenv("IDRAC_USER", "root")
IDRAC_PASS = os.getenv("IDRAC_PASS", "P@ssw0rd3128!")

# Temperature thresholds
NORMAL_TEMP_MAX = float(os.getenv("NORMAL_TEMP_MAX", "24"))  # Below 25 is normal
WARNING_TEMP = float(os.getenv("WARNING_TEMP", "25"))        # 25-29 is warning
CRITICAL_TEMP = float(os.getenv("CRITICAL_TEMP", "30"))      # 30+ is critical

# Monitor intervals
SAMPLE_INTERVAL_SEC = int(os.getenv("SAMPLE_INTERVAL_SEC", "5"))
PERSIST_EMAIL_EVERY_SEC = int(os.getenv("PERSIST_EMAIL_EVERY_SEC", "300"))  # 5 minutes persistent alert

# SMTP (PHP-like)
MAIL_FROM_ADDRESS = os.getenv("MAIL_FROM_ADDRESS", "noreply@j-display.com")
MAIL_FROM_NAME    = os.getenv("MAIL_FROM_NAME", "iDRAC Monitor")
EMAIL_TO          = [a.strip() for a in os.getenv("EMAIL_TO",
                      "supercompnxp@gmail.com, ian.tolentino.bp@j-display.com, lecelannharvey.echavarre.bn@j-display.com, ferrerasroyce@gmail.com, raffy.santiago.rbs@gmail.com, wongjm@ymail.com"
                   ).split(",") if a.strip()]

MAIL_HOST      = os.getenv("MAIL_HOST", "mrelay.intra.j-display.com")
MAIL_PORT      = int(os.getenv("MAIL_PORT", "25"))
MAIL_ENCRYPTION= os.getenv("MAIL_ENCRYPTION", "").lower().strip()   # "", "tls", "ssl"
MAIL_USERNAME  = os.getenv("MAIL_USERNAME", "")
MAIL_PASSWORD  = os.getenv("MAIL_PASSWORD", "")
SMTP_AUTH      = bool(MAIL_USERNAME)

# Optional: force known-good Redfish URIs (comma-separated)
FORCE_URIS = [p.strip() for p in os.getenv("FORCE_URIS", "").split(",") if p.strip()]

# Logging
logging.basicConfig(
    filename="idrac_monitor.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("idrac")

# CSV log
CSV_LOG_FILE = "idrac_log.csv"
os.makedirs("storage", exist_ok=True)

# Temperature log
TEMP_LOG_FILE = os.path.join("storage", "temperature.log")

# Flask
app = Flask(__name__)
requests.packages.urllib3.disable_warnings()  # silence self-signed warnings

# Acceptable names for inlet
PREFERRED_NAMES = [
    "System Inlet Temperature",
    "System Board Inlet Temp",
    "Inlet Temperature",
    "Inlet Temp",
    "Chassis Inlet Temp",
    "Inlet Ambient",
]

# =========================
# Redfish Client (auto root + dual-path retry + BFS discovery)
# =========================
class RedfishClient:
    """
    - Auto-detects root: '/' vs '/redfish/v1'
    - For every path, tries both root forms if needed
    - Crawls via @odata.id links to find System Inlet Temperature
    - Returns ONLY the System Inlet Temperature sensor
    """
    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self.scheme = "https"
        self.user = user
        self.password = password
        self.session = requests.Session()
        self.session.verify = False  # iDRAC typically has self-signed cert
        self.session.auth = HTTPBasicAuth(self.user, self.password)
        self.has_token = False

        # Which root prefix works? '' or '/redfish/v1'
        self.root_prefix = self._detect_root_prefix()

        # cache endpoint -> (status, json, text)
        self.cache: Dict[str, Tuple[int, Optional[Dict[str, Any]], Optional[str]]] = {}
        self.cache_ts: Dict[str, float] = {}
        self.cache_ttl = 3.0  # shorter cache for quicker updates

    def _detect_root_prefix(self) -> str:
        try:
            s1, _, _ = self._raw_get("/redfish/v1", use_prefix=None)
            if s1 == 200:
                logger.info("Using root prefix: /redfish/v1")
                return "/redfish/v1"
        except Exception:
            pass
        logger.info("Using root prefix: '' (no /redfish/v1)")
        return ""

    def _join(self, path: str, prefix: Optional[str] = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if prefix is None:
            prefix = self.root_prefix
        if prefix and path.startswith(prefix):
            url_path = path
        else:
            url_path = (prefix + path) if prefix else path
        return f"{self.scheme}://{self.host}{url_path}"

    def _raw_get(self, path: str, use_prefix: Optional[str]) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
        url = self._join(path, prefix=use_prefix)
        try:
            r = self.session.get(url, headers={"Accept": "application/json"}, timeout=8)
            if r.status_code == 200:
                try:
                    return r.status_code, r.json(), r.text
                except Exception:
                    return r.status_code, None, r.text
            if r.status_code in (401, 403) and not self.has_token:
                if self._login_token():
                    r2 = self.session.get(url, headers={"Accept": "application/json"}, timeout=8)
                    if r2.status_code == 200:
                        try:
                            return r2.status_code, r2.json(), r2.text
                        except Exception:
                            return r2.status_code, None, r2.text
                    return r2.status_code, None, r2.text
            return r.status_code, None, r.text
        except Exception as e:
            return 0, None, str(e)

    def _get(self, path: str) -> Tuple[int, Optional[Dict[str, Any]], Optional[str], str]:
        now = time.time()
        cache_key = f"{self.root_prefix}|{path}"
        if cache_key in self.cache and (now - self.cache_ts.get(cache_key, 0)) < self.cache_ttl:
            st, js, tx = self.cache[cache_key]
            return st, js, tx, "cached"

        st, js, tx = self._raw_get(path, use_prefix=self.root_prefix)
        final_form = f"root={self.root_prefix or '/'}"
        if st == 404:
            alt = "/redfish/v1" if self.root_prefix == "" else ""
            st2, js2, tx2 = self._raw_get(path, use_prefix=alt)
            if st2 == 200:
                self.cache[cache_key] = (st2, js2, tx2)
                self.cache_ts[cache_key] = now
                return st2, js2, tx2, f"root={alt or '/'}"
            else:
                self.cache[cache_key] = (st2, None, tx2)
                self.cache_ts[cache_key] = now
                return st2, None, tx2, f"root={alt or '/'}"
        else:
            self.cache[cache_key] = (st, js, tx)
            self.cache_ts[cache_key] = now
            return st, js, tx, final_form

    def _login_token(self) -> bool:
        for prefix in (self.root_prefix, ("/redfish/v1" if self.root_prefix == "" else "")):
            try:
                url = self._join("/SessionService/Sessions", prefix=prefix)
                r = self.session.post(url, json={"UserName": self.user, "Password": self.password},
                                      headers={"Content-Type": "application/json"}, timeout=8)
                if r.status_code in (200, 201):
                    tok = r.headers.get("X-Auth-Token")
                    if tok:
                        self.session.headers.update({"X-Auth-Token": tok})
                        self.session.auth = None
                        self.has_token = True
                        logger.info("Obtained session token (prefix=%s)", prefix or "/")
                        return True
            except Exception as e:
                logger.warning("Token attempt error (%s): %s", prefix or "/", e)
        return False

    def _collect_links(self, obj: Any) -> List[str]:
        found: List[str] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "@odata.id" and isinstance(v, str) and v.startswith("/"):
                    found.append(v)
                else:
                    found.extend(self._collect_links(v))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(self._collect_links(item))
        elif isinstance(obj, str):
            if obj.startswith("/"):
                found.append(obj)
        return found

    def crawl(self, seeds: List[str], max_nodes: int = 60) -> Dict[str, Dict[str, Any]]:
        visited: Set[str] = set()
        report: Dict[str, Dict[str, Any]] = {}
        q: deque[str] = deque()

        for s in seeds:
            if not s.startswith("/"):
                s = "/" + s
            if s not in visited:
                visited.add(s); q.append(s)

        nodes = 0
        while q and nodes < max_nodes:
            uri = q.popleft()
            nodes += 1
            status, data, text, form = self._get(uri)
            entry = {"status": status, "has_temperatures": False, "inlet_found": False, "sensor_sample": None, "form": form}
            if status == 200 and isinstance(data, dict):
                temps = data.get("Temperatures")
                if isinstance(temps, list) and temps:
                    entry["has_temperatures"] = True
                    inlet = self._pick_inlet_from_temperatures(temps)
                    if inlet:
                        entry["inlet_found"] = True
                        entry["sensor_sample"] = self._normalize_sensor(inlet)

                members = data.get("Members")
                if isinstance(members, list):
                    for m in members:
                        if isinstance(m, dict) and isinstance(m.get("@odata.id"), str):
                            link = m["@odata.id"]
                            if link not in visited and link.startswith("/"):
                                visited.add(link); q.append(link)

                for link in set(self._collect_links(data)):
                    if link not in visited and link.startswith("/"):
                        visited.add(link); q.append(link)

                for key in ("Thermal", "Sensors", "ThermalSubsystem", "EnvironmentMetrics"):
                    val = data.get(key)
                    if isinstance(val, dict):
                        oid = val.get("@odata.id")
                        if isinstance(oid, str) and oid.startswith("/") and oid not in visited:
                            visited.add(oid); q.append(oid)

            report[uri] = entry
        return report

    @staticmethod
    def _name_of(sensor: Dict[str, Any]) -> str:
        return (sensor.get("Name") or sensor.get("SensorName") or "").strip()

    @staticmethod
    def _reading_of(sensor: Dict[str, Any]) -> Optional[float]:
        val = sensor.get("ReadingCelsius")
        if val is None:
            val = sensor.get("Reading")
        try:
            return float(val) if val is not None else None
        except Exception:
            return None

    @staticmethod
    def _status_of(sensor: Dict[str, Any]) -> str:
        return (sensor.get("Status") or {}).get("Health") or sensor.get("Health") or "Unknown"

    def _pick_inlet_from_temperatures(self, temps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not temps:
            return None
        exact = [s for s in temps if self._name_of(s).lower() in [n.lower() for n in PREFERRED_NAMES]]
        if exact:
            for s in exact:
                if self._reading_of(s) is not None:
                    return s
            return exact[0]
        contains = [s for s in temps if "inlet" in self._name_of(s).lower()]
        if contains:
            for s in contains:
                if self._reading_of(s) is not None:
                    return s
            return contains[0]
        ctx_hits = [s for s in temps if (s.get("PhysicalContext") or "").lower() in {"inlet", "intake", "intakeair"}]
        if ctx_hits:
            for s in ctx_hits:
                if self._reading_of(s) is not None:
                    return s
            return ctx_hits[0]
        return None

    def _normalize_sensor(self, sensor: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": self._name_of(sensor) or "System Inlet Temperature",
            "reading_c": self._reading_of(sensor),
            "status": self._status_of(sensor),
            "physical_context": sensor.get("PhysicalContext"),
            "upper_threshold": sensor.get("UpperThresholdNonCritical"),
        }

    def get_system_inlet(self) -> Tuple[bool, Dict[str, Any], List[Dict[str, Any]]]:
        attempts: List[Dict[str, Any]] = []

        # 0) Forced URIs first (fast path)
        for forced in FORCE_URIS:
            st, js, tx, form = self._get(forced)
            found_temps = bool(st == 200 and isinstance(js, dict) and isinstance(js.get("Temperatures"), list))
            inlet_found = False
            detail = None
            if found_temps:
                s = self._pick_inlet_from_temperatures(js["Temperatures"])
                if s:
                    inlet_found = True
                    norm = self._normalize_sensor(s)
                    attempts.append({"endpoint": forced, "status": st, "has_temperatures": True, "inlet_found": True, "detail": f"forced ({form})"})
                    return True, {**norm, "endpoint": f"{forced} [{form}]", "source": "Forced"}, attempts
                detail = "Temps present but no inlet match"
            attempts.append({"endpoint": forced, "status": st, "has_temperatures": found_temps, "inlet_found": inlet_found, "detail": f"forced ({form})"})

        # 1) Crawl quickly
        seeds = ["/Chassis", "/Systems", "/Managers", "/redfish/v1"]
        crawl_map = self.crawl(seeds=seeds, max_nodes=60)

        for uri, info in crawl_map.items():
            attempts.append({
                "endpoint": uri,
                "status": info.get("status"),
                "has_temperatures": bool(info.get("has_temperatures")),
                "inlet_found": bool(info.get("inlet_found")),
                "detail": info.get("form"),
            })

        for uri, info in crawl_map.items():
            if info.get("inlet_found") and info.get("sensor_sample"):
                norm = info["sensor_sample"]
                return True, {**norm, "endpoint": f"{uri} [{info.get('form','') or ''}]", "source": "Discovered"}, attempts

        return False, {"message": "System Inlet Temperature not found via discovery"}, attempts


client = RedfishClient(IDRAC_HOST, IDRAC_USER, IDRAC_PASS)

# =========================
# Email utilities (with debug)
# =========================
_last_smtp_error: Optional[str] = None

def send_email(subject: str, html_body: str, text_body: Optional[str] = None, attachments: Optional[List[Tuple[str, str, bytes]]] = None) -> bool:
    global _last_smtp_error
    _last_smtp_error = None

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM_ADDRESS}>"
    msg["To"] = ", ".join(EMAIL_TO)
    # extra headers like PHP
    msg["Reply-To"] = MAIL_FROM_ADDRESS
    msg["X-Mailer"] = "iDRAC-Monitor/1.0"

    if text_body:
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
    else:
        msg.set_content(html_body, subtype="html")

    if attachments:
        for fname, mime, data in attachments:
            maintype, subtype = mime.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)

    try:
        if MAIL_ENCRYPTION == "ssl":
            with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=15) as s:
                if SMTP_AUTH:
                    s.login(MAIL_USERNAME, MAIL_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=15) as s:
                if MAIL_ENCRYPTION == "tls":
                    s.starttls()
                if SMTP_AUTH:
                    s.login(MAIL_USERNAME, MAIL_PASSWORD)
                s.send_message(msg)
        logger.info("Email sent: %s", subject)
        return True
    except Exception as e:
        _last_smtp_error = f"{type(e).__name__}: {e}"
        logger.error("Email send failed: %s", _last_smtp_error)
        return False

def classify(temp: Optional[float]) -> str:
    """Classify temperature based on thresholds"""
    if temp is None:
        return "UNKNOWN"
    if temp >= CRITICAL_TEMP:
        return "CRITICAL"
    if temp >= WARNING_TEMP:
        return "WARNING"
    return "NORMAL"

def get_status_color(status: str) -> str:
    """Get color for status"""
    colors = {
        "NORMAL": "#27AE60",     # Green
        "WARNING": "#F39C12",    # Orange
        "CRITICAL": "#E74C3C",   # Red
        "UNKNOWN": "#95A5A6"     # Gray
    }
    return colors.get(status, "#95A5A6")

def build_email_subject(kind: str, temp: Optional[float]) -> str:
    host = IDRAC_HOST
    state = classify(temp)
    temp_txt = "N/A" if temp is None else f"{temp:.1f}°C"
    return f"[iDRAC {kind}] {state} — {temp_txt} — {host}"

def build_email_body(kind: str, temp: Optional[float], timestamp: str, endpoint: Optional[str]) -> Tuple[str, str]:
    state = classify(temp)
    temp_txt = "N/A" if temp is None else f"{temp:.1f}°C"
    endpoint_txt = endpoint or "(discovered)"
    status_color = get_status_color(state)
    
    html = f"""
    <html><body style="font-family:Segoe UI, Arial, sans-serif;">
      <h2 style="color: {status_color};">iDRAC Temperature {kind}</h2>
      <p><b>Status:</b> <span style="color: {status_color}; font-weight: bold;">{state}</span></p>
      <p><b>Temperature:</b> {temp_txt}</p>
      <p><b>Thresholds:</b> Normal ≤{NORMAL_TEMP_MAX}°C | Warning {WARNING_TEMP}-{CRITICAL_TEMP-1}°C | Critical ≥{CRITICAL_TEMP}°C</p>
      <p><b>Time:</b> {timestamp}</p>
      <p><b>Host:</b> {IDRAC_HOST}</p>
      <p><b>Endpoint:</b> <code>{endpoint_txt}</code></p>
      <hr/>
      <p style="color: #7F8C8D; font-size: 0.9em;">This message was generated automatically by iDRAC Monitor.</p>
      <p style="color: #7F8C8D; font-size: 0.9em;"><em>Do not reply to this email; this is an automated monitoring system.</em></p>
    </body></html>
    """
    text = f"""iDRAC Temperature {kind}
Status: {state}
Temperature: {temp_txt}
Thresholds: Normal ≤{NORMAL_TEMP_MAX}°C | Warning {WARNING_TEMP}-{CRITICAL_TEMP-1}°C | Critical ≥{CRITICAL_TEMP}°C
Time: {timestamp}
Host: {IDRAC_HOST}
Endpoint: {endpoint_txt}

Do not reply to this email; this is an automated monitoring system.
"""
    return html, text

# =========================
# Chart generation (10‑min steps, last 20 readings)
# =========================
def resample_10min_last(points: List[Tuple[float, Optional[float]]], max_points: int = 20) -> List[Tuple[float, Optional[float]]]:
    """
    Group all available history points into 10-minute buckets and pick the latest
    sample per bucket. Return the last `max_points` buckets (chronological order).
    """
    if not points:
        return []
    # Keep only the most recent sample in each 10-min bucket
    buckets: Dict[int, Tuple[float, Optional[float]]] = {}
    for ts, v in points:
        key = int(ts // 600)  # 600s = 10 minutes
        # Keep the latest timestamp in the bucket
        if (key not in buckets) or (ts > buckets[key][0]):
            buckets[key] = (ts, v)

    if not buckets:
        return []

    # Sort by bucket (descending), take last `max_points`, then restore chrono order
    keys_desc = sorted(buckets.keys(), reverse=True)[:max_points]
    selected = [buckets[k] for k in reversed(keys_desc)]
    return selected

def chart_png_from_points(points: List[Tuple[float, Optional[float]]]) -> bytes:
    xs = [datetime.fromtimestamp(ts) for ts, _ in points]
    ys = [None if v is None else float(v) for _, v in points]

    if not xs:
        xs = [datetime.now()]
        ys = [None]

    if HAS_MPL:
        fig, ax = plt.subplots(figsize=(6, 2))
        ax.set_facecolor("#111827")
        fig.patch.set_alpha(0)
        ax.grid(color="#334155", alpha=0.3, linestyle="--", linewidth=0.6)
        ax.tick_params(axis='x', colors="#9ca3af", labelsize=8, rotation=0)
        ax.tick_params(axis='y', colors="#9ca3af", labelsize=8)
        ax.spines["bottom"].set_color("#334155")
        ax.spines["top"].set_color("#334155")
        ax.spines["left"].set_color("#334155")
        ax.spines["right"].set_color("#334155")

        # Add threshold lines
        ax.axhline(y=WARNING_TEMP, color="#F39C12", linestyle="--", linewidth=1, alpha=0.5)
        ax.axhline(y=CRITICAL_TEMP, color="#E74C3C", linestyle="--", linewidth=1, alpha=0.5)

        x_plot = []
        y_plot = []
        for x, y in zip(xs, ys):
            if y is not None:
                x_plot.append(x)
                y_plot.append(y)
        if x_plot:
            ax.plot(x_plot, y_plot, color="#3b82f6", linewidth=2, marker="o", markersize=3)

        # Updated label for clarity
        ax.set_xlabel("10‑min steps (last 20 readings)", color="#9ca3af")
        ax.set_ylabel("°C", color="#9ca3af")
        ax.set_ylim(bottom=0)
        fig.tight_layout()
        bio = io.BytesIO()
        fig.savefig(bio, format="png", dpi=130, bbox_inches="tight")
        plt.close(fig)
        return bio.getvalue()

    # PIL fallback (unchanged except label isn't drawn in PIL mode)
    W, H = 600, 160
    bg = (17, 24, 39)
    line = (59, 130, 246)
    grid = (51, 65, 85)
    im = Image.new("RGB", (W, H), bg)
    dr = ImageDraw.Draw(im)

    for y in range(20, H, 40):
        dr.line((0, y, W, y), fill=grid)
    for x in range(0, W, 80):
        dr.line((x, 0, x, H), fill=grid)

    vals = [y for y in ys if y is not None]
    if vals:
        vmin = min(vals); vmax = max(vals)
        if vmax == vmin:
            vmax = vmin + 1.0
        step = W / max(1, (len(vals) - 1))
        pts_line = []
        for i, y in enumerate(vals):
            x = int(i * step)
            yy = int(H - (y - vmin) / (vmax - vmin) * (H - 20) - 10)
            pts_line.append((x, yy))
        if len(pts_line) >= 2:
            dr.line(pts_line, fill=line, width=3)

    bio = io.BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()

# =========================
# Temperature Log Writer
# =========================
def log_temperature(temp: Optional[float], status: str, endpoint: Optional[str]):
    """Append temperature data to temperature.log"""
    try:
        os.makedirs("storage", exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        temp_str = "N/A" if temp is None else f"{temp:.1f}°C"
        endpoint_str = endpoint or "N/A"
        
        line = f"{timestamp} | {temp_str} | {status} | {endpoint_str}\n"
        
        with open(TEMP_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.warning("Temperature log write failed: %s", e)

def _append_past_hour_stats(html: str, text: str, history: deque) -> Tuple[str, str]:
    """Append past-hour min/avg/max to the email body (HTML and text) at the end."""
    now = time.time()
    hour_vals = [v for ts, v in list(history) if ts >= now - 3600 and v is not None]
    if not hour_vals:
        return html, text
    vmin = min(hour_vals); vmax = max(hour_vals)
    vavg = sum(hour_vals) / len(hour_vals)
    html_insert = f'<p><b>Past hour:</b> min {vmin:.1f}°C • avg {vavg:.1f}°C • max {vmax:.1f}°C</p>'
    # Insert before closing tags
    html = html.replace("</body></html>", html_insert + "</body></html>")
    text += f"\nPast hour: min {vmin:.1f}°C, avg {vavg:.1f}°C, max {vmax:.1f}°C\n"
    return html, text

# =========================
# Monitor thread (for emails + logging)
# =========================
class TempMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.history: deque[Tuple[float, Optional[float]]] = deque(maxlen=3600)
        self.last_temp: Optional[float] = None
        self.last_status: str = "UNKNOWN"
        self.last_endpoint: Optional[str] = None

        # email state
        self.last_hourly_sent_hour: Optional[int] = None
        self.alert_state: Optional[str] = None  # WARNING/CRITICAL
        self.last_alert_time: float = 0.0

        self.last_hourly_sent_ts: Optional[str] = None
        self.last_alert_sent_ts: Optional[str] = None

        self.thread = threading.Thread(target=self._run, name="TempMonitor", daemon=True)
        self.stop_event = threading.Event()

    def start(self):
        if not self.thread.is_alive():
            self.stop_event.clear()
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def _sample_once(self):
        ts = time.time()
        ok, payload, _ = client.get_system_inlet()
        if ok:
            temp = payload.get("reading_c")
            endpoint = payload.get("endpoint")
        else:
            temp = None
            endpoint = None

        status = classify(temp)
        with self.lock:
            self.last_temp = temp
            self.last_status = status
            self.last_endpoint = endpoint
            self.history.append((ts, temp))
            
            # Log to temperature.log
            log_temperature(temp, status, endpoint)
            
            # CSV log every 5 minutes
            minute = int(datetime.now().strftime("%M"))
            if minute % 5 == 0 and temp is not None:
                try:
                    with open(CSV_LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')},{temp:.1f},{status}\n")
                except Exception as e:
                    logger.warning("CSV write failed: %s", e)

    def _run(self):
        logger.info("TempMonitor started (interval=%ss)", SAMPLE_INTERVAL_SEC)

        # FIRST FETCH IMMEDIATELY
        self._sample_once()

        # THEN LOOP
        while not self.stop_event.is_set():
            self.stop_event.wait(SAMPLE_INTERVAL_SEC)
            self._sample_once()
            # Emails
            self._maybe_send_hourly()
            self._maybe_send_alerts()

    def _maybe_send_hourly(self):
        """Send hourly email at every hour (00-23)"""
        now = datetime.now()
        current_hour = now.hour
        
        with self.lock:
            # Send if we haven't sent for this hour yet
            if self.last_hourly_sent_hour != current_hour:
                temp = self.last_temp
                ep = self.last_endpoint
                subject = build_email_subject("Hourly Report", temp)
                html, text = build_email_body("Hourly Report", temp, now.strftime("%Y-%m-%d %H:%M:%S"), ep)
                html, text = _append_past_hour_stats(html, text, self.history)
                chart = self._build_chart_attachment()
                ok = send_email(subject, html, text, attachments=chart)
                if ok:
                    self.last_hourly_sent_hour = current_hour
                    self.last_hourly_sent_ts = now.strftime("%Y-%m-%d %H:%M:%S")
                    logger.info(f"Hourly email sent for hour {current_hour}:00")

    def _maybe_send_alerts(self):
        now_ts = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            st = self.last_status
            temp = self.last_temp
            ep = self.last_endpoint

            if st in ("WARNING", "CRITICAL"):
                # New alert state
                if self.alert_state != st:
                    subject = build_email_subject(f"{st} Alert", temp)
                    html, text = build_email_body(f"{st} Alert", temp, now_str, ep)
                    html, text = _append_past_hour_stats(html, text, self.history)
                    chart = self._build_chart_attachment()
                    if send_email(subject, html, text, attachments=chart):
                        self.alert_state = st
                        self.last_alert_time = now_ts
                        self.last_alert_sent_ts = now_str
                        logger.info(f"New {st} alert sent")
                        return
                
                # Persistent alert (every 5 minutes)
                if self.alert_state == st and (now_ts - self.last_alert_time) >= PERSIST_EMAIL_EVERY_SEC:
                    subject = build_email_subject(f"{st} (Persistent)", temp)
                    html, text = build_email_body(f"{st} (Persistent)", temp, now_str, ep)
                    html, text = _append_past_hour_stats(html, text, self.history)
                    chart = self._build_chart_attachment()
                    if send_email(subject, html, text, attachments=chart):
                        self.last_alert_time = now_ts
                        self.last_alert_sent_ts = now_str
                        logger.info(f"Persistent {st} alert sent")
            else:
                # Reset alert state when back to normal
                self.alert_state = None

    def _build_chart_attachment(self) -> List[Tuple[str, str, bytes]]:
        pts = list(self.history)
        pts_10m_20 = resample_10min_last(pts, max_points=20)
        png = chart_png_from_points(pts_10m_20)
        return [("last_20x10min.png", "image/png", png)]

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            # Read last 20 lines from temperature.log
            log_entries = []
            try:
                if os.path.exists(TEMP_LOG_FILE):
                    with open(TEMP_LOG_FILE, "r", encoding="utf-8") as f:
                        lines = f.readlines()[-20:]
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
            
            return {
                "last_temp": self.last_temp,
                "last_status": self.last_status,
                "last_endpoint": self.last_endpoint,
                "last_hourly_sent": self.last_hourly_sent_ts,
                "last_alert_sent": self.last_alert_sent_ts,
                "alert_state": self.alert_state,
                "history_len": len(self.history),
                "log_entries": log_entries,
                "thresholds": {
                    "normal_max": NORMAL_TEMP_MAX,
                    "warning": WARNING_TEMP,
                    "critical": CRITICAL_TEMP
                }
            }

    def last_hour_points(self) -> List[Tuple[float, Optional[float]]]:
        with self.lock:
            return resample_last_hour(list(self.history), target_pts=20)

monitor = TempMonitor()
monitor.start()

# =========================
# Routes
# =========================
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/inlet")
def api_inlet():
    # Serve cached last sample to avoid slow live Redfish crawl on page load
    s = monitor.snapshot()
    ok = s["last_temp"] is not None
    data = None
    if ok:
        data = {
            "name": "System Inlet Temperature",
            "reading_c": s["last_temp"],
            "status": s["last_status"],
            "endpoint": s["last_endpoint"],
            "source": "Cached"
        }
    return jsonify({"success": ok, "data": data, "error": None if ok else "No recent sample", "attempts": []})

@app.route("/diag")
def diag():
    results = {}
    for p in ("/redfish/v1", "/Chassis", "/Systems", "/Managers"):
        st, js, tx, form = client._get(p)
        results[p] = {"status": st, "ok": st == 200, "form": form, "keys": list(js.keys())[:10] if isinstance(js, dict) else []}
    ok, inlet_payload, attempts = client.get_system_inlet()
    return jsonify({"probes": results, "inlet": {"success": ok, "payload": inlet_payload if ok else None}, "attempts": attempts})

@app.route("/health")
def health():
    s = monitor.snapshot()
    return jsonify({
        "status": "healthy" if s["last_status"] != "UNKNOWN" else "degraded",
        "connected": s["last_status"] != "UNKNOWN",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "endpoint": s["last_endpoint"],
        "temp": s["last_temp"],
        "state": s["last_status"]
    })

@app.route("/graph.png")
def graph_png():
    pts = resample_10min_last(list(monitor.history), max_points=20)
    data = chart_png_from_points(pts)
    return Response(data, mimetype="image/png")

@app.route("/api/status")
def api_status():
    s = monitor.snapshot()
    return jsonify({
        "success": True,
        "data": s,
        "email": {
            "from": f"{MAIL_FROM_NAME} <{MAIL_FROM_ADDRESS}>",
            "to": EMAIL_TO,
            "host": MAIL_HOST,
            "port": MAIL_PORT,
            "tls": MAIL_ENCRYPTION,
            "last_smtp_error": _last_smtp_error,
        }
    })

@app.route("/api/test_email")
def api_test_email():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s = monitor.snapshot()
    subject = "[iDRAC Test] Email Connectivity"
    html, text = build_email_body("Test", s["last_temp"], now, s["last_endpoint"])
    html, text = _append_past_hour_stats(html, text, monitor.history)
    chart = monitor._build_chart_attachment()
    ok = send_email(subject, html, text, attachments=chart)
    return jsonify({"success": ok, "error": None if ok else (_last_smtp_error or "unknown error")})

if __name__ == "__main__":
    try:
        from waitress import serve
        print("=" * 60)
        print("iDRAC Temperature Monitor")
        print("=" * 60)
        print(f"Server: http://0.0.0.0:5000")
        print(f"Dashboard: http://127.0.0.1:5000")
        print(f"\nTemperature Thresholds:")
        print(f"  Normal: ≤{NORMAL_TEMP_MAX}°C")
        print(f"  Warning: {WARNING_TEMP}-{CRITICAL_TEMP-1}°C")
        print(f"  Critical: ≥{CRITICAL_TEMP}°C")
        print(f"\nEmail Recipients: {len(EMAIL_TO)}")
        print(f"Hourly Reports: Every hour (00-23)")
        print(f"Alert Reports: Every {PERSIST_EMAIL_EVERY_SEC}s while in Warning/Critical")
        print("=" * 60)
        serve(app, host="0.0.0.0", port=5000)
    except Exception as e:
        print(f"Failed to start with waitress: {e}")
        print("Starting with Flask development server...")
        app.run(host="0.0.0.0", port=5000, debug=False)
