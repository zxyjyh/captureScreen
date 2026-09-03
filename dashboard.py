"""拾遗的看板"""

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


@app.route("/api/storage")
def api_storage():
    """按日期列出占用，图片和文本分开算。

    分开是有意义的：图片占 99.9%，文本占 0.1%。
    看板上把这两个数并排放，删的时候才知道该删哪个 ——
    删图片省下几乎全部空间，而「那天我在干什么」照样答得上来。
    """
    import purge

    shots, reports, db_path = purge._dirs()
    rows = []
    for name in sorted({d.name for base in (shots, reports)
                        if base.exists() for d in base.iterdir() if d.is_dir()}):
        s_dir, r_dir = shots / name, reports / name
        img = txt = count = 0
        for f in (s_dir.rglob("*") if s_dir.exists() else []):
            if not f.is_file():
                continue
            count += 1
            if f.suffix in (".png", ".jpg"):
                img += f.stat().st_size
            else:
                txt += f.stat().st_size
        rows.append({
            "date": name,
            "images": img,
            "text": txt,
            "file_count": count,
            "reports": purge._size(r_dir),
            "has_report": r_dir.exists(),
        })

    backups = [{"name": b.name, "size": purge._size(b)}
               for b in sorted(SCRIPT_DIR.glob(".git.bak-*"))]

    import shutil as _sh
    free = _sh.disk_usage(str(SCRIPT_DIR)).free
    cfg = load_config().get("capture", {})
    return jsonify({
        "rows": rows,
        "total": sum(r["images"] + r["text"] + r["reports"] for r in rows),
        "vectors": purge._size(db_path),
        "backups": backups,
        "disk_free": free,
        "max_disk_mb": cfg.get("max_disk_mb", 0),
        "image_retention_days": cfg.get("image_retention_days", 0),
    })


@app.route("/api/purge", methods=["POST"])
def api_purge():
    """按日期删除。keep_text=true 时只删图片。

    删除不可逆，所以要求前端显式带上日期，不接受「删全部」这种含糊指令。
    """
    import shutil as _sh
    import purge

    data = request.get_json(silent=True) or {}
    dates = data.get("dates") or []
    keep_text = bool(data.get("keep_text"))
    if not dates:
        return jsonify({"error": "没有指定日期"}), 400

    shots, reports, db_path = purge._dirs()
    freed, removed = 0, []
    for date in dates:
        s_dir = shots / date
        if s_dir.exists():
            if keep_text:
                for f in list(s_dir.rglob("*")):
                    if f.is_file() and f.suffix in (".png", ".jpg"):
                        freed += f.stat().st_size
                        f.unlink()
            else:
                freed += purge._size(s_dir)
                _sh.rmtree(s_dir)
        if not keep_text:
            r_dir = reports / date
            if r_dir.exists():
                freed += purge._size(r_dir)
                _sh.rmtree(r_dir)
        removed.append(date)

    vectors = 0 if keep_text else purge.purge_vectors(db_path, dates, dry_run=False)
    if not keep_text:
        purge._forget_index(dates)

    import audit
    audit.record(
        "看板",
        ("只删图片：" if keep_text else "全部删除：") + "、".join(removed),
        len(removed), freed,
    )
    return jsonify({"ok": True, "dates": removed, "freed": freed, "vectors": vectors})


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


def _run(args: list[str], timeout: int = 900):
    """跑一个子进程脚本，把 stdout 也带回来 ——
    analyze.py 把「这个小时没内容」这类原因打在 stdout，只看 stderr 会得到空串。"""
    r = subprocess.run(
        [str(SCRIPT_DIR / "venv/bin/python")] + args,
        capture_output=True, text=True, timeout=timeout, cwd=str(SCRIPT_DIR),
    )
    tail = (r.stdout.strip() or r.stderr.strip() or "").splitlines()
    return r.returncode, (tail[-1][:200] if tail else "")


@app.route("/api/control/analyze", methods=["POST"])
def api_analyze():
    """按需分析某一个小时。

    小时报告不再自动生成 —— 那是每小时一次模型调用，而绝大多数小时
    根本没人会看。要看哪个小时就点哪个。
    """
    data = request.get_json(silent=True) or {}
    now = datetime.now()
    date_str = data.get("date") or now.strftime("%Y-%m-%d")
    hour = data.get("hour")
    if hour is None:
        prev = now - timedelta(hours=1)
        date_str, hour = prev.strftime("%Y-%m-%d"), prev.hour

    code, msg = _run([str(SCRIPT_DIR / "analyze.py"), "--date", date_str, "--hour", str(int(hour))])
    if code != 0:
        return jsonify({"ok": False, "message": msg or "分析失败"}), 500

    made = (SCRIPT_DIR / load_config()["report"]["output_dir"] / date_str / f"{int(hour):02d}.md").exists()
    return jsonify({
        "ok": True,
        "generated": made,
        "message": f"{date_str} {int(hour):02d}:00 分析完成" if made else (msg or "该时段无内容，未生成报告"),
    })


@app.route("/api/control/summarize", methods=["POST"])
def api_summarize():
    """按需生成日 / 周 / 月总结。日总结另有每天一次的自动任务，这里是手动补跑。"""
    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "daily")
    today = datetime.now()

    if kind == "daily":
        date_str = data.get("date") or today.strftime("%Y-%m-%d")
        code, msg = _run([str(SCRIPT_DIR / "summarize.py"), "--date", date_str])
        label = f"{date_str} 日总结"
    elif kind == "weekly":
        end = datetime.strptime(data["end"], "%Y-%m-%d") if data.get("end") else today
        start = end - timedelta(days=6)
        code, msg = _run([str(SCRIPT_DIR / "period_summary.py"), "--weekly",
                          start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")])
        label = f"{start:%m-%d} 至 {end:%m-%d} 周报"
    elif kind == "monthly":
        year = int(data.get("year") or today.year)
        month = int(data.get("month") or today.month)
        code, msg = _run([str(SCRIPT_DIR / "period_summary.py"), "--monthly", str(year), str(month)])
        label = f"{year}-{month:02d} 月报"
    else:
        return jsonify({"ok": False, "message": f"未知类型 {kind}"}), 400

    if code != 0:
        return jsonify({"ok": False, "message": msg or f"{label}生成失败"}), 500
    return jsonify({"ok": True, "message": f"{label}已生成"})


@app.route("/api/pending")
def api_pending():
    """哪些小时有数据但还没分析 —— 看板据此在时间线上标出可点击分析的时段。"""
    import capture as cap
    cfg = load_config()
    shots = SCRIPT_DIR / cfg["capture"]["output_dir"]
    reports = SCRIPT_DIR / cfg["report"]["output_dir"]
    return jsonify({
        "hours": [{"date": d, "hour": h} for d, h in cap.pending_hours(shots, reports, cap=200)],
        "days": cap.pending_days(shots, reports),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5001)
