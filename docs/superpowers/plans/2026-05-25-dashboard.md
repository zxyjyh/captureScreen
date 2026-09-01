# Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a three-column Web dashboard (Flask + vanilla JS) for the screen capture system with report browsing, annotation, process control, and search.

**Architecture:** Flask serves a single-page app with three columns. Backend provides JSON API endpoints. Frontend is vanilla HTML/CSS/JS with no build step. All data flows through REST endpoints.

**Tech Stack:** Python 3.14, Flask, vanilla HTML/CSS/JS

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `dashboard.py` | Rewrite | Flask app with API endpoints + serving static files |
| `static/index.html` | Create | Single-page HTML with three-column layout |
| `static/style.css` | Create | All styles |
| `static/app.js` | Create | Frontend logic: menu, timeline, content, annotations, search, controls |
| `annotations.json` | Created at runtime | Annotation data |

---

### Task 1: Flask backend with API endpoints

**Files:**
- Rewrite: `dashboard.py`

- [ ] **Step 1: Install Flask**

Run: `./venv/bin/pip install flask && echo "flask" >> requirements.txt`

- [ ] **Step 2: Write Flask backend**

Rewrite `dashboard.py`:

```python
"""Web 看板 - 截图采集系统"""

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

SCRIPT_DIR = Path(__file__).resolve().parent
ANNOTATIONS_FILE = SCRIPT_DIR / "annotations.json"

app = Flask(__name__, static_folder="static", static_url_path="")


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
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def api_status():
    running, pid = is_capture_running()
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = SCRIPT_DIR / "screenshots" / today
    count = 0
    latest = ""
    if today_dir.exists():
        pngs = sorted(today_dir.glob("*.png"))
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
    subprocess.run(
        [str(SCRIPT_DIR / "venv/bin/python"), str(SCRIPT_DIR / "analyze.py"),
         "--date", prev.strftime("%Y-%m-%d"), "--hour", str(prev.hour)],
        capture_output=True,
    )
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
```

- [ ] **Step 3: Verify Flask starts**

Run: `./venv/bin/python dashboard.py`
Expected: Flask server starts on port 5000. Browser shows 404 for static files (expected — frontend is in next task).

- [ ] **Step 4: Commit**

```bash
git add dashboard.py requirements.txt
git commit -m "feat: Flask backend with API endpoints for dashboard"
```

---

### Task 2: Frontend — HTML + CSS three-column layout

**Files:**
- Create: `static/index.html`
- Create: `static/style.css`

- [ ] **Step 1: Create static directory**

Run: `mkdir -p static`

- [ ] **Step 2: Write index.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>截图采集看板</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <div id="app">
        <!-- 左列：菜单 -->
        <div class="column" id="menu-column">
            <h3>菜单</h3>
            <ul id="menu-list">
                <li class="active" data-type="">系统状态</li>
                <li data-type="hourly">小时报告</li>
                <li data-type="daily">日总结</li>
                <li data-type="weekly">周报</li>
                <li data-type="monthly">月报</li>
            </ul>
            <hr>
            <div id="search-box">
                <input type="text" id="search-input" placeholder="搜索...">
                <button id="search-btn">搜索</button>
            </div>
            <hr>
            <div id="control-area">
                <button id="btn-start">▶ 启动截图</button>
                <button id="btn-stop">■ 停止截图</button>
                <button id="btn-analyze">⟳ 触发分析</button>
            </div>
            <hr>
            <div id="status-area"></div>
        </div>

        <!-- 中列：报告列表 -->
        <div class="column" id="timeline-column">
            <h3>报告列表</h3>
            <ul id="report-list"></ul>
        </div>

        <!-- 右列：内容 -->
        <div class="column" id="content-column">
            <div id="content-view"></div>
            <div id="annotation-area">
                <div id="annotation-display"></div>
                <button id="btn-annotate">编辑标注</button>
            </div>
        </div>
    </div>

    <!-- 标注弹窗 -->
    <div id="annotation-modal" class="modal hidden">
        <div class="modal-content">
            <h3>编辑标注</h3>
            <label>标签 (逗号分隔)</label>
            <input type="text" id="ann-tags">
            <label>备注</label>
            <input type="text" id="ann-note">
            <label>状态</label>
            <select id="ann-status">
                <option value="none">未标记</option>
                <option value="done">已完成</option>
                <option value="todo">待跟进</option>
            </select>
            <div class="modal-buttons">
                <button id="ann-save">保存</button>
                <button id="ann-cancel">取消</button>
            </div>
        </div>
    </div>

    <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 3: Write style.css**

