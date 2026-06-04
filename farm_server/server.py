import asyncio
import tempfile
import json
import queue
import threading
import time
import sys
import os
import webbrowser
import unicodedata

import requests
import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer
from flask import Flask, render_template_string, jsonify

ESP32_IP   = "192.168.100.50"  
ESP32_PORT = 80
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")
SAMPLE_RATE = 16000

app = Flask(__name__)

log_messages = []  
is_listening = False
is_speaking  = False   
alert_chuong_lon = False  
ptt_active = False          # Push-to-Talk: chỉ xử lý giọng nói khi đang giữ nút
ptt_grace_until = 0         # Thời điểm kết thúc grace period sau khi thả nút
esp32_lock = threading.Lock()

import random
sensor_data = {
    "nhiet_do":   [],
    "do_am":      [],
    "anh_sang":   [],
    "nhan":       []
}

def _init_sensor_labels():
    now = time.time()
    for i in range(30, 0, -1):
        t = now - i * 10
        sensor_data["nhan"].append(time.strftime("%H:%M:%S", time.localtime(t)))
    # Khởi tạo bằng random walk để đường biểu đồ mượt tự nhiên
    t_val, h_val, l_val = 36.5, 68.0, 480.0
    for _ in range(30):
        t_val = round(max(35.0, min(39.0, t_val + random.uniform(-0.3, 0.3))), 1)
        h_val = round(max(50.0, min(90.0, h_val + random.uniform(-0.5, 0.5))), 1)
        l_val = round(max(200.0, min(800.0, l_val + random.uniform(-5,  5 ))))
        sensor_data["nhiet_do"].append(t_val)
        sensor_data["do_am"].append(h_val)
        sensor_data["anh_sang"].append(l_val)

_init_sensor_labels()

def sensor_loop():
    """Cập nhật dữ liệu cảm biến giả lập mỗi 2 giây."""
    global alert_chuong_lon
    while True:
        time.sleep(2)
        now_str = time.strftime("%H:%M:%S")
        sensor_data["nhan"].append(now_str)
        # Random walk: chỉ thay đổi nhỏ so với điểm trước
        prev_t = sensor_data["nhiet_do"][-1]
        prev_h = sensor_data["do_am"][-1]
        prev_l = sensor_data["anh_sang"][-1]
        sensor_data["nhiet_do"].append(round(max(35.0, min(39.0, prev_t + random.uniform(-0.3, 0.3))), 1))
        sensor_data["do_am"].append(   round(max(50.0, min(90.0, prev_h + random.uniform(-0.5, 0.5))), 1))
        sensor_data["anh_sang"].append(round(max(200.0, min(800.0, prev_l + random.uniform(-5,  5 )))))
        nhiet_hien_tai = sensor_data["nhiet_do"][-1]
        for k in ["nhan", "nhiet_do", "do_am", "anh_sang"]:
            if len(sensor_data[k]) > 30:
                sensor_data[k].pop(0)
        nhiet_hien_tai = sensor_data["nhiet_do"][-1]
        if nhiet_hien_tai >= 39 and not alert_chuong_lon:
            alert_chuong_lon = True
            log(f"CANH BAO: Nhiet do chuong lon {nhiet_hien_tai}°C >= 39°C!")
            speak(f"Cảnh báo! Nhiệt độ chuồng lợn {nhiet_hien_tai} độ, vượt ngưỡng an toàn!")
        elif nhiet_hien_tai < 39 and alert_chuong_lon:
            alert_chuong_lon = False
            log(f"Nhiet do chuong lon da ve binh thuong: {nhiet_hien_tai}°C")
            speak("Nhiệt độ chuồng lợn đã về bình thường")




def log(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry)
    log_messages.append(entry)
    if len(log_messages) > 50:
        log_messages.pop(0)

def speak(text):
    log(f"TTS: {text}")
    def _speak():
        try:
            import edge_tts
            from playsound import playsound
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            tmp.close()
            async def _gen():
                comm = edge_tts.Communicate(text, "vi-VN-HoaiMyNeural")
                await comm.save(tmp.name)
            asyncio.run(_gen())
            playsound(tmp.name)
            os.unlink(tmp.name)
        except Exception as e:
            log(f"Loi TTS: {e}")
    threading.Thread(target=_speak, daemon=True).start()

