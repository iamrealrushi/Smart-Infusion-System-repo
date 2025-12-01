#!/usr/bin/env python3
"""
DoseMate Syringe Pump - Flask Backend (Raspberry Pi 5)
- Serves the UI (dosemate_pump1.html or dosemate_pump1 (2).html)
- WiFi scan/connect via nmcli
- TB6600 + NEMA17 control on BCM: STEP=23, DIR=24, EN=25
- Start / Pause / Resume / Cancel / Reset (retract)
- Records save/load
- Email export via Gmail SMTP (enter your 16-digit app password)
"""

from flask import Flask, jsonify, request
import subprocess, json, os, threading, time, csv, tempfile, smtplib
from datetime import datetime
from email.message import EmailMessage

app = Flask(__name__)

# --- Built-in CORS (replaces Flask-Cors) ---
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# --- Email settings (put your app password in the string below, keep quotes) ---
SENDER_EMAIL    = "dosematedevice@gmail.com"
SENDER_PASSWORD = "sf**********nl"   # <<< REPLACE ONLY inside the quotes
SMTP_HOST       = "smtp.gmail.com"
SMTP_PORT       = 587
USE_TLS         = True

# --- GPIO / TB6600 ---
try:
    import RPi.GPIO as GPIO
    HARDWARE_AVAILABLE = True
except ImportError:
    HARDWARE_AVAILABLE = False

STEP_PIN = 23  # BCM
DIR_PIN  = 24
EN_PIN   = 25

# ---------- Steps per mL (with calibration) ----------
# Original measured values from your design:
#   10 mL syringe: 303 steps / mL
#   15 mL syringe: 265 steps / mL
#   20 mL syringe: 168 steps / mL
#
# From your test: commanded 5 mL → only ~2 mL infused.
# That implies the real steps/mL is ~2.5x larger than the original numbers.
# We add a calibration factor so this can be tuned if you re-measure later.
BASE_SYRINGE_STEPS_PER_ML = {10: 303, 15: 265, 20: 168}

# Overall calibration factor. Adjust this if you re-test.
# From 5 mL → 2 mL result: factor ≈ 5/2 = 2.5
CALIBRATION_FACTOR = 2.5

def get_steps_per_ml(syringe_size: int) -> float:
    base = BASE_SYRINGE_STEPS_PER_ML.get(int(syringe_size), BASE_SYRINGE_STEPS_PER_ML[10])
    return base * CALIBRATION_FACTOR

infusion_state = {
    "running": False,
    "paused": False,
    "cancelled": False,
    "total_steps": 0,
    "steps_done": 0,
    "steps_per_sec": 0.0,
    "syringe_size": 10,
    "volume_ml": 0.0,
    "flow_rate_ml_hr": 0.0,
    "thread": None,
    "lock": threading.Lock(),
}
delivered_steps_history = 0  # for reset/retract