```css
* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
    font-size: 14px;
    color: #333;
    background: #1a1a2e;
}

#app {
    display: flex;
    height: 100vh;
}

.column {
    overflow-y: auto;
    padding: 12px;
}

h3 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #888;
    margin-bottom: 12px;
}

/* 左列 */
#menu-column {
    width: 200px;
    background: #16213e;
    border-right: 1px solid #333;
    display: flex;
    flex-direction: column;
}

#menu-list {
    list-style: none;
}

#menu-list li {
    padding: 8px 12px;
    cursor: pointer;
    border-radius: 6px;
    color: #ccc;
    margin-bottom: 2px;
}

#menu-list li:hover {
    background: #1a1a3e;
}

#menu-list li.active {
    background: #4361ee;
    color: #fff;
}

hr {
    border: none;
    border-top: 1px solid #333;
    margin: 12px 0;
}

#search-box {
    display: flex;
    gap: 6px;
}

#search-input {
    flex: 1;
    padding: 6px 8px;
    border: 1px solid #444;
    border-radius: 4px;
    background: #1a1a2e;
    color: #ddd;
    font-size: 13px;
}

#search-btn {
    padding: 6px 10px;
    background: #4361ee;
    color: #fff;
    border: none;
    border-radius: 4px;
    cursor: pointer;
}

#control-area {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

#control-area button {
    padding: 8px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
}

#btn-start { background: #2d6a4f; color: #fff; }
#btn-stop { background: #d62828; color: #fff; }
#btn-analyze { background: #5a189a; color: #fff; }

#status-area {
    font-size: 12px;
    color: #888;
    line-height: 1.6;
}

/* 中列 */
#timeline-column {
    width: 280px;
    background: #0f3460;
    border-right: 1px solid #333;
}

#report-list {
    list-style: none;
}

#report-list li {
    padding: 8px 12px;
    cursor: pointer;
    border-radius: 6px;
    color: #ccc;
    margin-bottom: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

#report-list li:hover {
    background: #1a1a3e;
}

#report-list li.active {
    background: #4361ee;
    color: #fff;
}

/* 右列 */
#content-column {
    flex: 1;
    background: #1a1a2e;
    display: flex;
    flex-direction: column;
}

#content-view {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
    color: #ddd;
    line-height: 1.7;
}

#content-view h1 { font-size: 20px; color: #fff; margin-bottom: 12px; }
#content-view h2 { font-size: 16px; color: #4cc9f0; margin: 16px 0 8px 0; }
#content-view h3 { font-size: 14px; color: #aaa; }
#content-view ul { padding-left: 20px; }
#content-view li { margin-bottom: 4px; }
#content-view p { margin-bottom: 8px; }
#content-view strong { color: #f72585; }

#annotation-area {
    border-top: 1px solid #333;
    padding: 10px 12px;
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 40px;
}

#annotation-display {
    flex: 1;
    color: #888;
    font-size: 13px;
}

#btn-annotate {
    padding: 6px 12px;
    background: #4361ee;
    color: #fff;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
}

/* 标注弹窗 */
.modal {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
}

.modal.hidden { display: none; }

.modal-content {
    background: #16213e;
    padding: 24px;
    border-radius: 12px;
    width: 400px;
    border: 1px solid #333;
}

.modal-content h3 {
    color: #fff;
    margin-bottom: 16px;
}

.modal-content label {
    display: block;
    color: #888;
    margin: 8px 0 4px 0;
    font-size: 12px;
}

.modal-content input,
.modal-content select {
    width: 100%;
    padding: 8px;
    border: 1px solid #444;
    border-radius: 4px;
    background: #1a1a2e;
    color: #ddd;
    font-size: 14px;
}

.modal-buttons {
    display: flex;
    gap: 8px;
    margin-top: 16px;
}

.modal-buttons button {
    flex: 1;
    padding: 8px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
}

#ann-save { background: #4361ee; color: #fff; }
#ann-cancel { background: #444; color: #ddd; }

.tag { display: inline-block; background: #4361ee; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-right: 4px; }
.status-done { color: #2d6a4f; }
.status-todo { color: #f72585; }
```

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/style.css
git commit -m "feat: HTML and CSS for three-column dashboard layout"
```

---

### Task 3: Frontend — JavaScript logic

**Files:**
- Create: `static/app.js`

- [ ] **Step 1: Write app.js**

```javascript
// State
let currentType = "hourly";
let currentReport = null;
let annotations = {};

