import requests
import logging
import time
import json
import sqlite3
import os
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from requests.auth import HTTPBasicAuth

# Disable SSL warnings for iDRAC self-signed certs
requests.packages.urllib3.disable_warnings()

app = Flask(__name__)

# Configuration
IDRAC_CONFIG = {
    'host': os.getenv('IDRAC_HOST', '10.129.16.81'),
    'username': os.getenv('IDRAC_USER', 'root'),
    'password': os.getenv('IDRAC_PASS', 'calvin'),
    'verify_ssl': False
}

# Setup logging
logging.basicConfig(
    filename='idrac_monitor.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class iDRACClient:
    """Client for iDRAC Redfish API"""
    
    def __init__(self, host, username, password):
        self.base_url = f"https://{host}/redfish/v1"
        self.auth = (username, password)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.verify = False
        self.cache = {}
        self.cache_timeout = 30  # seconds
        
    def _fetch(self, endpoint):
        """Fetch with simple caching"""
        now = time.time()
        if endpoint in self.cache:
            data, timestamp = self.cache[endpoint]
            if now - timestamp < self.cache_timeout:
                return data
                
        try:
            url = f"{self.base_url}/{endpoint}"
            response = self.session.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                self.cache[endpoint] = (data, now)
                return data
        except Exception as e:
            logging.error(f"Error fetching {endpoint}: {e}")
        return None
    
    def get_system_info(self):
        """Get basic system information"""
        data = self._fetch("Systems/System.Embedded.1")
        if data:
            return {
                'model': data.get('Model', 'N/A'),
                'manufacturer': data.get('Manufacturer', 'N/A'),
                'serial': data.get('SerialNumber', 'N/A'),
                'bios_version': data.get('BiosVersion', 'N/A'),
                'power_state': data.get('PowerState', 'N/A'),
                'memory_gb': data.get('MemorySummary', {}).get('TotalSystemMemoryGiB', 0),
                'cpu_count': data.get('ProcessorSummary', {}).get('Count', 0),
                'cpu_model': data.get('ProcessorSummary', {}).get('Model', 'N/A')
            }
        return None
    
    def get_power_supplies(self):
        data = self._fetch("Chassis/System.Embedded.1/Power")
        psus = []
        if data and 'PowerSupplies' in data:
            for psu in data['PowerSupplies']:
                psus.append({
                    'name': psu.get('Name', 'N/A'),
                    'status': psu.get('Status', {}).get('Health', 'Unknown'),
                    'capacity': psu.get('PowerCapacityWatts', 'N/A'),
                    'input_voltage': psu.get('LineInputVoltage', 'N/A')
                })
        return psus
        
    def get_temperatures(self):
        """Get temperature sensors"""
        data = self._fetch("Chassis/System.Embedded.1/Thermal")
        temps = []
        if data and 'Temperatures' in data:
            for sensor in data['Temperatures']:
                temps.append({
                    'name': sensor.get('Name', 'N/A'),
                    'reading': sensor.get('ReadingCelsius', 'N/A'),
                    'status': sensor.get('Status', {}).get('Health', 'Unknown'),
                    'threshold': sensor.get('UpperThresholdNonCritical', 'N/A')
                })
        return temps
    
    def get_fans(self):
        """Get fan information"""
        data = self._fetch("Chassis/System.Embedded.1/Thermal")
        fans = []
        if data and 'Fans' in data:
            for fan in data['Fans']:
                fans.append({
                    'name': fan.get('Name', 'N/A'),
                    'speed': fan.get('Reading', 'N/A'),
                    'status': fan.get('Status', {}).get('Health', 'Unknown')
                })
        return fans

# Initialize iDRAC client
idrac = iDRACClient(
    IDRAC_CONFIG['host'],
    IDRAC_CONFIG['username'],
    IDRAC_CONFIG['password']
)

@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html')

@app.route('/api/system')
def api_system():
    """API endpoint for system info"""
    data = idrac.get_system_info()
    if data:
        return jsonify({'success': True, 'data': data})
    return jsonify({'success': False, 'error': 'Could not fetch system data'})

@app.route('/api/power')
def api_power():
    """API endpoint for power supplies"""
    data = idrac.get_power_supplies()
    return jsonify({'success': True, 'data': data})

@app.route('/api/temperatures')
def api_temperatures():
    """API endpoint for temperatures"""
    data = idrac.get_temperatures()
    return jsonify({'success': True, 'data': data})

@app.route('/api/fans')
def api_fans():
    """API endpoint for fans"""
    data = idrac.get_fans()
    return jsonify({'success': True, 'data': data})

@app.route('/api/all')
def api_all():
    """API endpoint for all data"""
    return jsonify({
        'success': True,
        'data': {
            'system': idrac.get_system_info(),
            'power': idrac.get_power_supplies(),
            'temperatures': idrac.get_temperatures(),
            'fans': idrac.get_fans()
        }
    })

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'idrac_connected': idrac.get_system_info() is not None
    })

if __name__ == '__main__':
    # Use waitress for production
    from waitress import serve
    print("Starting iDRAC Monitor on http://127.0.0.1:5000")
    serve(app, host='0.0.0.0', port=5000)