def _gpio_setup():
    if not HARDWARE_AVAILABLE:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(STEP_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(DIR_PIN,  GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(EN_PIN,   GPIO.OUT, initial=GPIO.HIGH)  # disabled
    # Enable driver (invert if your TB6600 wants HIGH to enable)
    GPIO.output(EN_PIN, GPIO.LOW)

def _gpio_disable():
    if not HARDWARE_AVAILABLE:
        return
    try:
        GPIO.output(EN_PIN, GPIO.HIGH)
    except Exception:
        pass

def _gpio_cleanup():
    if not HARDWARE_AVAILABLE:
        return
    _gpio_disable()
    GPIO.cleanup()

def _pulse_steps(step_count, direction, steps_per_sec, pause_flag_getter, cancel_flag_getter):
    """
    Send 'step_count' pulses at 'steps_per_sec' in 'direction'.
    Works both for steps_per_sec < 1 and > 1.
    """
    if steps_per_sec <= 0:
        steps_per_sec = 0.0001  # avoid divide-by-zero, extremely slow

    # Simulate if not on Pi
    if not HARDWARE_AVAILABLE:
        delay = 1.0 / (steps_per_sec * 2.0)
        for _ in range(step_count):
            while pause_flag_getter():
                time.sleep(0.05)
            if cancel_flag_getter():
                break
            time.sleep(delay * 2)
        return

    GPIO.output(DIR_PIN, GPIO.HIGH if direction == "forward" else GPIO.LOW)
    delay = 1.0 / (steps_per_sec * 2.0)  # HIGH then LOW → full step period

    for _ in range(step_count):
        while pause_flag_getter():
            time.sleep(0.05)
        if cancel_flag_getter():
            break
        GPIO.output(STEP_PIN, GPIO.HIGH)
        time.sleep(delay)
        GPIO.output(STEP_PIN, GPIO.LOW)
        time.sleep(delay)

def infusion_worker(flow_rate_ml_hr, volume_ml, syringe_size):
    """
    Main infusion loop: computes total steps & step rate from
    volume and ml/hr, then pulses the driver.
    """
    global delivered_steps_history

    # Use calibrated steps/mL
    steps_per_ml = get_steps_per_ml(syringe_size)
    total_steps  = max(int(round(volume_ml * steps_per_ml)), 0)

    # ml/hr → ml/s
    ml_per_sec    = max(flow_rate_ml_hr, 0.0) / 3600.0
    # True steps/sec (no artificial clamp to 1.0 so ETA is correct)
    steps_per_sec = ml_per_sec * steps_per_ml
    if steps_per_sec <= 0:
        steps_per_sec = 0.0001

    with infusion_state["lock"]:
        infusion_state.update({
            "running": True,
            "paused": False,
            "cancelled": False,
            "total_steps": total_steps,
            "steps_done": 0,
            "steps_per_sec": steps_per_sec,
            "syringe_size": syringe_size,
            "volume_ml": volume_ml,
            "flow_rate_ml_hr": flow_rate_ml_hr,
        })

    steps_sent = 0

    for _ in range(total_steps):
        with infusion_state["lock"]:
            if infusion_state["cancelled"]:
                break

        _pulse_steps(
            1,
            "forward",
            steps_per_sec,
            pause_flag_getter=lambda: infusion_state["paused"],
            cancel_flag_getter=lambda: infusion_state["cancelled"],
        )

        steps_sent += 1
        with infusion_state["lock"]:
            infusion_state["steps_done"] = steps_sent

        with infusion_state["lock"]:
            if infusion_state["cancelled"]:
                break

    # Finish
    with infusion_state["lock"]:
        infusion_state["running"] = False
        infusion_state["paused"]  = False

    delivered_steps_history = steps_sent
    if steps_sent == 0:
        _gpio_disable()

def retract_worker():
    """
    Retracts by the number of steps previously moved (delivered_steps_history).
    Uses a comfortable speed (independent of infusion speed).
    """
    global delivered_steps_history
    steps_to_retract = delivered_steps_history

    if steps_to_retract <= 0:
        _gpio_disable()
        return

    # Use a reasonable retract speed (e.g. 400 steps/sec)
    retract_sps = 400.0

    _pulse_steps(
        steps_to_retract,
        "reverse",
        retract_sps,
        pause_flag_getter=lambda: False,
        cancel_flag_getter=lambda: False,
    )
    delivered_steps_history = 0
    _gpio_disable()

# ---- WiFi helpers (nmcli) ----
def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)

def scan_wifi_networks():
    networks = []
    code, out, err = _run(['nmcli','-t','-f','SSID,SECURITY,SIGNAL','dev','wifi'], timeout=10)
    if code == 0 and out:
        for line in out.splitlines():
            parts = line.split(':')
            if len(parts) >= 3:
                ssid = ':'.join(parts[:-2]) or 'Hidden Network'
                security = parts[-2] or ''
                strength = parts[-1] or '0'
                if not security or security == '--':
                    security = 'Open'
                try:
                    sig = int(strength)
                except Exception:
                    sig = 0
                label = 'Excellent' if sig>=80 else 'Good' if sig>=60 else 'Fair' if sig>=40 else 'Weak'
                networks.append({'ssid': ssid, 'signal': label, 'security': security, 'strength': f'{sig}%'})
    # dedupe + sort
    seen, res = set(), []
    for n in networks:
        if n['ssid'] in seen:
            continue
        seen.add(n['ssid'])
        res.append(n)
    res.sort(key=lambda n: int(n['strength'].rstrip('%') or 0), reverse=True)
    return res

def connect_wifi(ssid, password="", security=""):
    if not ssid:
        return False, "SSID required"
    # Open network?
    if (security or '').lower() in ['open','--','none','']:
        cmd = ['nmcli','dev','wifi','connect', ssid]
    else:
        cmd = ['nmcli','dev','wifi','connect', ssid]
        if password:
            cmd += ['password', password]
    code, out, err = _run(cmd, timeout=30)
    if code == 0:
        return True, "Connected"

    # Alternate profile flow
    if password and (security or '').lower() not in ['open','--','none','']:
        _run(['nmcli','con','delete', ssid], timeout=5)
        c1, o1, e1 = _run(['nmcli','con','add','type','wifi','con-name',ssid,'ifname','wlan0','ssid',ssid], timeout=10)
        if c1 == 0:
            c2, o2, e2 = _run(['nmcli','con','modify', ssid, 'wifi-sec.key-mgmt','wpa-psk','wifi-sec.psk',password], timeout=10)
            if c2 == 0:
                c3, o3, e3 = _run(['nmcli','con','up', ssid], timeout=20)
                if c3 == 0:
                    return True, "Connected"
                else:
                    return False, (e3 or o3 or "UP failed")
            else:
                return False, (e2 or o2 or "PSK set failed")
        else:
            return False, (e1 or o1 or "Profile add failed")
    return False, (err or out or "Failed to connect")

# ---- Export & Email ----
def export_records_csv():
    records = []
    if os.path.exists('infusion_records.json'):
        try:
            with open('infusion_records.json','r') as f:
                records = json.load(f)
        except Exception:
            records = []
    path = os.path.join(
        tempfile.gettempdir(),
        f'dosemate_records_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    )
    with open(path, 'w', newline='', encoding='utf-8') as csvfile:
        if records:
            fn = list(records[0].keys())
            w = csv.DictWriter(csvfile, fieldnames=fn)
            w.writeheader()
            w.writerows(records)
        else:
            csv.writer(csvfile).writerow(
                ['timestamp','patient_name','medicine','volume_ml','flow_rate_ml_hr','syringe_size']
            )
    return path

def export_records_json():
    records = []
    if os.path.exists('infusion_records.json'):
        try:
            with open('infusion_records.json','r') as f:
                records = json.load(f)
        except Exception:
            records = []
    path = os.path.join(
        tempfile.gettempdir(),
        f'dosemate_records_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    )
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(records, f, indent=2)
    return path

def send_email_fixed(to_addr, attachments=None):
    if not to_addr:
        return False, "Recipient missing"
    msg = EmailMessage()
    msg["From"] = SENDER_EMAIL
    msg["To"]   = to_addr
    msg["Subject"] = "DoseMate — Infusion Records"
    msg.set_content(
        "Hello,\n\n"
        "Please find the DoseMate infusion record export attached.\n"
        "This email was sent automatically by the DoseMate device.\n\n"
        "Thank you.\n"
        "-- DoseMate"
    )
    for p in (attachments or []):
        name = os.path.basename(p)
        if name.lower().endswith(".csv"):
            mt, st = "text", "csv"
        elif name.lower().endswith(".json"):
            mt, st = "application", "json"
        else:
            mt, st = "application","octet-stream"
        try:
            with open(p,'rb') as f:
                msg.add_attachment(f.read(), maintype=mt, subtype=st, filename=name)
        except Exception:
            pass
    try:
        if USE_TLS:
            s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
            s.starttls()
        else:
            s = smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=20)
        s.login(SENDER_EMAIL, SENDER_PASSWORD)
        s.send_message(msg)
        s.quit()
        return True, "sent"
    except Exception as e:
        return False, str(e)

# ---- Routes ----
@app.route("/")
def index():
    # Serve whichever filename you have locally
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("dosemate_pump1.html", "dosemate_pump1 (2).html"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            return open(p, "r", encoding="utf-8").read()
    return "<h2>⚠️ Put dosemate_pump1.html next to app.py</h2>"

@app.route("/api/system_time")
def api_system_time():
    return jsonify({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

@app.route("/api/scan_wifi")
def api_scan_wifi():
    return jsonify(scan_wifi_networks())

@app.route("/api/connect_wifi", methods=["POST"])
def api_connect_wifi():
    d = request.json or {}
    ok, msg = connect_wifi(d.get("ssid",""), d.get("password",""), d.get("security",""))
    return jsonify({"status":"connected" if ok else "error", "message": msg}), (200 if ok else 400)

@app.route("/api/start_infusion", methods=["POST"])
def api_start_infusion():
    d = request.json or {}
    try:
        flow = float(d.get("flowRate", 0))
        vol  = float(d.get("volume", 0))
        syr  = int(d.get("syringeSize", 10))
    except Exception:
        return jsonify({"status":"error","message":"Invalid payload"}), 400
    if flow <= 0 or vol <= 0:
        return jsonify({"status":"error","message":"Invalid flow/volume"}), 400

    if HARDWARE_AVAILABLE:
        _gpio_setup()
    with infusion_state["lock"]:
        if infusion_state["running"]:
            return jsonify({"status":"error","message":"Already running"}), 400
        t = threading.Thread(target=infusion_worker, args=(flow, vol, syr), daemon=True)
        infusion_state["thread"] = t
        t.start()
    return jsonify({"status":"started"})

@app.route("/api/pause_infusion", methods=["POST"])
def api_pause_infusion():
    with infusion_state["lock"]:
        if infusion_state["running"]:
            infusion_state["paused"] = True
    return jsonify({"status":"paused"})

@app.route("/api/resume_infusion", methods=["POST"])
def api_resume_infusion():
    with infusion_state["lock"]:
        if infusion_state["running"]:
            infusion_state["paused"] = False
    return jsonify({"status":"resumed"})

@app.route("/api/cancel_infusion", methods=["POST"])
def api_cancel_infusion():
    with infusion_state["lock"]:
        infusion_state["cancelled"] = True
        infusion_state["running"] = False
        infusion_state["paused"]  = False
    _gpio_disable()
    return jsonify({"status":"cancelled"})

@app.route("/api/infusion_status")
def api_infusion_status():
    with infusion_state["lock"]:
        running = infusion_state["running"]
        paused = infusion_state["paused"]
        cancelled = infusion_state["cancelled"]
        total_steps = infusion_state["total_steps"]
        steps_done  = infusion_state["steps_done"]
        sps = infusion_state["steps_per_sec"]
    progress = (steps_done/total_steps*100.0) if total_steps>0 else 0.0
    rem = max(total_steps - steps_done, 0)
    eta = (rem/sps) if sps>0 else 0
    return jsonify({
        "running": running,
        "paused": paused,
        "cancelled": cancelled,
        "progress_pct": progress,
        "steps_done": steps_done,
        "total_steps": total_steps,
        "eta_h": int(eta//3600),
        "eta_m": int((eta%3600)//60),
        "eta_s": int(eta%60)
    })

@app.route("/api/reset_plunger", methods=["POST"])
def api_reset_plunger():
    with infusion_state["lock"]:
        if infusion_state["running"]:
            return jsonify({"status":"error","message":"Infusion running"}), 400
    if HARDWARE_AVAILABLE:
        _gpio_setup()
    threading.Thread(target=retract_worker, daemon=True).start()
    return jsonify({"status":"retracting"})

@app.route("/api/save_record", methods=["POST"])
def api_save_record():
    try:
        rec = request.json
        data = []
        if os.path.exists('infusion_records.json'):
            try:
                data = json.load(open('infusion_records.json','r'))
            except Exception:
                data = []
        data.append(rec)
        json.dump(data, open('infusion_records.json','w'), indent=2)
        return jsonify({"status":"success"})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 400

@app.route("/api/load_records")
def api_load_records():
    if os.path.exists('infusion_records.json'):
        try:
            data = json.load(open('infusion_records.json','r'))
        except Exception:
            data = []
    else:
        data = []
    return jsonify(data)

@app.route("/api/email_records", methods=["POST"])
def api_email_records():
    d = request.json or {}
    to = (d.get('to') or '').strip()
    kind = (d.get('kind') or 'csv').lower()
    if not to:
        return jsonify({'status':'error','message':'Recipient required'}), 400
    try:
        attach = export_records_csv() if kind=='csv' else export_records_json()
    except Exception as e:
        return jsonify({'status':'error','message':f'export failed: {e}'}), 400
    ok, msg = send_email_fixed(to, [attach])
    try:
        os.remove(attach)
    except Exception:
        pass
    return (jsonify({'status':'ok'}) if ok else
            (jsonify({'status':'error','message':msg}), 400))

if __name__ == "__main__":
    print("""
DoseMate Syringe Infusion Pump Server (Pi 5)
UI: http://0.0.0.0:5000
Run with:  sudo python3 app.py
Kiosk: chromium-browser --kiosk --app=http://localhost:5000 --window-size=800,480 --force-device-scale-factor=1
""")
    app.run(host="0.0.0.0", port=5000, debug=False)
