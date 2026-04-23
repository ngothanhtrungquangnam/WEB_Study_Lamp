"""
============================================================
 SMART STUDY ASSISTANT - Web Server (Python Flask)
 Trường Đại học Bách Khoa Đà Nẵng
============================================================
 Cài đặt:
   pip install flask flask-cors

 Chạy:
   python server.py

 ESP32 gửi lên:  POST /api/study    (JSON data)
 ESP32 nhận về:  GET  /api/command  (lệnh điều khiển)
 Browser xem:    GET  /             (dashboard)
============================================================
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime
import json, os, threading
import paho.mqtt.client as mqtt
app = Flask(__name__, static_folder='static')
CORS(app)

# ============================================================
#  BỘ NHỚ TRẠNG THÁI HỆ THỐNG
# ============================================================
state_lock = threading.Lock()

system_state = {
    "state":          "IDLE",
    "remaining_sec":  0,
    "lux":            0.0,
    "led_brt":        0,
    "hour":           0,
    "min":            0,
    "sec":            0,
    "date":           0,        # ← bổ sung (bản cũ thiếu init)
    "month":          0,        # ← bổ sung
    "year":           0,        # ← bổ sung
    "sessions_today": 0,
    "total_time_sec": 0,
    "led_r":          0,
    "led_g":          0,
    "led_b":          0,
    "last_seen":      None,
    "online":         False,
}

# # Hàng đợi lệnh gửi xuống ESP32
# pending_command = {
#     "cmd": None,   # "START" | "STOP" | "RESET" | "COLOR" | None
#     "r":   0,
#     "g":   0,
#     "b":   0,
# }

# Lịch sử
study_log    = []
lux_history  = []
MAX_LUX_HISTORY = 200
MAX_LOG_MEM     = 500   # số mục tối đa giữ trong RAM
LOG_FILE        = "study_log.json"

# Đọc log cũ nếu có
if os.path.exists(LOG_FILE):
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            study_log = json.load(f)
        print(f"[LOG] Đã đọc {len(study_log)} mục từ {LOG_FILE}")
    except Exception as e:
        print(f"[LOG] Đọc file lỗi: {e}")
        study_log = []

# ==========================================
# CẤU HÌNH MQTT BROKER
# ==========================================
# Sửa từ hivemq sang emqx
MQTT_BROKER = "broker.emqx.io" 
MQTT_PORT = 1883
TOPIC_STATUS = "dut/smartclock/trung/status"
TOPIC_COMMAND = "dut/smartclock/trung/command"

def on_connect(client, userdata, flags, rc):
    print("✅ Đã kết nối tới MQTT Broker thành công!")
    client.subscribe(TOPIC_STATUS) # Bắt đầu lắng nghe ESP32

def on_message(client, userdata, msg):
    # Khai báo các biến global y hệt như hàm /api/study cũ
    global system_state, _last_state, _lux_sum, _lux_count, _work_start_sec 

    try:
        # Đọc dữ liệu từ MQTT gửi về
        payload = msg.payload.decode('utf-8')
        data = json.loads(payload)
        now = datetime.now()

        with state_lock:
            prev_state = system_state["state"]
            cur_state  = data.get("state", system_state["state"])

            # Cập nhật toàn bộ trạng thái
            system_state.update({
                "state":          cur_state,
                "remaining_sec":  data.get("remaining_sec",  0),
                "lux":            data.get("lux",            0.0),
                "led_brt":        data.get("led_brt",        0),
                "hour":           data.get("hour",           0),
                "min":            data.get("min",            0),
                "sec":            data.get("sec",            0),
                "date":           data.get("date",           0),
                "month":          data.get("month",          0),
                "year":           data.get("year",           0),
                "sessions_today": data.get("sessions_today", 0),
                "total_time_sec": data.get("total_time_sec", 0),
                "led_r":          data.get("led_r",          0),
                "led_g":          data.get("led_g",          0),
                "led_b":          data.get("led_b",          0),
                "last_seen":      now.strftime("%Y-%m-%d %H:%M:%S"),
                "online":         True,
            })

            # Ghi lịch sử lux
            lux_val = data.get("lux", 0.0)
            lux_history.append({
                "time": now.strftime("%H:%M:%S"),
                "lux":  round(lux_val, 1),
            })
            if len(lux_history) > MAX_LUX_HISTORY:
                lux_history.pop(0)

            # Tích lũy lux
            _lux_sum   += lux_val
            _lux_count += 1

            # ── Phát hiện chuyển trạng thái → ghi log ──
            if prev_state != cur_state:
                avg_lux   = (_lux_sum / _lux_count) if _lux_count > 0 else 0.0
                ts        = make_rtc_timestamp(data, now)
                total_sec = data.get("total_time_sec", 0)

                if prev_state == "WORK":
                    duration = total_sec - _work_start_sec
                    add_log_entry("WORK_END", max(duration, 0), avg_lux, ts)
                elif prev_state == "BREAK":
                    add_log_entry("BREAK_END", 0, avg_lux, ts)
                elif prev_state in ("WORK", "BREAK", "PAUSE") and cur_state == "IDLE":
                    add_log_entry("RESET", 0, avg_lux, ts)

                # Ghi nhớ điểm bắt đầu WORK
                if cur_state == "WORK" and prev_state != "WORK":
                    _work_start_sec = total_sec

                # Reset bộ tích lũy lux
                _lux_sum   = 0.0
                _lux_count = 0

        # In ra Terminal (Có chữ [MQTT] để dễ nhận biết)
        print(f"[MQTT ESP32] {now.strftime('%H:%M:%S')} | "
              f"State={cur_state} | "
              f"Remain={data.get('remaining_sec', 0)}s | "
              f"Lux={lux_val:.1f} | "
              f"LED=rgb({data.get('led_r',0)},{data.get('led_g',0)},{data.get('led_b',0)})")

    except Exception as e:
        print("Lỗi đọc MQTT:", e)

# Khởi động MQTT Client chạy ngầm
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start() # Hàm này giúp MQTT chạy song song với Flask
# ==========================================


# ============================================================
#  HÀM TIỆN ÍCH
# ============================================================
def save_log():
    """Lưu log ra file JSON, giữ tối đa 500 mục gần nhất."""
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(study_log[-500:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[LOG] Ghi file lỗi: {e}")


def add_log_entry(event_type, duration_sec, lux_avg, rtc_timestamp=None):
    """Thêm 1 mục vào log, giới hạn RAM."""
    entry = {
        "timestamp": rtc_timestamp if rtc_timestamp else datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event":     event_type,
        "duration":  int(duration_sec),
        "lux_avg":   round(lux_avg, 1),
    }
    study_log.append(entry)
    # Giới hạn RAM — xóa mục cũ nhất nếu vượt quá
    if len(study_log) > MAX_LOG_MEM:
        study_log.pop(0)
    save_log()
    print(f"[LOG] Ghi sự kiện: {event_type} | duration={duration_sec}s | lux_avg={lux_avg:.1f}")
    return entry


def make_rtc_timestamp(data, fallback_now):
    """Tạo timestamp từ RTC ESP32, fallback về giờ server nếu thiếu."""
    h  = data.get("hour",  fallback_now.hour)
    m  = data.get("min",   fallback_now.minute)
    s  = data.get("sec",   fallback_now.second)
    dd = data.get("date",  fallback_now.day)
    mo = data.get("month", fallback_now.month)
    yr = data.get("year",  fallback_now.year % 100)
    return "20%02d-%02d-%02d %02d:%02d:%02d" % (yr, mo, dd, h, m, s)

def is_online():
    """Kiểm tra ESP32 còn online không (timeout 25 giây)."""
    if not system_state["last_seen"]:
        return False
    try:
        delta = (datetime.now() -
                 datetime.strptime(system_state["last_seen"],
                                   "%Y-%m-%d %H:%M:%S")).total_seconds()
        # Đã thay đổi: Cho phép trễ tối đa 25 giây
        return delta < 25 
    except Exception:
        return False


# ============================================================
#  Biến theo dõi chuyển trạng thái
# ============================================================
_last_state     = "IDLE"
_lux_sum        = 0.0
_lux_count      = 0
_work_start_sec = 0   # total_time_sec lúc bắt đầu WORK — để tính duration chính xác



# ============================================================
#  API: Browser → Server  (POST /api/control)
# ============================================================
@app.route("/api/control", methods=["POST"])
def control():
    data = request.get_json(silent=True)
    if not data or "cmd" not in data:
        return jsonify({"error": "missing cmd"}), 400

    cmd = data["cmd"].upper()
    if cmd not in ("START", "STOP", "RESET", "COLOR"):
        return jsonify({"error": f"unknown cmd: {cmd}"}), 400

    # Đóng gói lệnh thành JSON
    payload = {"cmd": cmd}
    log_extra = ""
    
    if cmd == "COLOR":
        payload["r"] = max(0, min(255, int(data.get("r", 255))))
        payload["g"] = max(0, min(255, int(data.get("g", 255))))
        payload["b"] = max(0, min(255, int(data.get("b", 255))))
        log_extra = f"R={payload['r']} G={payload['g']} B={payload['b']}"

    # BẮN LỆNH THẲNG XUỐNG ESP32 QUA MQTT
    mqtt_client.publish(TOPIC_COMMAND, json.dumps(payload))
    print(f"[WEB MQTT] Đã bắn lệnh → {cmd} {log_extra}")

    return jsonify({"status": "published_to_mqtt", "cmd": cmd}), 200


# ============================================================
#  API: Trạng thái hiện tại  (GET /api/status)
# ============================================================
@app.route("/api/status", methods=["GET"])
def get_status():
    with state_lock:
        system_state["online"] = is_online()
        return jsonify(dict(system_state)), 200


# ============================================================
#  API: Lịch sử lux  (GET /api/lux)
# ============================================================
@app.route("/api/lux", methods=["GET"])
def get_lux():
    # Tham số ?n=60 để lấy số điểm tùy chọn
    n = min(int(request.args.get("n", 60)), MAX_LUX_HISTORY)
    with state_lock:
        return jsonify(lux_history[-n:]), 200


# ============================================================
#  API: Log phiên học  (GET /api/log)
# ============================================================
@app.route("/api/log", methods=["GET"])
def get_log():
    n = min(int(request.args.get("n", 50)), MAX_LOG_MEM)
    with state_lock:
        return jsonify(list(reversed(study_log[-n:]))), 200


# ============================================================
#  API: Xóa log  (DELETE /api/log)  ← BỔ SUNG MỚI
# ============================================================
@app.route("/api/log", methods=["DELETE"])
def clear_log():
    with state_lock:
        study_log.clear()
        save_log()
    print("[LOG] Đã xóa toàn bộ lịch sử phiên học.")
    return jsonify({"status": "cleared"}), 200


# ============================================================
#  API: Thống kê nhanh  (GET /api/stats)  ← BỔ SUNG MỚI
# ============================================================
@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Trả về thống kê tổng hợp từ log."""
    with state_lock:
        total_work = sum(
            e["duration"] for e in study_log if e["event"] == "WORK_END"
        )
        total_sessions = sum(
            1 for e in study_log if e["event"] == "WORK_END"
        )
        avg_lux_work = 0.0
        work_entries = [e for e in study_log if e["event"] == "WORK_END"]
        if work_entries:
            avg_lux_work = sum(e["lux_avg"] for e in work_entries) / len(work_entries)

        return jsonify({
            "total_work_sec":    total_work,
            "total_work_min":    round(total_work / 60, 1),
            "total_sessions":    total_sessions,
            "avg_lux_during_work": round(avg_lux_work, 1),
            "log_entries":       len(study_log),
        }), 200


# ============================================================
#  Phục vụ file tĩnh (dashboard HTML)
# ============================================================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# Cho phép truy cập file tĩnh khác nếu cần
@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


# ============================================================
#  MAIN
# ============================================================
if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)

    # Lấy IP máy tính để hiển thị cho tiện
    import socket
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "127.0.0.1"

    print("=" * 55)
    print("  Smart Study Dashboard Server")
    print(f"  Browser  : http://localhost:5000")
    print(f"  ESP32    : POST http://{local_ip}:5000/api/study")
    print(f"  IP máy   : {local_ip}")
    print("=" * 55)
    print("  Endpoints:")
    print("  POST /api/study    ← ESP32 gửi data lên")
    print("  GET  /api/command  ← ESP32 lấy lệnh")
    print("  GET  /api/status   ← Dashboard lấy trạng thái")
    print("  GET  /api/lux      ← Dashboard lấy lịch sử lux")
    print("  GET  /api/log      ← Dashboard lấy log phiên học")
    print("  DELETE /api/log    ← Xóa toàn bộ log")
    print("  GET  /api/stats    ← Thống kê tổng hợp")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5000, debug=True)