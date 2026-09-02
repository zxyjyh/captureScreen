"""Web 看板 - 截图采集系统"""

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

SCRIPT_DIR = Path(__file__).resolve().parent

# launchd / cron 启动时不继承 shell 环境，凭证只能从 .env 取
import env_file  # noqa: E402

env_file.load()
ANNOTATIONS_FILE = SCRIPT_DIR / "annotations.json"

STATIC_DIR = str(SCRIPT_DIR / "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")


def load_config():
    import yaml
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def is_capture_running():
    pid_file = SCRIPT_DIR / "capture.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            return True, pid_file.read_text().strip()
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    return False, None


def scan_reports():
    report_dir = SCRIPT_DIR / "reports"
    if not report_dir.exists():
        return []
    reports = []
    for date_dir in sorted(report_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for rf in sorted(date_dir.glob("[0-9][0-9].md")):
            reports.append({
                "date": date_dir.name,
                "hour": rf.stem,
                "type": "hourly",
                "label": f"{date_dir.name} {rf.stem}:00",
                "path": str(rf.relative_to(SCRIPT_DIR)),
            })
        ds = date_dir / "daily-summary.md"
        if ds.exists():
            reports.append({
                "date": date_dir.name,
                "hour": "",
                "type": "daily",
                "label": f"{date_dir.name} 日总结",
                "path": str(ds.relative_to(SCRIPT_DIR)),
            })
    weekly_dir = report_dir / "weekly"
    if weekly_dir.exists():
        for wf in sorted(weekly_dir.glob("*.md"), reverse=True):
            reports.append({
                "date": wf.stem,
                "hour": "",
                "type": "weekly",
                "label": f"周报 {wf.stem}",
                "path": str(wf.relative_to(SCRIPT_DIR)),
            })
    monthly_dir = report_dir / "monthly"
    if monthly_dir.exists():
        for mf in sorted(monthly_dir.glob("*.md"), reverse=True):
            reports.append({
                "date": mf.stem,
                "hour": "",
                "type": "monthly",
                "label": f"月报 {mf.stem}",
                "path": str(mf.relative_to(SCRIPT_DIR)),
            })
    return reports


def load_annotations():
    if ANNOTATIONS_FILE.exists():
        return json.loads(ANNOTATIONS_FILE.read_text())
    return {}


def save_annotations(data):
    ANNOTATIONS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# --- API Routes ---

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/status")
def api_status():
    running, pid = is_capture_running()
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = SCRIPT_DIR / "screenshots" / today
    count = 0
    latest = ""
    if today_dir.exists():
        pngs = sorted(list(today_dir.glob("*.png")) + list(today_dir.glob("*.jpg")))
        count = len(pngs)
        if pngs:
            latest = pngs[-1].stem.replace("-", ":")
    return jsonify({
        "running": running,
        "pid": pid,
        "today_count": count,
        "today_latest": latest,
    })


@app.route("/api/reports")
def api_reports():
    report_type = request.args.get("type")
    reports = scan_reports()
    if report_type:
        reports = [r for r in reports if r["type"] == report_type]
    return jsonify(reports)


@app.route("/api/report-content")
def api_report_content():
    path = request.args.get("path", "")
    full_path = SCRIPT_DIR / path
    if full_path.exists() and full_path.is_file():
        return jsonify({"content": full_path.read_text()})
    return jsonify({"content": "报告不存在"}), 404


@app.route("/api/screenshots")
def api_screenshots():
    date = request.args.get("date", "")
    hour = request.args.get("hour", "")
    date_dir = SCRIPT_DIR / "screenshots" / date
    if not date_dir.exists():
        return jsonify([])
    pngs = sorted(list(date_dir.glob("*.png")) + list(date_dir.glob("*.jpg")))
    if hour:
        pngs = [p for p in pngs if p.name.startswith(f"{hour}-")]
    result = []
    for p in pngs:
        meta_file = p.with_suffix(".meta")
        app_name = ""
        title = ""
        if meta_file.exists():
            lines = meta_file.read_text().strip().splitlines()
            app_name = lines[0] if lines else ""
            title = lines[1] if len(lines) > 1 else ""
        result.append({
            "filename": p.name,
            "time": p.stem.replace("-", ":"),
            "app": app_name,
            "title": title,
            "url": f"/api/screenshot-image?date={date}&file={p.name}",
        })
    return jsonify(result)


@app.route("/api/screenshot-image")
def api_screenshot_image():
    date = request.args.get("date", "")
    filename = request.args.get("file", "")
    full_path = SCRIPT_DIR / "screenshots" / date / filename
    if full_path.exists() and full_path.suffix in (".png", ".jpg"):
        return send_from_directory(str(full_path.parent), filename, mimetype="image/png")
    return "Not found", 404
    return jsonify({"content": "报告不存在"}), 404


@app.route("/api/annotations")
def api_annotations():
    return jsonify(load_annotations())


@app.route("/api/annotations", methods=["POST"])
def api_save_annotation():
    data = request.json
    date = data.get("date", "")
    hour = data.get("hour", "")
    annotation = data.get("annotation", {})
    annotations = load_annotations()
    if date not in annotations:
        annotations[date] = {}
    annotations[date][hour] = annotation
    save_annotations(annotations)
    return jsonify({"ok": True})


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").lower()
    report_type = request.args.get("type")
    reports = scan_reports()
    if report_type:
        reports = [r for r in reports if r["type"] == report_type]
    if not query:
        return jsonify(reports)
    results = []
    for r in reports:
        full_path = SCRIPT_DIR / r["path"]
        if full_path.exists():
            content = full_path.read_text().lower()
            if query in content or query in r["label"].lower():
                results.append(r)
    return jsonify(results)


@app.route("/api/control/start", methods=["POST"])
def api_start():
    subprocess.Popen(
        [str(SCRIPT_DIR / "venv/bin/python"), str(SCRIPT_DIR / "capture.py")],
        start_new_session=True,
    )
    return jsonify({"ok": True})


@app.route("/api/control/stop", methods=["POST"])
def api_stop():
    pid_file = SCRIPT_DIR / "capture.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 15)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    return jsonify({"ok": True})


@app.route("/api/control/analyze", methods=["POST"])
def api_analyze():
    now = datetime.now()
    prev = now - timedelta(hours=1)
    result = subprocess.run(
        [str(SCRIPT_DIR / "venv/bin/python"), str(SCRIPT_DIR / "analyze.py"),
         "--date", prev.strftime("%Y-%m-%d"), "--hour", str(prev.hour)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode == 0:
        return jsonify({"ok": True, "message": f"分析完成: {prev.strftime('%Y-%m-%d %H')}:00"})
    else:
        return jsonify({"ok": False, "message": result.stderr[-200:] if result.stderr else "分析失败"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
