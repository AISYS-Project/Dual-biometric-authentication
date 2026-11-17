import time, threading, sqlite3, socket, webbrowser, json, urllib.request, base64, platform, subprocess
from datetime import datetime
import cv2
import numpy as np
from flask import Flask, Response, request, jsonify

CLOUD_URL = ""  # Optional: paste cloud API URL later
CAP_W, CAP_H = 640, 480
IS_PI = platform.system() != "Windows"
app = Flask(__name__)
# --- FINGERPRINT AUTHENTICATION ---
def sql(q, args=(), fetch=False):
    conn = sqlite3.connect("hostel.db")
    cur = conn.execute(q, args)
    rows = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return rows
import os

FINGERPRINT_MODE = os.getenv("FINGERPRINT_MODE", "simulation")

class FingerprintService:

    def __init__(self):
        self.sensor = None
        self.pending_scan = None
        self.simulated_templates = {}

        if FINGERPRINT_MODE == "simulation":
            mappings = sql(
                "SELECT scan_code, fingerprint_id FROM fingerprint_simulation",
                fetch=True
            )

            for scan_code, fingerprint_id in mappings:
                self.simulated_templates[scan_code] = fingerprint_id

    def connect(self):
        if FINGERPRINT_MODE == "simulation":
            print("Fingerprint simulation ready.")
            return True

        # Hardware connection will be added after
        # verifying the ZA620-M5 serial protocol.
        raise NotImplementedError(
            "AS608 hardware driver is not configured yet."
        )

    def identify(self, simulated_scan=None):
        if FINGERPRINT_MODE == "simulation":
            if simulated_scan in self.simulated_templates:
                return {
                    "status": "matched",
                    "fingerprint_id": self.simulated_templates[simulated_scan]
                }

            self.pending_scan = simulated_scan

            return {
                "status": "not_found",
                "message": "Unknown fingerprint. Admin approval required."
            }

        raise NotImplementedError("Hardware identification not configured.")

    def enroll(self, simulated_scan):
        if FINGERPRINT_MODE == "simulation":
            if simulated_scan is None:
                return None

            rows = sql(
                "SELECT COALESCE(MAX(fingerprint_id), 0) + 1 FROM students",
                fetch=True
            )

            fingerprint_id = rows[0][0]
            return fingerprint_id

        raise NotImplementedError("Hardware enrollment not configured.")
sql("""
    CREATE TABLE IF NOT EXISTS fingerprint_simulation (
        scan_code TEXT PRIMARY KEY,
        fingerprint_id INTEGER UNIQUE NOT NULL
    )
""")
fingerprint_service = FingerprintService()
fingerprint_service.connect()

# --- 1. DATABASE (LOW-RES PHOTO + EMBEDDINGS + ADMINS + LOGS) ---
def sql(q, args=(), fetch=False):
    conn = sqlite3.connect("hostel.db")
    cur = conn.execute(q, args)
    rows = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return rows

sql("CREATE TABLE IF NOT EXISTS students (id INTEGER PRIMARY KEY, roll_no TEXT UNIQUE, name TEXT, room_no TEXT, face_embedding BLOB, photo BLOB, status TEXT DEFAULT 'OUT', last_seen REAL DEFAULT 0)")
try: sql("ALTER TABLE students ADD COLUMN photo BLOB")
except Exception: pass
# Add fingerprint support without changing existing student records
try:
    sql("ALTER TABLE students ADD COLUMN fingerprint_id INTEGER")
except sqlite3.OperationalError:
    pass