// --- API helpers ---
async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    return res.json();
}

// --- Menu ---
function initMenu() {
    const items = document.querySelectorAll("#menu-list li");
    items.forEach(li => {
        li.addEventListener("click", () => {
            items.forEach(i => i.classList.remove("active"));
            li.classList.add("active");
            currentType = li.dataset.type;
            loadReports();
        });
    });
    // default: 小时报告
    items[1].classList.add("active");
    items[0].classList.remove("active");
    currentType = "hourly";
}

// --- Reports list ---
async function loadReports(query) {
    let url = query
        ? `/api/search?q=${encodeURIComponent(query)}&type=${currentType}`
        : `/api/reports?type=${currentType}`;
    const reports = await api(url);
    const list = document.getElementById("report-list");
    list.innerHTML = "";
    reports.forEach((r, i) => {
        const li = document.createElement("li");
        li.textContent = r.label;
        li.addEventListener("click", () => {
            list.querySelectorAll("li").forEach(l => l.classList.remove("active"));
            li.classList.add("active");
            showReport(r);
        });
        list.appendChild(li);
    });
    if (reports.length > 0) {
        list.children[0].classList.add("active");
        showReport(reports[0]);
    } else {
        document.getElementById("content-view").innerHTML = "<p>暂无报告</p>";
        updateAnnotationDisplay(null);
    }
}

// --- Content ---
async function showReport(report) {
    currentReport = report;
    const data = await api(`/api/report-content?path=${encodeURIComponent(report.path)}`);
    document.getElementById("content-view").innerHTML = renderMarkdown(data.content);
    updateAnnotationDisplay(report);
}