def send_to_esp32(command, timeout=4):
    with esp32_lock:
        try:
            url = f"http://{ESP32_IP}:{ESP32_PORT}/cmd"
            resp = requests.get(url, params={"cmd": command}, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                msg = data.get("msg", "Da thuc hien")
                log(f"ESP32 phan hoi: {msg}")
                return msg
            else:
                log(f"Loi HTTP {resp.status_code}")
                return "Loi ket noi ESP32"
        except requests.ConnectionError:
            log("Khong ket noi duoc ESP32! Kiem tra IP va WiFi.")
            return "Khong ket noi duoc ESP32"
        except Exception as e:
            log(f"Loi: {e}")
            return "Co loi xay ra"

def normalize_vn(text):
    """Bỏ dấu tiếng Việt, lowercase, chuẩn hóa khoảng trắng để so khớp keyword."""
    text = text.lower().strip()
    # Bỏ dấu câu (Web Speech API hay thêm . , ? ! cuối câu)
    text = ''.join(c for c in text if c not in '.,!?;:…')
    # Xử lý đ/Đ trước (không bị NFD decompose)
    text = text.replace('đ', 'd').replace('Đ', 'd')
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    text = ' '.join(text.split())
    return text

def process_voice_command(text):
    original = text
    text_norm = normalize_vn(text)
    log(f"Nhan dien: '{original}' → '{text_norm}'")

    if len(text_norm) < 2:
        return

    # Keyword (đã bỏ dấu) → command
    keywords = {
        "bat den vuon rau":    "bat vuon rau",
        "bat vuon rau":        "bat vuon rau",
        "tat den vuon rau":    "tat vuon rau",
        "tat vuon rau":        "tat vuon rau",
        "bat den chuong lon":  "bat chuong lon",
        "bat chuong lon":      "bat chuong lon",
        "tat den chuong lon":  "tat chuong lon",
        "tat chuong lon":      "tat chuong lon",
        "bat den nha ve sinh": "bat nha ve sinh",
        "bat nha ve sinh":     "bat nha ve sinh",
        "tat den nha ve sinh": "tat nha ve sinh",
        "tat nha ve sinh":     "tat nha ve sinh",
        "bat quat chuong":     "bat quat",
        "tat quat chuong":     "tat quat",
        "bat quat":            "bat quat",
        "tat quat":            "tat quat",
        "bat toan bo":         "bat tat ca",
        "tat toan bo":         "tat tat ca",
        "bat tat ca":          "bat tat ca",
        "tat tat ca":          "tat tat ca",
        "bat het":             "bat tat ca",
        "tat het":             "tat tat ca",
        "bat den":             "bat tat ca",
        "tat den":             "tat tat ca",
        "trang thai":          "trang thai",
        "kiem tra":            "trang thai",
        # cảm biến
        "nhiet do chuong lon":  "hoi nhiet do",
        "nhiet do hien tai":    "hoi nhiet do",
        "nhiet do la bao nhieu": "hoi nhiet do",
        "nhiet do":             "hoi nhiet do",
        "do am hien tai":       "hoi do am",
        "do am la bao nhieu":   "hoi do am",
        "do am":                "hoi do am",
        "anh sang hien tai":    "hoi anh sang",
        "cuong do anh sang":    "hoi anh sang",
        "anh sang la bao nhieu": "hoi anh sang",
        "anh sang":             "hoi anh sang",
    }

    COMPOUND = {
        "bat vuon rau":    (["bat tuoi"],                       "Đã bật đèn vườn rau"),
        "tat vuon rau":    (["tat tuoi"],                       "Đã tắt đèn vườn rau"),
        "bat chuong lon":  (["bat den", "bat quat"],            "Đã bật đèn và quạt chuồng lợn"),
        "tat chuong lon":  (["tat den", "tat quat"],            "Đã tắt đèn và quạt chuồng lợn"),
        "bat nha ve sinh": (["mo cua"],                         "Đã bật đèn nhà vệ sinh"),
        "tat nha ve sinh": (["dong cua"],                       "Đã tắt đèn nhà vệ sinh"),
        "bat quat":        (["bat quat"],                       "Đã bật quạt chuồng lợn"),
        "tat quat":        (["tat quat"],                       "Đã tắt quạt chuồng lợn"),
        "bat tat ca":      (["bat tat ca"],                     "Đã bật tất cả đèn"),
        "tat tat ca":      (["tat tat ca"],                     "Đã tắt tất cả đèn"),
        "trang thai":      (["trang thai"],                     "Đang kiểm tra trạng thái hệ thống"),
    }

    matched_cmd = None
    # Sắp xếp keyword dài trước để tránh match nhầm (vd "bat den" trước "bat")
    for keyword in sorted(keywords.keys(), key=len, reverse=True):
        if keyword in text_norm:
            matched_cmd = keywords[keyword]
            log(f"Khop keyword: '{keyword}' → {matched_cmd}")
            break

    # Fuzzy fallback: Vosk hay nghe nhầm từ địa điểm (vd "vuon"→"quen", "rau"→"sau")
    # Dùng action (bat/tat) + location hints để xác định lệnh
    if not matched_cmd:
        words = set(text_norm.split())
        # "bat" cũng bị nghe thành "vat"
        # "tat" thường bị nghe thành "tach", "sach", "dach", "dat", "tac"
        BAT_VARIANTS = {"bat", "vat", "bac", "vac", "ban", "van", "pac"}
        TAT_VARIANTS = {"tat", "tach", "sach", "dach", "dat", "tac", "sac",
                        "tach", "tech", "teck", "tak", "dac", "tac", "tắc"}
        if any(w in words for w in BAT_VARIANTS):
            action = "bat"
        elif any(w in words for w in TAT_VARIANTS):
            action = "tat"
        else:
            action = None
        if action:
            loc_cmd = None
            # vườn rau
            if any(w in words for w in ["vuon", "vun", "von", "bun", "bung",
                                         "rau", "sau", "mau", "cau", "hau", "tuoi"]):
                loc_cmd = f"{action} vuon rau"
            # chuồng lợn
            elif any(w in words for w in ["chuong", "thuong", "truong", "chong",
                                           "trong", "tuong", "cuong", "cong",
                                           "lon", "long", "lin", "lun", "dem"]):
                loc_cmd = f"{action} chuong lon"
            # nhà vệ sinh
            elif any(w in words for w in ["nha", "na", "sinh", "tinh", "trinh",
                                           "ve", "be", "we"]):
                loc_cmd = f"{action} nha ve sinh"
            elif any(w in words for w in ["quat", "kuat", "guat"]):
                loc_cmd = f"{action} quat"
            elif any(w in words for w in ["ca", "het", "toan", "bo", "tat"]):
                loc_cmd = f"{action} tat ca"
            if loc_cmd and loc_cmd in COMPOUND:
                matched_cmd = loc_cmd
                log(f"Fuzzy match: '{text_norm}' → {matched_cmd}")

    # Xử lý lệnh hỏi cảm biến
    if matched_cmd in ("hoi nhiet do", "hoi do am", "hoi anh sang"):
        nhiet = sensor_data["nhiet_do"][-1] if sensor_data["nhiet_do"] else "?"
        doam  = sensor_data["do_am"][-1]    if sensor_data["do_am"]    else "?"
        lux   = sensor_data["anh_sang"][-1] if sensor_data["anh_sang"] else "?"
        if matched_cmd == "hoi nhiet do":
            msg = f"Nhiệt độ chuồng lợn hiện tại là {nhiet} độ C"
        elif matched_cmd == "hoi do am":
            msg = f"Độ ẩm hiện tại là {doam} phần trăm"
        else:
            msg = f"Ánh sáng hiện tại là {lux} lux"
        log(f"Sensor query: {msg}")
        speak(msg)
        return

    if matched_cmd:
        if matched_cmd in COMPOUND:
            cmds, msg = COMPOUND[matched_cmd]
            for c in cmds:
                send_to_esp32(c)
                time.sleep(0.15)
            log(f"Phan hoi: {msg}")
            speak(msg)
        else:
            response = send_to_esp32(matched_cmd)
            speak(response)
    else:
        log(f"Khong hieu lenh: '{original}'")

def listen_loop():
    """Vòng lặp lắng nghe microphone liên tục, tự khởi động lại nếu lỗi."""
    global is_listening

    if not os.path.exists(MODEL_PATH):
        log(f"KHONG TIM THAY MODEL tai '{MODEL_PATH}'!")
        log("Vao https://alphacephei.com/vosk/models tai vosk-model-small-vn")
        log("Giai nen vao thu muc 'model/'")
        speak("Khong tim thay mo hinh giong noi. Vui long cai dat model.")
        return

    log("Dang tai model giong noi...")
    # Grammar: giới hạn Vosk chỉ nhận từ vựng lệnh → độ chính xác cao hơn nhiều
    GRAMMAR = json.dumps([
        "bật đèn vườn rau", "tắt đèn vườn rau",
        "bật vườn rau", "tắt vườn rau",
        "bật đèn chuồng lợn", "tắt đèn chuồng lợn",
        "bật chuồng lợn", "tắt chuồng lợn",
        "bật đèn nhà vệ sinh", "tắt đèn nhà vệ sinh",
        "bật nhà vệ sinh", "tắt nhà vệ sinh",
        "bật quạt chuồng", "tắt quạt chuồng",
        "bật quạt", "tắt quạt",
        "bật tất cả", "tắt tất cả",
        "bật toàn bộ", "tắt toàn bộ",
        "bật hết", "tắt hết",
        "trạng thái", "kiểm tra",
        "[unk]"
    ])
    try:
        model = Model(MODEL_PATH)
        rec   = KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)
    except Exception as e:
        log(f"Loi tai model: {e}")
        import traceback; log(traceback.format_exc())
        return

    log("San sang! Hay noi lenh...")
    try:
        speak("He thong san sang, hay noi lenh")
    except Exception as e:
        log(f"Loi TTS khoi dong: {e}")

    while True:
        try:
            q = queue.Queue()
            rec.Reset()

            def audio_callback(indata, frames, time_info, status):
                global is_speaking
                if status:
                    log(f"Audio status: {status}")
                amplitude = np.abs(np.frombuffer(bytes(indata), dtype=np.int16)).mean()
                is_speaking = bool(amplitude > 300)
                q.put(bytes(indata))

            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=16000,
                dtype='int16',
                channels=1,
                callback=audio_callback
            ):
                is_listening = True
                log("Microphone dang hoat dong. Noi de ra lenh.")
                ptt_was_active = False
                while True:
                    data = q.get()
                    # Reset khi PTT bắt đầu để xóa audio cũ trước khi nhấn
                    if ptt_active and not ptt_was_active:
                        rec.Reset()
                    ptt_was_active = ptt_active
                    # Luôn feed audio vào Vosk để stream liên tục (đảm bảo chất lượng)
                    if rec.AcceptWaveform(data):
                        result = json.loads(rec.Result())
                        text   = result.get("text", "").strip()
                        if text:
                            # Log raw để debug (kể cả khi không PTT)
                            log(f"[RAW] '{text}' | PTT={ptt_active} grace={max(0, ptt_grace_until - time.time()):.1f}s")
                        # Chỉ xử lý lệnh khi PTT đang hoặc vừa được nhấn
                        if text and (ptt_active or time.time() < ptt_grace_until):
                            process_voice_command(text)
        except Exception as e:
            is_listening = False
            import traceback
            log(f"Loi microphone: {e}. Thu lai sau 3 giay...")
            log(traceback.format_exc())
            time.sleep(3)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trang Trại Thông Minh</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    :root {
      --bg:      #0a0e1a;
      --panel:   #111827;
      --border:  #1f2937;
      --text:    #f1f5f9;
      --muted:   #64748b;
      --green:   #22c55e;
      --red:     #ef4444;
      --orange:  #f97316;
      --blue:    #3b82f6;
      --yellow:  #eab308;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

    /* HEADER */
    .hdr {
      background: linear-gradient(90deg, #0d1f1a 0%, #0a0e1a 60%, #0d1a2e 100%);
      border-bottom: 1px solid #1a2a1a;
      padding: 0 28px;
      display: flex; align-items: center; justify-content: space-between; gap: 20px;
      height: 64px;
    }
    .hdr-brand { display: flex; align-items: center; gap: 10px; }
    .hdr-brand span { font-size: 20px; font-weight: 800; color: var(--green); }
    .hdr-brand sub { font-size: 11px; color: var(--muted); margin-left: 4px; }
    .hdr-ip { font-size: 12px; color: var(--muted); }
    .hdr-ip b { color: #60a5fa; }

    /* CLOCK */
    .clock { text-align: right; }
    #clock-t { font-size: 26px; font-weight: 700; color: var(--green); letter-spacing: 3px; font-variant-numeric: tabular-nums; }
    #clock-d { font-size: 11px; color: var(--muted); }

    /* ALERT BANNER */
    #alert-banner {
      display: none;
      background: linear-gradient(90deg, #7f1d1d, #b91c1c);
      border-bottom: 2px solid #ef4444;
      padding: 10px 28px;
      font-size: 14px; font-weight: 700; color: #fff;
      animation: blink-bg 0.8s infinite alternate;
      text-align: center;
    }
    @keyframes blink-bg {
      from { background: linear-gradient(90deg,#7f1d1d,#b91c1c); }
      to   { background: linear-gradient(90deg,#b91c1c,#dc2626); }
    }

    /* GRID */
    .main { padding: 20px 28px; display: flex; flex-direction: column; gap: 18px; }
    .row-3 { display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; }
    .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .row-2b { display: grid; grid-template-columns: 3fr 2fr; gap: 16px; }
    @media(max-width:960px){ .row-3,.row-2,.row-2b { grid-template-columns:1fr; } }

    /* CARD */
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px 20px;
    }
    .card-hd {
      font-size: 11px; font-weight: 600; color: var(--muted);
      text-transform: uppercase; letter-spacing: 1.2px;
      margin-bottom: 14px; display: flex; align-items: center; gap: 7px;
    }
    .card-hd .dot { width:7px;height:7px;border-radius:50%;background:var(--green); }

    /* SENSOR BIG NUMBER */
    .s-val { font-size: 42px; font-weight: 800; line-height: 1; }
    .s-val.warn { color: var(--orange) !important; }
    .s-val.danger { color: var(--red) !important; animation: pulse 0.8s infinite alternate; }
    @keyframes pulse { from { opacity:1; } to { opacity:0.5; } }
    .s-unit { font-size: 16px; color: var(--muted); margin-left: 3px; }
    .s-sub  { font-size: 12px; color: var(--muted); margin-top: 6px; }
    .green { color: var(--green); }
    .blue  { color: var(--blue); }
    .yellow{ color: var(--yellow); }

    /* ZONE CARDS */
    .zones { display: flex; flex-direction: column; gap: 10px; }
    .zone {
      display: flex; align-items: center; gap: 12px;
      background: #0d1117; border: 1px solid var(--border);
      border-radius: 10px; padding: 11px 14px;
      transition: border-color .2s;
    }
    .zone:hover { border-color: #374151; }
    .zone.active { border-color: #166534; background: #052e16; }
    .zone.alert  { border-color: #991b1b; background: #1f0a0a; animation: blink-border 0.6s infinite alternate; }
    @keyframes blink-border { from {border-color:#991b1b;} to {border-color:#dc2626;} }
    .z-icon { font-size: 24px; width: 34px; text-align: center; flex-shrink: 0; }
    .z-info { flex: 1; }
    .z-name { font-size: 14px; font-weight: 600; }
    .z-sub  { font-size: 11px; color: var(--muted); margin-top: 1px; }
    .badge {
      font-size: 10px; font-weight: 700; padding: 3px 9px;
      border-radius: 20px; text-transform: uppercase; flex-shrink: 0;
    }
    .badge.on  { background: #14532d; color: #4ade80; }
    .badge.off { background: #3b0e0e; color: #f87171; }
    .badge.alert { background: #7f1d1d; color: #fca5a5; animation: pulse 0.6s infinite alternate; }
    .zbtn { border: none; padding: 6px 13px; border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; transition: opacity .15s; }
    .zbtn:hover { opacity: 0.8; }
    .zbtn.on  { background: #166534; color: #86efac; }
    .zbtn.off { background: #7f1d1d; color: #fca5a5; }

    /* ALL BUTTONS */
    .btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
    .btn-all {
      border: 1px solid var(--border); background: #1e293b; color: #94a3b8;
      padding: 7px 14px; border-radius: 8px; cursor: pointer;
      font-size: 12px; font-weight: 600; transition: background .15s;
    }
    .btn-all:hover { background: #334155; color: var(--text); }

    /* MIC */
    .mic-box { display: flex; align-items: center; gap: 14px; background: #0d1117; border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; }
    #mic-icon { font-size: 44px; }
    #mic-icon.idle    { filter: grayscale(1) opacity(.3); }
    #mic-icon.hearing { animation: shake .1s infinite alternate; filter: drop-shadow(0 0 12px #ef4444); }
    @keyframes shake { 0%{transform:rotate(-8deg) scale(1.1);} 100%{transform:rotate(8deg) scale(1.3);} }
    #mic-status { font-size: 18px; font-weight: 700; }
    #mic-status.on { color: var(--red); }
    #mic-status.off { color: #374151; }
    #mic-last { font-size: 11px; color: var(--muted); margin-top: 3px; }

    /* HINTS */
    .hints { background: #0d1117; border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; font-size: 12px; color: var(--muted); line-height: 1.9; margin-top: 12px; }
    .hints strong { color: var(--green); }
    .hints .kw { color: #93c5fd; }

    /* LOG */
    .log-box { background: #0d1117; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; height: 200px; overflow-y: auto; font-size: 11px; line-height: 1.8; font-family: 'Courier New',monospace; }
    .log-box div { border-bottom: 1px solid #111827; padding: 1px 0; }
    .log-box div:first-child { color: #86efac; }
  </style>
</head>
<body>

<!-- HEADER -->
<div class="hdr">
  <div class="hdr-brand">
    <span>Trang Trại Thông Minh</span>
    <sub>Smart Farm v2.0</sub>
  </div>
  <div class="hdr-ip">ESP32: <b>{{ esp32_ip }}</b></div>
  <div class="clock">
    <div id="clock-t">--:--:--</div>
    <div id="clock-d">Đang tải...</div>
  </div>
</div>

<!-- CẢNH BÁO NHIỆT ĐỘ -->
<div id="alert-banner">
  ⚠️ CẢNH BÁO: Nhiệt độ chuồng lợn vượt 38°C! Đèn chuồng lợn đang nhấp nháy liên tục.
</div>

<div class="main">

  <!-- ĐIỀU KHIỂN + MIC/LOG -->
  <div class="row-2b">

    <!-- ZONE CONTROL -->
    <div class="card">
      <div class="card-hd"><span class="dot"></span>Điều khiển theo khu vực</div>
      <div class="zones">

        <div class="zone" id="z-vuon">
          <div class="z-icon"></div>
          <div class="z-info">
            <div class="z-name">Vườn Rau</div>
            <div class="z-sub">LED 1 (GPIO 2) – Đèn / Tưới nước</div>
          </div>
          <span class="badge off" id="badge-vuon">TẮT</span>
          <a href="/zone/vuon-rau/on"  style="text-decoration:none"><button class="zbtn on">Bật</button></a>
          <a href="/zone/vuon-rau/off" style="text-decoration:none"><button class="zbtn off">Tắt</button></a>
        </div>

        <div class="zone" id="z-chuong">
          <div class="z-icon"></div>
          <div class="z-info">
            <div class="z-name">Đèn chuồng lợn</div>
            <div class="z-sub">LED 2 (GPIO 4) – Đèn chiếu sáng</div>
          </div>
          <span class="badge off" id="badge-chuong">TẮT</span>
          <a href="/zone/chuong-lon/on"  style="text-decoration:none"><button class="zbtn on">Bật</button></a>
          <a href="/zone/chuong-lon/off" style="text-decoration:none"><button class="zbtn off">Tắt</button></a>
        </div>

        <div class="zone" id="z-quat">
          <div class="z-icon"></div>
          <div class="z-info">
            <div class="z-name">Quạt Thông Gió</div>
            <div class="z-sub">LED 3 (GPIO 5) – Quạt làm mát</div>
          </div>
          <span class="badge off" id="badge-quat">TẮT</span>
          <a href="/zone/quat-chuong/on"  style="text-decoration:none"><button class="zbtn on">Bật</button></a>
          <a href="/zone/quat-chuong/off" style="text-decoration:none"><button class="zbtn off">Tắt</button></a>
        </div>

        <div class="zone" id="z-nha">
          <div class="z-icon"></div>
          <div class="z-info">
            <div class="z-name">Nhà Vệ Sinh</div>
            <div class="z-sub">LED 4 (GPIO 18) – Đèn cửa</div>
          </div>
          <span class="badge off" id="badge-nha">TẮT</span>
          <a href="/zone/nha-ve-sinh/on"  style="text-decoration:none"><button class="zbtn on">Bật</button></a>
          <a href="/zone/nha-ve-sinh/off" style="text-decoration:none"><button class="zbtn off">Tắt</button></a>
        </div>

      </div>
      <div class="btn-row">
        <a href="/zone/tat-ca/on"  style="text-decoration:none"><button class="btn-all">Bật Toàn Bộ</button></a>
        <a href="/zone/tat-ca/off" style="text-decoration:none"><button class="btn-all">Tắt Toàn Bộ</button></a>
      </div>
    </div>

    <!-- MIC + LOG -->
    <div style="display:flex;flex-direction:column;gap:16px">
      <div class="card">
        <div class="card-hd"><span class="dot" style="background:var(--red)"></span>Nhận dạng giọng nói</div>
        <div class="mic-box">
          <span id="mic-icon" class="idle">🎙️</span>
          <div style="flex:1">
            <div id="mic-status" class="off">Chờ nói...</div>
            <div id="mic-last">Chưa nhận được lệnh</div>
          </div>
        </div>
        <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
          <button id="btn-speak"
            onmousedown="startMicHold(event)" onmouseup="stopMicHold(event)" onmouseleave="stopMicHold(event)"
            ontouchstart="startMicHold(event)" ontouchend="stopMicHold(event)"
            style="background:#166534;color:#86efac;border:none;padding:8px 18px;border-radius:8px;
                   font-size:13px;font-weight:700;cursor:pointer;width:100%;user-select:none;-webkit-user-select:none">
             Giữ để nói 
          </button>
        </div>
        <div id="browser-mic-result" style="margin-top:8px;font-size:11px;color:#64748b;min-height:18px"></div>
      </div>
      <div class="card" style="flex:1">
        <div class="card-hd"><span class="dot" style="background:#6366f1"></span>Nhật ký hoạt động</div>
        <div class="log-box" id="log-box">
          {% for msg in logs %}<div>{{ msg }}</div>{% endfor %}
        </div>
      </div>
    </div>

  </div>

  <!-- CẢM BIẾN -->
  <div class="row-3">
    <div class="card">
      <div class="card-hd"><span class="dot" style="background:var(--orange)"></span>Nhiệt độ chuồng lợn</div>
      <div><span class="s-val green" id="s-nhiet">--</span><span class="s-unit">°C</span></div>
      <div class="s-sub" id="s-nhiet-sub">Đang cập nhật...</div>
    </div>
    <div class="card">
      <div class="card-hd"><span class="dot" style="background:var(--blue)"></span>Độ ẩm</div>
      <div><span class="s-val blue" id="s-doam">--</span><span class="s-unit">%</span></div>
      <div class="s-sub">Độ ẩm không khí</div>
    </div>
    <div class="card">
      <div class="card-hd"><span class="dot" style="background:var(--yellow)"></span>Ánh sáng</div>
      <div><span class="s-val yellow" id="s-lux">--</span><span class="s-unit">lux</span></div>
      <div class="s-sub">Cường độ ánh sáng</div>
    </div>
  </div>

  <!-- BIỂU ĐỒ -->
  <div class="row-2">
    <div class="card">
      <div class="card-hd"><span class="dot" style="background:var(--orange)"></span>Biểu đồ nhiệt độ</div>
      <canvas id="ch-nhiet" height="110"></canvas>
    </div>
    <div class="card">
      <div class="card-hd"><span class="dot" style="background:var(--blue)"></span>Biểu đồ độ ẩm</div>
      <canvas id="ch-doam" height="110"></canvas>
    </div>
  </div>

</div><!-- main -->

<script>
// ---- ĐỒNG HỒ ----
const THU = ['Chủ nhật','Thứ hai','Thứ ba','Thứ tư','Thứ năm','Thứ sáu','Thứ bảy'];
function tick() {
  const n=new Date();
  const p=v=>String(v).padStart(2,'0');
  document.getElementById('clock-t').textContent=p(n.getHours())+':'+p(n.getMinutes())+':'+p(n.getSeconds());
  document.getElementById('clock-d').textContent=THU[n.getDay()]+', ngày '+p(n.getDate())+'/'
    +p(n.getMonth()+1)+'/'+n.getFullYear();
}
setInterval(tick,1000); tick();

// ---- CHARTS ----
function mkChart(id,label,color){
  return new Chart(document.getElementById(id).getContext('2d'),{
    type:'line',
    data:{labels:[],datasets:[{label,data:[],borderColor:color,backgroundColor:color+'20',
      borderWidth:2,pointRadius:2,tension:0.4,fill:true}]},
    options:{animation:false,responsive:true,
      plugins:{legend:{labels:{color:'#64748b',font:{size:11}}}},
      scales:{x:{ticks:{color:'#64748b',font:{size:9},maxTicksLimit:6},grid:{color:'#1f2937'}},
              y:{ticks:{color:'#64748b',font:{size:10}},grid:{color:'#1f2937'}}}}
  });
}
const chN=mkChart('ch-nhiet','Nhiệt độ (°C)','#f97316');
const chD=mkChart('ch-doam','Độ ẩm (%)','#3b82f6');

// ---- SENSORS ----
async function updSensors(){
  try{
    const r=await fetch('/api/sensors'); const d=await r.json();
    const last=d.nhiet_do.length-1;
    const nhiet=d.nhiet_do[last];
    const el=document.getElementById('s-nhiet');
    el.textContent=nhiet;
    el.className='s-val ' + (nhiet>=39?'danger': nhiet>=38?'warn':'green');
    document.getElementById('s-nhiet-sub').textContent=
      nhiet>=39?'NGUY HIỂM!': nhiet>=38?'Hơi nóng':'Bình thường';
    document.getElementById('s-doam').textContent=d.do_am[last];
    document.getElementById('s-lux').textContent=d.anh_sang[last];
    // alert banner
    const banner=document.getElementById('alert-banner');
    banner.style.display=d.alert_chuong_lon?'block':'none';
    // zone badge chuong khi alert
    const zc=document.getElementById('z-chuong');
    const bc=document.getElementById('badge-chuong');
    if(d.alert_chuong_lon){zc.classList.add('alert');bc.className='badge alert';bc.textContent='CẢNH BÁO';}
    else{zc.classList.remove('alert');}
    // charts
    chN.data.labels=d.nhan; chN.data.datasets[0].data=d.nhiet_do; chN.update('none');
    chD.data.labels=d.nhan; chD.data.datasets[0].data=d.do_am;    chD.update('none');
  }catch(e){}
}

// ---- STATUS ----
async function updStatus(){
  try{
    const r=await fetch('/api/status'); const d=await r.json();
    // vườn rau = tuoi (LED1)
    setZone('z-vuon','badge-vuon', d.tuoi, 'BẬT','TẮT');
    // chuồng lợn = den only (LED2)
    if(!document.getElementById('badge-chuong').textContent.includes('CẢNH'))
      setZone('z-chuong','badge-chuong', d.den,'BẬT','TẮT');
    // quạt thông gió = quat (LED3)
    setZone('z-quat','badge-quat', d.quat,'BẬT','TẮT');
    // nhà vệ sinh = cua (LED4)
    setZone('z-nha','badge-nha', d.cua,'BẬT','TẮT');
  }catch(e){}
}
function setZone(zid,bid,on,ton,toff){
  const z=document.getElementById(zid);
  const b=document.getElementById(bid);
  if(on){z.classList.add('active');}else{z.classList.remove('active');}
  b.className='badge '+(on?'on':'off');
  b.textContent=on?ton:toff;
}

// ---- MIC ----
async function updMic(){
  try{
    const r=await fetch('/api/mic'); const d=await r.json();
    const icon=document.getElementById('mic-icon');
    const st=document.getElementById('mic-status');
    const last=document.getElementById('mic-last');
    if(d.speaking){icon.className='hearing';st.className='on';st.textContent='🔴 ĐANG NGHE...';}
    else if(d.listening){icon.className='idle';st.className='off';st.textContent='🟢 Sẵn sàng...';}
    else{icon.className='idle';st.className='off';st.textContent='⚫ Mic tắt';}
    if(d.last_heard) last.textContent='Nghe được: "'+d.last_heard+'"';
  }catch(e){}
}

// ---- LOG ----
async function updLog(){
  try{
    const r=await fetch('/api/logs'); const d=await r.json();
    document.getElementById('log-box').innerHTML=d.logs.map(m=>'<div>'+m+'</div>').join('');
  }catch(e){}
}

updSensors(); updStatus(); updMic(); updLog();
setInterval(updMic,   300);
setInterval(updLog,  2000);
setInterval(updStatus,4000);
setInterval(updSensors,1000);

// ---- BROWSER MIC – Web Speech API (Google, hỗ trợ tiếng Việt tốt) ----
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let pttHeld = false;

function startMicHold(e) {
  e.preventDefault();
  if (pttHeld) return;
  pttHeld = true;
  const btn = document.getElementById('btn-speak');
  const res = document.getElementById('browser-mic-result');
  btn.textContent = '🔴 Đang nghe… (thả để dừng)';
  btn.style.background = '#7f1d1d';
  res.textContent = '🎙️ Hãy nói lệnh...';

  if (!SpeechRec) {
    res.textContent = '❌ Dùng Chrome hoặc Edge để nhận diện giọng nói';
    pttHeld = false; resetMicBtn(); return;
  }
  recognition = new SpeechRec();
  recognition.lang = 'vi-VN';
  recognition.interimResults = true;
  recognition.maxAlternatives = 3;
  recognition.continuous = false;

  recognition.onresult = (event) => {
    let interim = '', final_t = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const t = event.results[i][0].transcript;
      if (event.results[i].isFinal) final_t += t; else interim += t;
    }
    const display = final_t || interim;
    res.textContent = display ? '🎤 "' + display + '"' : '🎙️ Đang nhận diện...';
    if (final_t) sendVoiceCommand(final_t.trim());
  };

  recognition.onerror = (evt) => {
    if (evt.error === 'aborted') { resetMicBtn(); pttHeld = false; return; }
    res.textContent = '⚠️ Lỗi: ' + evt.error + ' (cần internet & quyền micro)';
    resetMicBtn(); pttHeld = false;
  };

  recognition.onend = () => { resetMicBtn(); pttHeld = false; };

  try { recognition.start(); } catch(ex) { resetMicBtn(); pttHeld = false; }
}

function stopMicHold(e) {
  if (!pttHeld) return;
  if (recognition) { try { recognition.stop(); } catch(ex) {} recognition = null; }
  pttHeld = false; resetMicBtn();
  setTimeout(() => { updLog(); updStatus(); }, 800);
}

function sendVoiceCommand(text) {
  document.getElementById('browser-mic-result').textContent = 'Ghi nhận: "' + text + '"';
  fetch('/api/voice_command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: text})
  }).then(r => r.json()).then(() => {
    setTimeout(() => { updLog(); updStatus(); }, 600);
  }).catch(() => {});
}

function resetMicBtn() {
  const btn = document.getElementById('btn-speak');
  btn.textContent = 'Giữ để nói lệnh';
  btn.style.background = '#166534';
}
</script>
</body>
</html>
"""

@app.route("/api/sensors")
def api_sensors():
    data = dict(sensor_data)
    data["alert_chuong_lon"] = alert_chuong_lon
    return jsonify(data)


@app.route("/zone/<zone>/<action>")
def zone_cmd(zone, action):
    from flask import redirect
    ZONE_MAP = {
        ("vuon-rau",   "on"):  ["bat tuoi"],
        ("vuon-rau",   "off"): ["tat tuoi"],
        ("chuong-lon", "on"):  ["bat den"],
        ("chuong-lon", "off"): ["tat den"],
        ("quat-chuong", "on"):  ["bat quat"],
        ("quat-chuong", "off"): ["tat quat"],
        ("nha-ve-sinh","on"):  ["mo cua"],
        ("nha-ve-sinh","off"): ["dong cua"],
        ("tat-ca",     "on"):  ["bat tat ca"],
        ("tat-ca",     "off"): ["tat tat ca"],
    }
    cmds = ZONE_MAP.get((zone, action), [])
    for c in cmds:
        log(f"Zone {zone}/{action}: {c}")
        send_to_esp32(c)
        time.sleep(0.1)
    return redirect("/")

@app.route("/api/ptt/on")
def ptt_on():
    global ptt_active, ptt_grace_until
    ptt_active = True
    ptt_grace_until = 0
    log("PTT: Bat dau nghe...")
    return jsonify({"ok": True})

@app.route("/api/ptt/off")
def ptt_off():
    global ptt_active, ptt_grace_until
    ptt_active = False
    ptt_grace_until = time.time() + 2.5  
    return jsonify({"ok": True})

@app.route("/api/mic")
def api_mic():
    return jsonify({
        "listening": is_listening,
        "speaking":  is_speaking,
        "last_heard": log_messages[-1] if log_messages else ""
    })

@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": list(reversed(log_messages))})

@app.route("/")
def index():
    from flask import render_template_string
    return render_template_string(DASHBOARD_HTML,
        esp32_ip=ESP32_IP,
        listening=is_listening,
        logs=list(reversed(log_messages))
    )

@app.route("/send/<cmd>")
def send_cmd(cmd):
    from flask import redirect
    cmd = cmd.replace("+", " ")
    log(f"Lenh tu web: {cmd}")
    process_voice_command(cmd)
    return redirect("/")

@app.route("/api/voice_command", methods=["POST"])
def api_voice_command():
    from flask import request
    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text", "")).strip()
    if text:
        log(f"[Browser STT] '{text}'")
        process_voice_command(text)
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    try:
        url = f"http://{ESP32_IP}:{ESP32_PORT}/status"
        r   = requests.get(url, timeout=3)
        return jsonify(r.json())
    except:
        return jsonify({"error": "Khong ket noi duoc ESP32"})

if __name__ == "__main__":
    log(f"ESP32 IP: {ESP32_IP}")
    log("Khoi dong web dashboard tai http://localhost:5000")

    voice_thread = threading.Thread(target=listen_loop, daemon=True)
    voice_thread.start()

    sensor_thread = threading.Thread(target=sensor_loop, daemon=True)
    sensor_thread.start()

    time.sleep(1)
    webbrowser.open("http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