# Ensure each fingerprint ID belongs to only one student
sql("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_students_fingerprint
    ON students(fingerprint_id)
    WHERE fingerprint_id IS NOT NULL
""")
sql("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, roll_no TEXT, name TEXT, room_no TEXT, action TEXT, timestamp TEXT)")
sql("CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY, username TEXT UNIQUE, pin TEXT)")
sql("INSERT OR IGNORE INTO admins (username, pin) VALUES (?, ?)", ("aisys", "1234"))  # Admin 1

def check_admin(d):
    u = (d.get("user") or "").strip()
    p = (d.get("pin") or "").strip()
    return bool(sql("SELECT id FROM admins WHERE username=? AND pin=?", (u, p), fetch=True))

def sync_cloud(payload):
    if not CLOUD_URL: return
    try:
        req = urllib.request.Request(CLOUD_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2)
    except Exception: pass

cache = []
def reload_cache():
    global cache
    rows = sql("SELECT id, roll_no, name, room_no, face_embedding FROM students", fetch=True)
    cache = [(r[0], r[1], r[2], r[3], np.frombuffer(r[4], np.float32).reshape(1, 128)) for r in rows if r[4]]
reload_cache()
# --- FINGERPRINT EMPLOYEE LOOKUP ---

def find_employee_by_fingerprint(fingerprint_id):
    
    rows = sql(
        """
        SELECT id, roll_no, name, room_no, status, last_seen
        FROM students
        WHERE fingerprint_id = ?
        """,
        (fingerprint_id,),
        fetch=True
    )

    return rows[0] if rows else None
# --- FINGERPRINT ACCESS CONTROL ---

def process_fingerprint_access(fingerprint_id):

    employee = find_employee_by_fingerprint(fingerprint_id)

    if employee is None:
        return {
            "c": "BAD",
            "t": "Fingerprint Not Registered",
            "s": "Please contact the administrator."
        }

    sid, roll, name, room, status, last_seen = employee

    # Prevent repeated scans within 15 seconds
    if time.time() - last_seen < 15:
        return {
            "c": "OK",
            "t": f"RECOGNIZED - VERIFIED ({status})",
            "s": f"{name} - Already scanned"
        }

    action = "ENTRY" if status == "OUT" else "EXIT"
    new_status = "IN" if action == "ENTRY" else "OUT"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sql(
        "UPDATE students SET status=?, last_seen=? WHERE id=?",
        (new_status, time.time(), sid)
    )

    sql(
        """INSERT INTO logs
        (roll_no, name, room_no, action, timestamp)
        VALUES (?, ?, ?, ?, ?)""",
        (roll, name, room, action, timestamp)
    )

    threading.Thread(
        target=sync_cloud,
        args=({
            "type": "log",
            "roll": roll,
            "action": action,
            "time": timestamp
        },),
        daemon=True
    ).start()

    speak(f"{name}. Fingerprint verified. {action.lower()} recorded.")

    return {
        "c": "OK",
        "t": f"FINGERPRINT VERIFIED ({action})",
        "s": f"Welcome, {name}! (Roll: {roll})"
    }
# --- 2. CAMERA, MODELS & VOICE HELPER ---
try:
    from picamera2 import Picamera2
    cam = Picamera2()
    cam.configure(cam.create_preview_configuration(main={"size": (CAP_W, CAP_H), "format": "RGB888"}))
    cam.start()
    get_frame = lambda: (True, cam.capture_array())
except ImportError:
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAP_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAP_H)
    get_frame = cap.read

detector = cv2.FaceDetectorYN.create("models/face_detection_yunet_2023mar.onnx", "", (320, 320), 0.75, 0.3, 5000)
recognizer = cv2.FaceRecognizerSF.create("models/face_recognition_sface_2021dec.onnx", "")

lock, mlock = threading.Lock(), threading.Lock()
jpg_bytes, last_frame, last_face, last_time = None, None, None, 0
state, t_start, t_until = "IDLE", 0, 0
voice_id, voice_txt = 0, ""
msg = {"c": "IDLE", "t": "Welcome", "s": "Please look at the camera"}

def greet():
    h = datetime.now().hour
    return "Good Morning" if h < 12 else ("Good Afternoon" if h < 17 else "Good Evening")

def speak(text):
    global voice_id, voice_txt
    voice_id += 1
    voice_txt = text
    if IS_PI:
        try: subprocess.Popen(["espeak-ng", "-s", "145", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception: pass

def scale_face(face, w, h):
    f = face.copy()
    f[0:14:2] *= (w / 320.0)
    f[1:14:2] *= (h / 320.0)
    return f

def net_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return ""

# --- 3. ALWAYS-ON CAMERA & RECOGNITION LOOP ---
def camera_loop():
    global jpg_bytes, last_frame, last_face, last_time, state, t_start, t_until, msg
    while True:
        ok, frame = get_frame()
        if not ok:
            time.sleep(0.05)
            continue

        h, w = frame.shape[:2]
        small = cv2.resize(frame, (320, 320))
        with mlock:
            _, faces = detector.detect(small)

        best = max(faces, key=lambda f: f[2] * f[3]) if faces is not None and len(faces) else None
        near = best is not None and best[2] >= 55 and best[14] >= 0.80

        if near:
            hi_face = scale_face(best, w, h)
            with lock:
                last_frame, last_face, last_time = frame.copy(), hi_face.copy(), time.time()

            if state == "IDLE":
                state, t_start = "WAIT", time.time()
                with lock: msg = {"c": "WAIT", "t": "Verifying...", "s": f"{greet()}! Hold steady..."}

            elif state == "WAIT" and time.time() - t_start >= 0.8:
                with mlock:
                    emb = recognizer.feature(recognizer.alignCrop(frame, hi_face))
                match, best_score = max(
                    ((s, recognizer.match(emb, s[4], cv2.FaceRecognizerSF_FR_COSINE)) for s in cache),
                    key=lambda x: x[1], default=(None, -1)
                )
                g = greet()
                if match and best_score >= 0.38:
                    sid, roll, name, room, _ = match
                    row = sql("SELECT status, last_seen FROM students WHERE id=?", (sid,), fetch=True)[0]
                    if time.time() - row[1] >= 15:
                        act = "ENTRY" if row[0] == "OUT" else "EXIT"
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        sql("UPDATE students SET status=?, last_seen=? WHERE id=?", ("IN" if act == "ENTRY" else "OUT", time.time(), sid))
                        sql("INSERT INTO logs (roll_no, name, room_no, action, timestamp) VALUES (?,?,?,?,?)", (roll, name, room, act, ts))
                        threading.Thread(target=sync_cloud, args=({"type": "log", "roll": roll, "action": act, "time": ts},), daemon=True).start()
                        res = {"c": "OK", "t": f"RECOGNIZED - VERIFIED ({act})", "s": f"{g}, {name}! (Roll: {roll})"}
                        speak(f"{g} {name}. Recognized and verified. {act.lower()} recorded.")
                    else:
                        res = {"c": "OK", "t": f"RECOGNIZED - VERIFIED ({row[0]})", "s": f"{g}, {name} - Already scanned"}
                else:
                    res = {"c": "BAD", "t": "NEW USER FOUND", "s": "Not recognized - Ask Admin (aisys) to enroll"}
                    speak("New user found. Please ask admin to enroll.")
                state, t_until = "RES", time.time() + 3.0
                with lock: msg = res

            col = (94, 197, 34) if msg["c"] == "OK" else ((11, 158, 245) if msg["c"] == "WAIT" else (68, 68, 239))
            cv2.rectangle(frame, (int(hi_face[0]), int(hi_face[1])), (int(hi_face[0]+hi_face[2]), int(hi_face[1]+hi_face[3])), col, 2)

        if state == "RES" and time.time() >= t_until: state = "IDLE"
        if state == "IDLE" and not near:
            with lock: msg = {"c": "IDLE", "t": "Welcome", "s": "Please look at the camera"}

        _, jpg = cv2.imencode(".jpg", frame)
        with lock: jpg_bytes = jpg.tobytes()

# --- 4. MODERN KIOSK + ADMIN MENU & 2025-TODAY DATE DROPDOWN ---
HTML = """<!DOCTYPE html><html><head><title>Hostel Biometric</title><meta name="viewport" content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'Segoe UI',sans-serif;background:radial-gradient(circle at top,#172554,#090d16 70%);color:#f8fafc;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.top,.foot{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;background:rgba(15,23,42,.9);border-bottom:1px solid #1e293b;font-size:12px}
.pills{display:flex;gap:8px;align-items:center}.pill{background:#1e293b;padding:5px 10px;border-radius:20px;border:1px solid #334155;cursor:pointer}
.btn{background:#0284c7;color:#fff;border:none;padding:6px 14px;border-radius:20px;font-weight:700;cursor:pointer}.ibtn{background:#1e293b;color:#cbd5e1;border:1px solid #334155;padding:6px 10px;border-radius:20px;cursor:pointer}
.main{flex:1;display:grid;grid-template-columns:1.1fr 1fr;gap:18px;padding:18px;max-width:1100px;width:100%;margin:auto;align-items:center}
.cam{position:relative;border-radius:18px;overflow:hidden;border:2px solid rgba(56,189,248,.3);background:#000;aspect-ratio:4/3}.cam img{width:100%;height:100%;object-fit:cover}
.tag{position:absolute;top:12px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,.8);padding:4px 12px;border-radius:20px;font-size:11px;color:#4ade80}
.card{background:rgba(30,41,59,.7);border:2px solid #334155;border-radius:20px;padding:28px;text-align:center;backdrop-filter:blur(10px);transition:.3s}
.card.WAIT{border-color:#f59e0b}.card.OK{border-color:#22c55e;background:rgba(20,83,45,.45)}.card.BAD{border-color:#ef4444;background:rgba(127,29,29,.45)}
.ico{width:64px;height:64px;margin:0 auto 12px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:28px;border:2px solid #38bdf8;background:rgba(56,189,248,.15)}
.ov{position:fixed;inset:0;background:rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:.2s;z-index:10}.ov.open{opacity:1;pointer-events:auto}
.dr{position:fixed;top:0;right:0;height:100%;width:430px;max-width:95vw;background:#0f172a;border-left:1px solid #334155;transform:translateX(100%);transition:.25s;z-index:20;padding:16px;overflow-y:auto}.dr.open{transform:translateX(0)}
input,select{width:100%;padding:9px;margin:4px 0;border-radius:7px;border:1px solid #334155;background:#1e293b;color:#fff;font-size:13px}
.act{width:100%;padding:11px;margin-top:6px;border-radius:8px;border:none;background:#0284c7;color:#fff;font-weight:700;cursor:pointer;font-size:13px;text-align:center}
.mbtn{background:#1e293b;border:1px solid #334155;padding:13px;margin:6px 0;border-radius:10px;color:#fff;width:100%;text-align:left;cursor:pointer;font-size:14px;font-weight:600}
.mbtn:hover{border-color:#38bdf8;background:#172554}
.rowbtn{display:flex;gap:6px;align-items:center}
table{width:100%;border-collapse:collapse;font-size:11px;margin-top:6px}th,td{padding:5px 4px;border-bottom:1px solid #1e293b;text-align:left;vertical-align:middle}
.ENTRY{color:#4ade80;font-weight:700}.EXIT{color:#f87171;font-weight:700}
.sbtn{border:none;padding:3px 7px;border-radius:4px;cursor:pointer;color:#fff;font-size:11px}
.pic{width:28px;height:28px;border-radius:50%;object-fit:cover;border:1px solid #38bdf8}
@media(max-width:720px){.main{grid-template-columns:1fr;overflow-y:auto}}
</style></head><body>
<div class="top">
  <b style="color:#38bdf8;font-size:14px">HOSTEL BIOMETRIC</b><span id="clk" style="font-weight:600"></span>
  <div class="pills">
    <span id="snd" class="pill" onclick="toggleSnd()">Voice: ON</span>
    <span id="net" class="pill">...</span><span id="bat" class="pill">UPS / AC</span>
    <button class="ibtn" onclick="document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen()">[ ]</button>
    <button class="btn" onclick="openD()">Admin</button>
  </div>
</div>
<div class="main">
  <div class="cam"><img src="/video"><div class="tag">&#9679; LIVE SCANNER</div></div>
  <div id="sc" class="card IDLE">
    <div id="ico" class="ico">&#9711;</div>
    <div id="gr" style="color:#38bdf8;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:1px">Welcome</div>
    <h1 id="ti" style="font-size:26px;margin:6px 0">Welcome</h1><p id="ms" style="color:#cbd5e1">Please look at the camera</p>
  </div>
</div>
<div class="foot"><span>SD Card + Cloud Sync Ready</span><span>Low-Res Photo (112px) + Embeddings</span></div>
<div id="ov" class="ov" onclick="closeD()"></div>
<div id="dr" class="dr">
  <div style="display:flex;justify-content:space-between;margin-bottom:10px"><h3 style="color:#38bdf8">Admin Control</h3><button class="ibtn" onclick="closeD()">X</button></div>
  <div id="lv">
    <p style="font-size:12px;color:#94a3b8;margin-bottom:6px">Admin 1 Login Required (aisys / 1234)</p>
    <input id="au" placeholder="Admin Username" value="aisys">
    <input id="ap" type="password" placeholder="Password / PIN" onkeydown="if(event.key==='Enter')login()">
    <div id="lm" style="color:#f87171;font-size:12px;min-height:18px;margin-top:4px"></div>
    <button class="act" style="background:#16a34a" onclick="login()">Login</button>
  </div>
  <div id="av" style="display:none">
    <div id="st_cnt" style="color:#38bdf8;font-weight:700;font-size:12px;margin-bottom:10px"></div>

    <div id="tmenu">
      <p style="font-size:14px;font-weight:700;margin-bottom:8px;color:#e2e8f0">Welcome! What are you looking for?</p>
      <button class="mbtn" onclick="view('e')">1. New / Re-Enrollment of Student</button>
      <button class="mbtn" onclick="view('fp')">2. Fingerprint Enrollment</button>
      <button class="mbtn" onclick="view('l')">2. Data Log of Any Date (Jan 2025 - Today)</button>
      <button class="mbtn" onclick="view('s')">3. View / Edit / Delete Students</button>
      <button class="mbtn" onclick="view('cfg')">4. Register New Admin (aisys only)</button>
    </div>

    <div id="te" style="display:none">
      <button class="ibtn" onclick="view('menu')" style="margin-bottom:8px">&larr; Back to Menu</button>
      <input id="r" placeholder="Roll No"><input id="n" placeholder="Full Name"><input id="rm" placeholder="Room No">
      <select id="st_sel"><option value="OUT">Status: OUT</option><option value="IN">Status: IN</option></select>
      <div class="rowbtn">
        <button class="act" style="background:#16a34a" onclick="enroll()">Capture Face &amp; Save</button>
        <button class="act" style="background:#0284c7" onclick="updateStu()">Update Info</button>
      </div>
      <div id="em" style="color:#fbbf24;font-size:12px;margin-top:6px"></div>
    </div>
    <div id="tfp" style="display:none">

        <button class="ibtn" onclick="view('menu')" style="margin-bottom:8px"> &larr; Back to Menu</button>

  <h4 style="color:#38bdf8;margin:6px 0">
    Fingerprint Enrollment
  </h4>

  <p style="font-size:12px;color:#94a3b8;margin-bottom:10px">
    First, scan the unknown fingerprint. Then enter employee details
    after admin approval.
  </p>
  <input id="sim_scan" placeholder="Enter a test fingerprint code">

  <button class="act" style="background:#0284c7"
          onclick="scanFingerprint()">
    Detect Fingerprint
  </button>

  <div id="fscan_msg"
       style="color:#fbbf24;font-size:12px;margin:8px 0">
  </div>

  <div id="fdetails" style="display:none">

    <input id="froll" placeholder="Employee Roll No">
    <input id="fname" placeholder="Full Name">
    <input id="froom" placeholder="Room No">

    <button class="act" style="background:#16a34a"
            onclick="saveFingerprint()">
      Approve &amp; Enroll Fingerprint
    </button>

  </div>

  <div id="fresult"
       style="color:#fbbf24;font-size:12px;margin-top:8px">
  </div>

</div>

    <div id="tl" style="display:none">
      <button class="ibtn" onclick="view('menu')" style="margin-bottom:8px">&larr; Back to Menu</button>
      <div class="rowbtn">
        <select id="dt" onchange="loadAdmin()"></select>
        <button class="sbtn" style="background:#16a34a;padding:9px" onclick="csv()">CSV</button>
      </div>
      <table><thead><tr><th>Date/Time</th><th>Roll</th><th>Name</th><th>Act</th></tr></thead><tbody id="lg"></tbody></table>
    </div>

    <div id="ts" style="display:none">
      <button class="ibtn" onclick="view('menu')" style="margin-bottom:8px">&larr; Back to Menu</button>
      <table><thead><tr><th>Pic</th><th>Roll</th><th>Name</th><th>Room</th><th>St</th><th>Act</th></tr></thead><tbody id="stb"></tbody></table>
    </div>

    <div id="tcfg" style="display:none">
      <button class="ibtn" onclick="view('menu')" style="margin-bottom:8px">&larr; Back to Menu</button>
      <h4 style="color:#38bdf8;margin:6px 0">Register New Admin (aisys only)</h4>
      <input id="nu" placeholder="New Admin Username"><input id="np" type="password" placeholder="New Admin Password">
      <button class="act" style="background:#16a34a" onclick="regAdmin()">Register Admin</button>
      <div id="cm" style="color:#fbbf24;font-size:12px;margin-top:6px"></div>
    </div>

    <button class="act" style="background:#334155;margin-top:16px" onclick="logout()">Lock Admin</button>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
let u="", p="", logRows=[], lastVid=0, soundOn=true, ic={IDLE:"&#9711;",WAIT:"&#9203;",OK:"&#10004;",BAD:"&#10008;"};

(function(){
  let cur=new Date(), end=new Date('2025-01-01T00:00:00'), opts='<option value="">All Dates (Jan 2025 - Today)</option>';
  while(cur>=end){
    let iso=`${cur.getFullYear()}-${String(cur.getMonth()+1).padStart(2,'0')}-${String(cur.getDate()).padStart(2,'0')}`;
    let lbl=cur.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'});
    opts+=`<option value="${iso}">${lbl}</option>`; cur.setDate(cur.getDate()-1);
  }
  $('dt').innerHTML=opts;
})();

if(navigator.getBattery) navigator.getBattery().then(b=>{let f=()=>$('bat').innerText=Math.round(b.level*100)+'%'+(b.charging?' AC':'');f();b.onlevelchange=f;b.onchargingchange=f;});
function openD(){$('ov').classList.add('open');$('dr').classList.add('open');}
function closeD(){$('ov').classList.remove('open');$('dr').classList.remove('open');}
function toggleSnd(){soundOn=!soundOn;$('snd').innerText=soundOn?'Voice: ON':'Voice: OFF';}
function view(v){
    ['menu','e','fp','l','s','cfg'].forEach(
        x => $('t'+x).style.display = x == v ? 'block' : 'none'
    );
}
async function post(url, body){return (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,user:u,pin:p})})).json();}

async function refresh(){
  try{
    let h=new Date().getHours(), d=await(await fetch('/data')).json();
    $('clk').innerText=new Date().toLocaleString();
    $('net').innerText=d.ip?'Online: '+d.ip:'Offline (SD)';
    $('gr').innerText=h<12?'Good Morning':h<17?'Good Afternoon':'Good Evening';
    $('sc').className='card '+d.m.c; $('ico').innerHTML=ic[d.m.c]; $('ti').innerText=d.m.t; $('ms').innerText=d.m.s;
    if(d.web_v && d.vid!==lastVid && d.vtxt && soundOn){lastVid=d.vid; let ut=new SpeechSynthesisUtterance(d.vtxt); speechSynthesis.cancel(); speechSynthesis.speak(ut);}
    if(u) loadAdmin();
  }catch(e){}
}

async function loadAdmin(){
  let a=await post('/admin',{date:$('dt').value}); if(!a.ok) return;
  $('st_cnt').innerText=`Logged in: ${u} | Students: ${a.tot} | Inside: ${a.ins} | Today: ${a.tod}`;
  logRows=a.logs;
  $('lg').innerHTML=a.logs.map(l=>`<tr><td>${l[0]}</td><td>${l[1]}</td><td>${l[2]}</td><td class="${l[3]}">${l[3]}</td></tr>`).join('');
  $('stb').innerHTML=a.studs.map(s=>`<tr><td>${s[4]?`<img class="pic" src="data:image/jpeg;base64,${s[4]}">`:'-'}</td><td>${s[0]}</td><td>${s[1]}</td><td>${s[2]}</td><td>${s[3]}</td><td><button class="sbtn" style="background:#0284c7" onclick="fillEdit('${s[0]}','${s[1]}','${s[2]}','${s[3]}')">Edit</button> <button class="sbtn" style="background:#dc2626" onclick="del('${s[0]}')">Del</button></td></tr>`).join('');
}

function fillEdit(roll,name,room,st){$('r').value=roll;$('n').value=name;$('rm').value=room;$('st_sel').value=st;view('e');$('em').innerText='Loaded '+roll;}

async function login(){
  $('lm').innerText='Checking...';
  try{
    let res=await fetch('/admin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:$('au').value,pin:$('ap').value})});
    let data=await res.json();
    if(data.ok){u=$('au').value;p=$('ap').value;$('ap').value='';$('lm').innerText='';$('lv').style.display='none';$('av').style.display='block';view('menu');loadAdmin();}
    else $('lm').innerText=data.err || 'Wrong Username or Password!';
  }catch(err){$('lm').innerText='Server error: '+err.message;}
}

function logout(){u='';p='';$('av').style.display='none';$('lv').style.display='block';closeD();}
async function enroll(){$('em').innerText=(await post('/enroll',{roll:$('r').value,name:$('n').value,room:$('rm').value})).msg;loadAdmin();}
async function updateStu(){$('em').innerText=(await post('/update',{roll:$('r').value,name:$('n').value,room:$('rm').value,status:$('st_sel').value})).msg;loadAdmin();}
async function del(roll){if(confirm('Delete '+roll+'?')){await post('/delete',{roll});loadAdmin();}}
async function regAdmin(){$('cm').innerText=(await post('/reg_admin',{new_user:$('nu').value,new_pin:$('np').value})).msg;$('nu').value='';$('np').value='';}
function csv(){let b=new Blob(["DateTime,Roll,Name,Action\\n"+logRows.map(x=>x.join(',')).join('\\n')],{type:'text/csv'});let a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='hostel_logs.csv';a.click();}
setInterval(refresh,900);refresh();
async function scanFingerprint() {
    const code = document.getElementById('sim_scan').value.trim();

    if (!code) {
        $('fscan_msg').textContent = 'Enter a test fingerprint code first.';
        return;
    }

    $('fscan_msg').textContent = 'Scanning fingerprint...';
    $('fdetails').style.display = 'none';
    $('fresult').textContent = '';

    try {
        const r = await post('/fingerprint/scan', {
            simulated_scan: code
        });

        const result = r.result;

        if (result && result.t === 'UNKNOWN FINGERPRINT') {
            $('fscan_msg').textContent =
                'Unknown fingerprint detected. Admin approval required.';
            $('fdetails').style.display = 'block';
        }
        else if (r.ok) {
            $('fscan_msg').textContent = result.t;
            $('fresult').textContent = result.s;
        }
        else {
            $('fscan_msg').textContent =
                (result && result.s) || r.msg || 'Fingerprint scan failed.';
        }
    }
    catch (error) {
        $('fscan_msg').textContent = 'Unable to connect to the server.';
    }
}
async function saveFingerprint() {
    const roll = $('froll').value.trim();
    const name = $('fname').value.trim();
    const room = $('froom').value.trim();

    if (!roll || !name) {
        $('fresult').textContent =
            'Please enter the employee roll number and name.';
        return;
    }

    $('fresult').textContent = 'Enrolling fingerprint...';

    try {
        const r = await post('/fingerprint/enroll', {
            roll: roll,
            name: name,
            room: room
        });

        $('fresult').textContent = r.msg;

        if (r.ok) {
            $('fdetails').style.display = 'none';
            $('sim_scan').value = '';
            $('froll').value = '';
            $('fname').value = '';
            $('froom').value = '';
            $('fscan_msg').textContent = 'Fingerprint enrolled successfully.';
        }
    }
    catch (error) {
        $('fresult').textContent =
            'Unable to connect to the server.';
    }
}
</script></body></html>"""

# --- 5. ROUTES ---
@app.route("/")
def home(): return HTML

@app.route("/video")
def video():
    def g():
        while True:
            with lock: b = jpg_bytes
            if b: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + b + b"\r\n"
            time.sleep(0.03)
    return Response(g(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/data")
def data():
    with lock: m = dict(msg)
    return jsonify({"ip": net_ip(), "m": m, "vid": voice_id, "vtxt": voice_txt, "web_v": not IS_PI})

@app.route("/admin", methods=["POST"])
def admin():
    try:
        d = request.json or {}
        if not check_admin(d): return jsonify({"ok": False})
        dt = (d.get("date") or "").strip()
        logs = sql("SELECT timestamp, roll_no, name, action FROM logs WHERE timestamp LIKE ? ORDER BY id DESC LIMIT 300", (dt + "%",), fetch=True) if dt else sql("SELECT timestamp, roll_no, name, action FROM logs ORDER BY id DESC LIMIT 300", fetch=True)
        rows = sql("SELECT roll_no, name, room_no, status, photo FROM students ORDER BY id DESC", fetch=True)
        studs = [(r[0], r[1], r[2], r[3], base64.b64encode(r[4]).decode() if r[4] else "") for r in rows]
        tod = datetime.now().strftime("%Y-%m-%d") + "%"
        return jsonify({"ok": True, "tot": sql("SELECT COUNT(*) FROM students", fetch=True)[0][0], "ins": sql("SELECT COUNT(*) FROM students WHERE status='IN'", fetch=True)[0][0], "tod": sql("SELECT COUNT(*) FROM logs WHERE timestamp LIKE ?", (tod,), fetch=True)[0][0], "studs": studs, "logs": logs})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})

@app.route("/enroll", methods=["POST"])
def enroll():
    d = request.json or {}
    if not check_admin(d): return jsonify({"msg": "Admin login required!"})
    roll, name, room = d["roll"].strip(), d["name"].strip(), d["room"].strip()
    if not roll or not name: return jsonify({"msg": "Enter Roll & Name!"})
    with lock:
        if last_face is None or time.time() - last_time > 3:
            return jsonify({"msg": "Look at camera & try again!"})
        fr, fc = last_frame.copy(), last_face.copy()
    with mlock:
        aligned = recognizer.alignCrop(fr, fc)
        emb = recognizer.feature(aligned).astype(np.float32).tobytes()
        _, p_jpg = cv2.imencode(".jpg", aligned, [cv2.IMWRITE_JPEG_QUALITY, 60])
        photo = p_jpg.tobytes()
    sql("""
    INSERT INTO students
        (roll_no, name, room_no, face_embedding, photo, status, last_seen)
    VALUES (?, ?, ?, ?, ?, 'OUT', 0)
    ON CONFLICT(roll_no) DO UPDATE SET
        name = excluded.name,
        room_no = excluded.room_no,
        face_embedding = excluded.face_embedding,
        photo = excluded.photo
""", (roll, name, room, emb, photo))
    reload_cache()
    speak(f"Enrollment complete for {name}")
    threading.Thread(target=sync_cloud, args=({"type": "enroll", "roll": roll, "name": name, "room": room},), daemon=True).start()
    return jsonify({"msg": f"Enrolled {name} (low-res photo + embedding saved)!"})

# --- FINGERPRINT SCAN ROUTE ---

@app.route("/fingerprint/scan", methods=["POST"])
def fingerprint_scan():

    # This route is only for Windows simulation
    if FINGERPRINT_MODE != "simulation":
        return jsonify({
            "ok": False,
            "msg": "Simulation is disabled."
        }), 400

    d = request.json or {}
    simulated_scan = (d.get("simulated_scan") or "").strip()

    if not simulated_scan:
        return jsonify({
            "ok": False,
            "msg": "No fingerprint scan provided."
        }), 400

    # Ask the fingerprint service to identify the scan
    result = fingerprint_service.identify(simulated_scan)

    if result["status"] == "matched":

        # Process ENTRY/EXIT using the existing employee database
        result = process_fingerprint_access(
            result["fingerprint_id"]
        )

    else:
        result = {
            "c": "BAD",
            "t": "UNKNOWN FINGERPRINT",
            "s": "Admin approval required for enrollment."
        }

    # Update the shared status message
    with lock:
        msg.update(result)

    return jsonify({
        "ok": result["c"] == "OK",
        "result": result
    })
# --- ADMIN-APPROVED FINGERPRINT ENROLLMENT ---

@app.route("/fingerprint/enroll", methods=["POST"])
def enroll_fingerprint():

    d = request.json or {}

    # Verify admin
    if not check_admin(d):
        return jsonify({
            "ok": False,
            "msg": "Admin login required!"
        })

    roll = (d.get("roll") or "").strip()
    name = (d.get("name") or "").strip()
    room = (d.get("room") or "").strip()

    if not roll or not name:
        return jsonify({
            "ok": False,
            "msg": "Enter employee roll number and name."
        })

    # Check whether an unknown fingerprint scan is waiting
    simulated_scan = fingerprint_service.pending_scan

    if simulated_scan is None:
        return jsonify({
            "ok": False,
            "msg": "No pending fingerprint. Scan an unknown finger first."
        })

    # Enroll the pending fingerprint and obtain its ID
    fingerprint_id = fingerprint_service.enroll(simulated_scan)

    if fingerprint_id is None:
        return jsonify({
            "ok": False,
            "msg": "Fingerprint enrollment failed."
        })

    # Check whether this fingerprint ID is already assigned
    if find_employee_by_fingerprint(fingerprint_id):
        fingerprint_service.simulated_templates.pop(
            simulated_scan, None
        )
        return jsonify({
            "ok": False,
            "msg": "Fingerprint ID is already assigned. Please try again."
        })

    # Attach fingerprint ID to an existing employee or create a new one
    existing = sql(
        "SELECT id FROM students WHERE roll_no=?",
        (roll,),
        fetch=True
    )

    try:
        if existing:
            sql(
                """UPDATE students
                SET name=?, room_no=?, fingerprint_id=?
                WHERE roll_no=?""",
                (name, room, fingerprint_id, roll)
            )
        else:
            sql(
                """INSERT INTO students
                (roll_no, name, room_no, fingerprint_id, status, last_seen)
                VALUES (?, ?, ?, ?, 'OUT', 0)""",
                (roll, name, room, fingerprint_id)
            )

    except sqlite3.IntegrityError:
        fingerprint_service.simulated_templates.pop(
            simulated_scan, None
        )
        return jsonify({
            "ok": False,
            "msg": "This fingerprint ID is already assigned."
        })

        # Save the simulated fingerprint mapping permanently
    try:
        sql(
            """INSERT INTO fingerprint_simulation
            (scan_code, fingerprint_id) VALUES (?, ?)""",
            (simulated_scan, fingerprint_id)
        )
    except sqlite3.IntegrityError:
        return jsonify({
            "ok": False,
            "msg": "Fingerprint mapping already exists. Please contact admin."
        })

    # Update in-memory mapping only after successful database save
    fingerprint_service.simulated_templates[simulated_scan] = fingerprint_id
    fingerprint_service.pending_scan = None

    reload_cache()

    return jsonify({
        "ok": True,
        "msg": f"Fingerprint enrolled successfully for {name}.",
        "fingerprint_id": fingerprint_id
    })
# --- FINGERPRINT EMPLOYEE ASSIGNMENT ---

@app.route("/fingerprint/assign", methods=["POST"])
def assign_fingerprint():
    d = request.json or {}

    if not check_admin(d):
        return jsonify({"ok": False, "msg": "Admin login required!"})

    roll = (d.get("roll") or "").strip()
    name = (d.get("name") or "").strip()
    room = (d.get("room") or "").strip()
    fingerprint_id = d.get("fingerprint_id")

    if not roll or not name or fingerprint_id is None:
        return jsonify({
            "ok": False,
            "msg": "Enter employee details and fingerprint ID."
        })

    try:
        fingerprint_id = int(fingerprint_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "Invalid fingerprint ID."})

        # Check whether this employee already exists
    existing = sql(
        "SELECT id FROM students WHERE roll_no=?",
        (roll,),
        fetch=True
    )

    try:
        if existing:
            sql(
                """UPDATE students
                SET name=?, room_no=?, fingerprint_id=?
                WHERE roll_no=?""",
                (name, room, fingerprint_id, roll)
            )
        else:
            sql(
                """INSERT INTO students
                (roll_no, name, room_no, fingerprint_id, status, last_seen)
                VALUES (?, ?, ?, ?, 'OUT', 0)""",
                (roll, name, room, fingerprint_id)
            )

    except sqlite3.IntegrityError:
        return jsonify({
            "ok": False,
            "msg": "This fingerprint ID is already assigned to another employee."
        })

    reload_cache()

    return jsonify({
        "ok": True,
        "msg": f"Fingerprint ID {fingerprint_id} assigned to {name}."
    })


@app.route("/update", methods=["POST"])
def update():
    d = request.json or {}
    if not check_admin(d): return jsonify({"msg": "Admin login required!"})
    sql("UPDATE students SET name=?, room_no=?, status=? WHERE roll_no=?", (d["name"].strip(), d["room"].strip(), d["status"], d["roll"].strip()))
    reload_cache()
    return jsonify({"msg": f"Updated {d['roll']}!"})

@app.route("/delete", methods=["POST"])
def delete():
    if not check_admin(request.json or {}): return jsonify({"ok": False})
    sql("DELETE FROM students WHERE roll_no=?", (request.json["roll"],))
    reload_cache()
    return jsonify({"ok": True})

@app.route("/reg_admin", methods=["POST"])
def reg_admin():
    d = request.json or {}
    if not check_admin(d) or d.get("user") != "aisys":
        return jsonify({"msg": "Admin 1 (aisys) login required!"})
    nu, npin = d["new_user"].strip(), d["new_pin"].strip()
    if not nu or not npin: return jsonify({"msg": "Enter username & password!"})
    sql("INSERT OR REPLACE INTO admins (username, pin) VALUES (?, ?)", (nu, npin))
    return jsonify({"msg": f"Admin '{nu}' registered!"})

if __name__ == "__main__":
    threading.Thread(target=camera_loop, daemon=True).start()
    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)