function renderMarkdown(text) {
    if (!text) return "<p>无内容</p>";
    let html = text
        .replace(/^### (.+)$/gm, "<h3>$1</h3>")
        .replace(/^## (.+)$/gm, "<h2>$1</h2>")
        .replace(/^# (.+)$/gm, "<h1>$1</h1>")
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/^- (.+)$/gm, "<li>$1</li>")
        .replace(/\n{2,}/g, "<br><br>")
        .replace(/\n/g, "<br>");
    // wrap consecutive <li> in <ul>
    html = html.replace(/((?:<li>.*?<\/li>(?:<br>)?)+)/g, "<ul>$1</ul>");
    return html;
}

// --- Status ---
async function loadStatus() {
    const s = await api("/api/status");
    const area = document.getElementById("status-area");
    const statusText = s.running
        ? `<span style="color:#2d6a4f">● 运行中</span> (PID=${s.pid})`
        : `<span style="color:#d62828">○ 未运行</span>`;
    area.innerHTML = `${statusText}<br>今日: ${s.today_count}张 ${s.today_latest ? "(最新 " + s.today_latest + ")" : ""}`;

    document.getElementById("btn-start").style.display = s.running ? "none" : "block";
    document.getElementById("btn-stop").style.display = s.running ? "block" : "none";
}

// --- Controls ---
function initControls() {
    document.getElementById("btn-start").addEventListener("click", async () => {
        await api("/api/control/start", { method: "POST" });
        setTimeout(loadStatus, 1000);
    });
    document.getElementById("btn-stop").addEventListener("click", async () => {
        await api("/api/control/stop", { method: "POST" });
        setTimeout(loadStatus, 500);
    });
    document.getElementById("btn-analyze").addEventListener("click", async () => {
        const btn = document.getElementById("btn-analyze");
        btn.textContent = "分析中...";
        btn.disabled = true;
        await api("/api/control/analyze", { method: "POST" });
        btn.textContent = "⟳ 触发分析";
        btn.disabled = false;
        loadReports();
    });
}

// --- Search ---
function initSearch() {
    const input = document.getElementById("search-input");
    const btn = document.getElementById("search-btn");
    const doSearch = () => {
        const q = input.value.trim();
        if (q) loadReports(q);
        else loadReports();
    };
    btn.addEventListener("click", doSearch);
    input.addEventListener("keydown", e => {
        if (e.key === "Enter") doSearch();
        if (e.key === "Escape") { input.value = ""; loadReports(); }
    });
}

// --- Annotations ---
async function loadAnnotations() {
    annotations = await api("/api/annotations");
}

function updateAnnotationDisplay(report) {
    const display = document.getElementById("annotation-display");
    if (!report) {
        display.innerHTML = "";
        return;
    }
    const ann = annotations[report.date]?.[report.hour];
    if (ann) {
        const tags = (ann.tags || []).map(t => `<span class="tag">${t}</span>`).join("");
        const statusClass = `status-${ann.status}`;
        const statusIcon = { done: "✓", todo: "◎", none: "○" }[ann.status] || "○";
        let html = `<span class="${statusClass}">${statusIcon} ${ann.status}</span>`;
        if (tags) html += ` ${tags}`;
        if (ann.note) html += ` 📝 ${ann.note}`;
        display.innerHTML = html;
    } else {
        display.innerHTML = '<span style="color:#555">按"编辑标注"添加</span>';
    }
}

function initAnnotations() {
    document.getElementById("btn-annotate").addEventListener("click", () => {
        if (!currentReport) return;
        const ann = annotations[currentReport.date]?.[currentReport.hour] || {};
        document.getElementById("ann-tags").value = (ann.tags || []).join(", ");
        document.getElementById("ann-note").value = ann.note || "";
        document.getElementById("ann-status").value = ann.status || "none";
        document.getElementById("annotation-modal").classList.remove("hidden");
    });

    document.getElementById("ann-save").addEventListener("click", async () => {
        if (!currentReport) return;
        const tags = document.getElementById("ann-tags").value
            .split(",").map(t => t.trim()).filter(Boolean);
        const note = document.getElementById("ann-note").value;
        const status = document.getElementById("ann-status").value;
        await api("/api/annotations", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                date: currentReport.date,
                hour: currentReport.hour,
                annotation: { tags, note, status },
            }),
        });
        await loadAnnotations();
        updateAnnotationDisplay(currentReport);
        document.getElementById("annotation-modal").classList.add("hidden");
    });

    document.getElementById("ann-cancel").addEventListener("click", () => {
        document.getElementById("annotation-modal").classList.add("hidden");
    });
}

// --- Auto-refresh status every 30s ---
setInterval(loadStatus, 30000);

// --- Init ---
document.addEventListener("DOMContentLoaded", async () => {
    initMenu();
    initControls();
    initSearch();
    initAnnotations();
    await loadAnnotations();
    await loadStatus();
    await loadReports();
});
```

- [ ] **Step 2: Test full flow**

Run: `./venv/bin/python dashboard.py`
Open: `http://localhost:5000`
Expected: Three-column layout loads. Click menu items to filter reports. Click report to view content. Search works. Control buttons work. Annotation modal opens and saves.

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat: frontend JS with reports, search, annotations, and controls"
```

---

### Task 4: End-to-end verification

- [ ] **Step 1: Start Flask, open browser**

Run: `./venv/bin/python dashboard.py`
Open: `http://localhost:5000`

- [ ] **Step 2: Verify menu navigation**

Click each menu item (小时报告, 日总结, 周报, 月报). Middle column updates with filtered reports.

- [ ] **Step 3: Verify report viewing**

Click a report in middle column. Right panel shows rendered markdown with timeline, allocation, AI content.

- [ ] **Step 4: Verify search**

Type keyword in search box, press Enter. Middle column shows matching results only.

- [ ] **Step 5: Verify annotations**

Click "编辑标注", fill tags/note/status, save. Annotation shows in bottom of right panel. Check `annotations.json` is created.

- [ ] **Step 6: Verify process control**

Click "▶ 启动截图", verify status turns green. Click "■ 停止截图", verify status turns red. Click "⟳ 触发分析", verify loading state and report refresh.

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "feat: complete web dashboard with three-column layout, annotations, search, and controls"
```
