import os
import uuid
import math
import json
import random
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB

UPLOAD_DIR = Path(tempfile.gettempdir()) / "vshuf_uploads"
OUTPUT_DIR = Path(tempfile.gettempdir()) / "vshuf_outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# job_id -> { status, progress, message, output_path, error }
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def ff(name):
    p = shutil.which(name)
    return p or name


FFMPEG  = ff("ffmpeg")
FFPROBE = ff("ffprobe")


def get_duration(path: str):
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace"))
    return float(json.loads(r.stdout)["format"]["duration"])


# ── Processing ────────────────────────────────────────────────────────────────

def process_video(job_id, src, seg_len, fps, width, height):
    def upd(status=None, progress=None, message=None, error=None, output=None):
        with JOBS_LOCK:
            j = JOBS[job_id]
            if status:   j["status"]   = status
            if progress is not None: j["progress"] = progress
            if message:  j["message"]  = message
            if error:    j["error"]    = error
            if output:   j["output"]   = output

    tmpdir = Path(tempfile.mkdtemp(prefix=f"vshuf_{job_id}_"))
    try:
        # 1. duration
        upd(status="running", progress=2, message="Анализ видео…")
        dur = get_duration(src)

        # 2. segments
        n = max(1, int(math.floor(dur / seg_len)))
        starts = [i * seg_len for i in range(n)]
        shuffled = starts[:]
        if len(shuffled) > 1:
            for _ in range(10000):
                random.shuffle(shuffled)
                if all(shuffled[i] != starts[i] for i in range(n)):
                    break

        upd(progress=5, message=f"Нарезка {n} сегментов…")
        clip_paths = []

        for idx, t in enumerate(shuffled):
            with JOBS_LOCK:
                if JOBS[job_id].get("cancelled"):
                    raise InterruptedError("cancelled")

            pct = 5 + int((idx / n) * 75)
            upd(progress=pct, message=f"Сегмент {idx+1}/{n}")

            clip = str(tmpdir / f"clip_{idx:04d}.mp4")
            clip_paths.append(clip)

            r = subprocess.run([
                FFMPEG, "-y",
                "-ss", str(t), "-i", src,
                "-t", str(seg_len),
                "-vf", (f"scale={width}:{height}:"
                        f"force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                        f"fps={fps}"),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-avoid_negative_ts", "1", clip
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            if r.returncode != 0:
                raise RuntimeError(r.stderr.decode(errors="replace")[-400:])

        # 3. concat
        upd(progress=82, message="Склейка…")
        list_file = str(tmpdir / "list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for cp in clip_paths:
                f.write(f"file '{cp}'\n")

        out_path = str(OUTPUT_DIR / f"{job_id}.mp4")
        r2 = subprocess.run([
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", list_file, "-c", "copy", out_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        if r2.returncode != 0:
            raise RuntimeError(r2.stderr.decode(errors="replace")[-400:])

        upd(status="done", progress=100,
            message=f"Готово! {n} сегментов × {seg_len}с",
            output=out_path)

    except InterruptedError:
        upd(status="cancelled", message="Отменено")
    except Exception as e:
        upd(status="error", error=str(e), message="Ошибка обработки")
    finally:
        shutil.rmtree(str(tmpdir), ignore_errors=True)
        # clean up upload
        try:
            os.remove(src)
        except Exception:
            pass


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/upload", methods=["POST"])
def upload():
    if "video" not in request.files:
        return jsonify(error="Нет файла"), 400

    f = request.files["video"]
    if not f.filename:
        return jsonify(error="Пустое имя файла"), 400

    ext  = Path(secure_filename(f.filename)).suffix.lower()
    allowed = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v", ".flv"}
    if ext not in allowed:
        return jsonify(error=f"Формат {ext} не поддерживается"), 400

    job_id   = uuid.uuid4().hex
    src_path = str(UPLOAD_DIR / f"{job_id}{ext}")
    f.save(src_path)

    seg_len = float(request.form.get("seg_len", 5))
    fps     = request.form.get("fps", "30")
    res     = request.form.get("res", "1920x1080")
    w, h    = res.split("x")

    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 0,
                        "message": "В очереди…", "error": None,
                        "output": None, "cancelled": False}

    t = threading.Thread(
        target=process_video,
        args=(job_id, src_path, seg_len, fps, w, h),
        daemon=True
    )
    t.start()
    return jsonify(job_id=job_id)


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j:
        return jsonify(error="Задача не найдена"), 404
    return jsonify(j)


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["cancelled"] = True
    return jsonify(ok=True)


@app.route("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j or j["status"] != "done":
        return jsonify(error="Файл не готов"), 404
    out = j["output"]
    if not out or not os.path.isfile(out):
        return jsonify(error="Файл не найден на сервере"), 404
    return send_file(out, as_attachment=True,
                     download_name="shuffled.mp4",
                     mimetype="video/mp4")


# ── HTML (single-file frontend) ───────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Shuffler</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;600&display=swap');

  :root {
    --bg:      #0a0a0a;
    --panel:   #131313;
    --border:  #222;
    --accent:  #c8ff47;
    --accent2: #47c8ff;
    --fg:      #f0f0f0;
    --muted:   #555;
    --danger:  #ff4747;
    --success: #47ff8e;
    --r:       12px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--fg);
    font-family: 'DM Sans', sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 32px 16px 64px;
  }

  /* ── Header ── */
  .header {
    width: 100%; max-width: 520px;
    margin-bottom: 36px;
  }
  .logo {
    font-family: 'Space Mono', monospace;
    font-size: clamp(28px, 6vw, 42px);
    font-weight: 700;
    letter-spacing: -1px;
    line-height: 1;
  }
  .logo span { color: var(--accent); }
  .tagline {
    font-family: 'Space Mono', monospace;
    font-size: 11px;
    color: var(--muted);
    margin-top: 6px;
    letter-spacing: 2px;
    text-transform: uppercase;
  }

  /* ── Card ── */
  .card {
    width: 100%; max-width: 520px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--r);
    padding: 28px;
    display: flex;
    flex-direction: column;
    gap: 22px;
  }

  /* ── Drop zone ── */
  .drop-zone {
    border: 2px dashed var(--border);
    border-radius: 10px;
    padding: 40px 20px;
    text-align: center;
    cursor: pointer;
    transition: border-color .2s, background .2s;
    position: relative;
    overflow: hidden;
  }
  .drop-zone:hover, .drop-zone.over {
    border-color: var(--accent);
    background: rgba(200,255,71,.04);
  }
  .drop-zone input[type=file] {
    position: absolute; inset: 0; opacity: 0; cursor: pointer;
  }
  .drop-icon { font-size: 36px; margin-bottom: 10px; }
  .drop-title {
    font-family: 'Space Mono', monospace;
    font-size: 13px; font-weight: 700;
    color: var(--fg);
  }
  .drop-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .file-chosen {
    font-family: 'Space Mono', monospace;
    font-size: 11px;
    color: var(--accent);
    margin-top: 8px;
    word-break: break-all;
  }

  /* ── Settings grid ── */
  .settings {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 12px;
  }
  .field label {
    display: block;
    font-family: 'Space Mono', monospace;
    font-size: 9px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .field select, .field input[type=number] {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--accent);
    font-family: 'Space Mono', monospace;
    font-size: 13px;
    font-weight: 700;
    padding: 9px 10px;
    outline: none;
    -webkit-appearance: none;
    appearance: none;
    transition: border-color .2s;
  }
  .field select:focus, .field input:focus { border-color: var(--accent); }

  /* ── Buttons ── */
  .btn {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    width: 100%; padding: 16px;
    border: none; border-radius: 10px;
    font-family: 'Space Mono', monospace;
    font-size: 14px; font-weight: 700;
    cursor: pointer;
    transition: opacity .15s, transform .1s;
  }
  .btn:active { transform: scale(.98); }
  .btn-primary { background: var(--accent); color: #0a0a0a; }
  .btn-primary:disabled { opacity: .35; cursor: not-allowed; }
  .btn-danger  { background: var(--danger); color: #fff; }
  .btn-dl      { background: var(--success); color: #0a0a0a; }

  /* ── Progress ── */
  .progress-wrap { display: none; flex-direction: column; gap: 10px; }
  .progress-wrap.visible { display: flex; }
  .progress-labels {
    display: flex; justify-content: space-between;
    font-family: 'Space Mono', monospace; font-size: 11px;
  }
  .progress-msg  { color: var(--muted); }
  .progress-pct  { color: var(--accent); font-weight: 700; }
  .progress-track {
    background: var(--border); border-radius: 4px; height: 6px; overflow: hidden;
  }
  .progress-bar {
    height: 100%; background: var(--accent);
    border-radius: 4px;
    transition: width .4s ease;
    width: 0%;
  }

  /* ── Result ── */
  .result { display: none; flex-direction: column; gap: 12px; }
  .result.visible { display: flex; }
  .result-info {
    font-family: 'Space Mono', monospace;
    font-size: 11px; color: var(--success);
    text-align: center; letter-spacing: .5px;
  }

  /* ── Error ── */
  .err-box {
    display: none;
    background: rgba(255,71,71,.1);
    border: 1px solid var(--danger);
    border-radius: 8px; padding: 12px 16px;
    font-family: 'Space Mono', monospace;
    font-size: 11px; color: var(--danger);
    white-space: pre-wrap; word-break: break-word;
  }
  .err-box.visible { display: block; }

  /* ── Footer ── */
  .footer {
    margin-top: 40px;
    font-family: 'Space Mono', monospace;
    font-size: 10px; color: var(--muted);
    text-align: center; letter-spacing: 1px;
  }

  @media (max-width: 400px) {
    .settings { grid-template-columns: 1fr 1fr; }
    .card { padding: 20px; }
  }
</style>
</head>
<body>

<div class="header">
  <div class="logo"><span>VIDEO</span> SHUFFLER</div>
  <div class="tagline">// random cut engine · web edition</div>
</div>

<div class="card">

  <!-- Drop zone -->
  <div class="drop-zone" id="dropZone">
    <input type="file" id="fileInput" accept="video/*,.mp4,.mov,.avi,.mkv,.wmv,.webm,.m4v">
    <div class="drop-icon">🎬</div>
    <div class="drop-title">Выбери или перетащи видео</div>
    <div class="drop-sub">MP4 · MOV · AVI · MKV · до 2 ГБ</div>
    <div class="file-chosen" id="fileChosen"></div>
  </div>

  <!-- Settings -->
  <div class="settings">
    <div class="field">
      <label>Длина (сек)</label>
      <input type="number" id="segLen" value="5" min="1" max="60" step="0.5">
    </div>
    <div class="field">
      <label>FPS</label>
      <select id="fps">
        <option>24</option>
        <option>25</option>
        <option selected>30</option>
        <option>50</option>
        <option>60</option>
      </select>
    </div>
    <div class="field">
      <label>Разрешение</label>
      <select id="res">
        <option>1280x720</option>
        <option selected>1920x1080</option>
        <option>3840x2160</option>
      </select>
    </div>
  </div>

  <!-- Run button -->
  <button class="btn btn-primary" id="runBtn" disabled onclick="startJob()">
    ▶ ЗАПУСТИТЬ
  </button>

  <!-- Progress -->
  <div class="progress-wrap" id="progressWrap">
    <div class="progress-labels">
      <span class="progress-msg" id="progressMsg">Подготовка…</span>
      <span class="progress-pct" id="progressPct">0%</span>
    </div>
    <div class="progress-track">
      <div class="progress-bar" id="progressBar"></div>
    </div>
    <button class="btn btn-danger" onclick="cancelJob()">✕ ОТМЕНИТЬ</button>
  </div>

  <!-- Error -->
  <div class="err-box" id="errBox"></div>

  <!-- Result -->
  <div class="result" id="resultWrap">
    <div class="result-info" id="resultInfo"></div>
    <a class="btn btn-dl" id="dlBtn" href="#">⬇ СКАЧАТЬ ВИДЕО</a>
    <button class="btn btn-primary" onclick="resetUI()" style="opacity:.6">
      ↩ Новое видео
    </button>
  </div>

</div>

<div class="footer">POWERED BY FFMPEG · 1080P · H.264 · AAC</div>

<script>
let currentJobId = null;
let pollTimer    = null;

const $  = id => document.getElementById(id);
const dz = $('dropZone');

// drag-and-drop
dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('over'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('over');
  const f = e.dataTransfer.files[0];
  if (f) setFile(f);
});
$('fileInput').addEventListener('change', e => {
  if (e.target.files[0]) setFile(e.target.files[0]);
});

function setFile(f) {
  $('fileInput')._file = f;
  $('fileChosen').textContent = `📎 ${f.name} (${(f.size/1024/1024).toFixed(1)} МБ)`;
  $('runBtn').disabled = false;
  hideError(); hideResult(); hideProgress();
}

async function startJob() {
  const f = $('fileInput')._file;
  if (!f) return;

  hideError(); hideResult(); hideProgress();
  $('runBtn').disabled = true;

  const fd = new FormData();
  fd.append('video',   f);
  fd.append('seg_len', $('segLen').value);
  fd.append('fps',     $('fps').value);
  fd.append('res',     $('res').value);

  showProgress('Загрузка файла…', 1);

  try {
    const r   = await fetch('/upload', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) { showError(data.error || 'Ошибка загрузки'); return; }
    currentJobId = data.job_id;
    pollStatus();
  } catch(e) {
    showError('Ошибка соединения: ' + e.message);
    $('runBtn').disabled = false;
  }
}

function pollStatus() {
  if (!currentJobId) return;
  pollTimer = setTimeout(async () => {
    try {
      const r = await fetch('/status/' + currentJobId);
      const j = await r.json();
      if (j.error && j.status !== 'done') { showError(j.error); return; }

      showProgress(j.message || '…', j.progress || 0);

      if (j.status === 'done') {
        hideProgress();
        showResult(j.message, currentJobId);
      } else if (j.status === 'error') {
        hideProgress();
        showError(j.error || 'Неизвестная ошибка');
        $('runBtn').disabled = false;
      } else if (j.status === 'cancelled') {
        hideProgress();
        $('runBtn').disabled = false;
      } else {
        pollStatus();
      }
    } catch(e) {
      pollStatus(); // retry on network hiccup
    }
  }, 800);
}

async function cancelJob() {
  if (currentJobId) {
    await fetch('/cancel/' + currentJobId, { method: 'POST' });
  }
  hideProgress();
  $('runBtn').disabled = false;
}

function showProgress(msg, pct) {
  $('progressWrap').classList.add('visible');
  $('progressMsg').textContent  = msg;
  $('progressPct').textContent  = pct + '%';
  $('progressBar').style.width  = pct + '%';
}
function hideProgress() { $('progressWrap').classList.remove('visible'); }

function showResult(msg, jobId) {
  $('resultWrap').classList.add('visible');
  $('resultInfo').textContent = '✓ ' + msg;
  $('dlBtn').href = '/download/' + jobId;
}
function hideResult() { $('resultWrap').classList.remove('visible'); }

function showError(msg) {
  $('errBox').textContent = '⚠ ' + msg;
  $('errBox').classList.add('visible');
}
function hideError() { $('errBox').classList.remove('visible'); }

function resetUI() {
  currentJobId = null;
  if (pollTimer) clearTimeout(pollTimer);
  $('fileInput')._file = null;
  $('fileInput').value = '';
  $('fileChosen').textContent = '';
  $('runBtn').disabled = true;
  hideProgress(); hideResult(); hideError();
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
