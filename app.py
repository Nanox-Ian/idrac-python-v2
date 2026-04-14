# app.py
import os, time, csv, threading, logging, smtplib, requests
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from email.message import EmailMessage
from flask import Flask, jsonify, render_template, request, Response
from requests.auth import HTTPBasicAuth

# ================= ENV =================
def load_env():
    if os.path.exists(".env"):
        for l in open(".env"):
            if "=" in l and not l.startswith("#"):
                k, v = l.strip().split("=", 1)
                os.environ[k] = v.strip().strip('"')

load_env()

IDRAC_SERVERS = [s for s in os.getenv("IDRAC_SERVERS", "").split(",") if s]
IDRAC_USER = os.getenv("IDRAC_USER")
IDRAC_PASS = os.getenv("IDRAC_PASS")

WARNING_TEMP = float(os.getenv("WARNING_TEMP", 25))
CRITICAL_TEMP = float(os.getenv("CRITICAL_TEMP", 30))

SAMPLE_INTERVAL_SEC = int(os.getenv("SAMPLE_INTERVAL_SEC", 10))
SPIKE_CONFIRM_SEC = 300
ALERT_COOLDOWN_SEC = 1800

MAIL_FROM_ADDRESS = os.getenv("MAIL_FROM_ADDRESS")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME")
EMAIL_TO = os.getenv("EMAIL_TO","").split(",")
MAIL_HOST = os.getenv("MAIL_HOST")
MAIL_PORT = int(os.getenv("MAIL_PORT",25))

# ================= LOGS =================
os.makedirs("storage", exist_ok=True)
TEMP_LOG = "storage/temperature.log"
CSV_LOG = "storage/idrac_log.csv"

# CSV header
if not os.path.exists(CSV_LOG):
    with open(CSV_LOG,"w",newline="") as f:
        csv.writer(f).writerow(["timestamp","host","temp","status"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("storage/app.log"), logging.StreamHandler()]
)
log = logging.getLogger("idrac")

# ================= FLASK =================
app = Flask(__name__)
requests.packages.urllib3.disable_warnings()

# ================= EMAIL =================
def send_email(subject, html):
    msg = EmailMessage()
    msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM_ADDRESS}>"
    msg["To"] = ", ".join(EMAIL_TO)
    msg["Subject"] = subject
    msg.set_content(html, subtype="html")
    with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=15) as s:
        s.send_message(msg)

def html_table(state):
    rows=""
    for h,s in state.items():
        color = "#10b981"
        if s["status"]=="WARNING": color="#f59e0b"
        elif s["status"]=="CRITICAL": color="#ef4444"
        rows+=f"<tr><td>{h}</td><td>{s['temp']}</td><td style='color:{color}'>{s['status']}</td><td>{s['timestamp']}</td></tr>"
    return f"<table border='1' cellpadding='8'><tr><th>Server</th><th>Temp</th><th>Status</th><th>Time</th></tr>{rows}</table>"

# ================= REDFISH =================
class RedfishClient:
    def __init__(self, h):
        self.base=f"https://{h}/redfish/v1"
        self.s=requests.Session()
        self.s.verify=False
        self.s.auth=HTTPBasicAuth(IDRAC_USER,IDRAC_PASS)

    def read_temp(self)->Optional[float]:
        try:
            r=self.s.get(f"{self.base}/Chassis/System.Embedded.1/Thermal",timeout=8)
            for t in r.json().get("Temperatures",[]):
                if "inlet" in (t.get("Name","").lower()):
                    return float(t["ReadingCelsius"])
        except: pass
        return None

# ================= MONITOR =================
class Monitor:
    def __init__(self):
        self.clients={h:RedfishClient(h) for h in IDRAC_SERVERS}
        self.state={}
        self.last_hour=None
        self.last_alert=0
        threading.Thread(target=self.run,daemon=True).start()

    def classify(self,t):
        if t is None: return "UNKNOWN"
        if t>=CRITICAL_TEMP: return "CRITICAL"
        if t>=WARNING_TEMP: return "WARNING"
        return "NORMAL"

    def run(self):
        while True:
            now=datetime.now()
            alerting=False
            for h,c in self.clients.items():
                t=c.read_temp()
                st=self.classify(t)
                prev=self.state.get(h,{})
                spike=prev.get("spike_start")

                if st in ("WARNING","CRITICAL"):
                    spike = spike or time.time()
                    if time.time()-spike>=SPIKE_CONFIRM_SEC:
                        alerting=True
                else:
                    spike=None

                self.state[h]={
                    "temp":t,
                    "status":st,
                    "timestamp":now.strftime("%Y-%m-%d %H:%M:%S"),
                    "spike_start":spike
                }

                with open(TEMP_LOG,"a") as f:
                    f.write(f"{self.state[h]['timestamp']} | {h} | {t} | {st}\n")
                with open(CSV_LOG,"a",newline="") as f:
                    csv.writer(f).writerow([self.state[h]['timestamp'],h,t,st])

            if now.minute==0 and self.last_hour!=now.hour:
                send_email(f"[iDRAC Hourly Summary] {now:%H}:00", html_table(self.state))
                self.last_hour=now.hour

            if alerting and time.time()-self.last_alert>=ALERT_COOLDOWN_SEC:
                send_email("[iDRAC ALERT] Sustained High Temp", html_table(self.state))
                self.last_alert=time.time()

            time.sleep(SAMPLE_INTERVAL_SEC)

monitor=Monitor()

# ================= API =================
@app.route("/")
def index(): return render_template("dashboard.html")

@app.route("/api/state")
def api_state(): return jsonify(monitor.state)

@app.route("/api/logs")
def api_logs():
    host=request.args.get("host")
    lines=[]
    with open(TEMP_LOG) as f:
        for l in f.readlines()[-500:]:
            if not host or f"| {host} |" in l:
                lines.append(l)
    return jsonify(lines)

@app.route("/api/history")
def api_history():
    host=request.args.get("host")
    cutoff=datetime.now()-timedelta(hours=1)
    pts=[]
    with open(CSV_LOG) as f:
        next(f)
        for ts,h,t,_ in csv.reader(f):
            if h==host:
                dt=datetime.fromisoformat(ts)
                if dt>=cutoff:
                    pts.append({"t":ts,"v":t})
    return jsonify(pts)

@app.route("/api/export")
def api_export():
    host=request.args.get("host")
    def gen():
        yield "timestamp,host,temp,status\n"
        with open(CSV_LOG) as f:
            for l in f:
                if not host or f",{host}," in l:
                    yield l
    return Response(gen(),
        mimetype="text/csv",
        headers={"Content-Disposition":f"attachment; filename={host or 'all'}_logs.csv"}
    )

app.run("0.0.0.0",5000)
