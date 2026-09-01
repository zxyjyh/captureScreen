// State
let currentType = "hourly";
let currentReport = null;
let allReports = [];
let todayReports = [];
let annotations = {};
let allDates = [];

const COLORS = ["#4361ee", "#f72585", "#4cc9f0", "#7209b7", "#2d6a4f", "#e76f51"];

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
            if (currentType === "" || currentType === "hourly") {
                loadTimeline();
            } else {
                loadReportsList();
            }
        });
    });
    items[1].classList.add("active");
    items[0].classList.remove("active");
    currentType = "hourly";
}

// --- Date selector ---
function buildDateSelector() {
    const sel = document.getElementById("date-selector");
    sel.innerHTML = "";
    allDates.forEach(d => {
        const btn = document.createElement("button");
        btn.textContent = d.slice(5);
        btn.dataset.date = d;
        btn.addEventListener("click", () => {
            sel.querySelectorAll("button").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            filterByDate(d);
        });
        sel.appendChild(btn);
    });
    if (allDates.length > 0) {
        sel.children[0].classList.add("active");
    }
}

function filterByDate(date) {
    todayReports = allReports.filter(r => r.date === date && r.type === "hourly");
    renderTimeline(todayReports);
}

// --- Timeline rendering ---
function renderTimeline(reports) {
    const view = document.getElementById("timeline-view");
    view.innerHTML = "";

    if (reports.length === 0) {
        view.innerHTML = '<p style="color:#555;padding:20px;">暂无报告</p>';
        document.getElementById("content-view").innerHTML = "";
        updateAnnotationDisplay(null);
        return;
    }

    // 时间线
    const container = document.createElement("div");
    container.className = "tl-container";

    reports.forEach((r, i) => {
        const item = document.createElement("div");
        item.className = "tl-item" + (i === 0 ? " active" : "");
        item.innerHTML = `
            <div class="tl-time">${r.hour}:00 - ${r.hour}:59</div>
            <div class="tl-app">${r.app || "加载中..."}</div>
            <div class="tl-title">${r.title || ""}</div>
            <div class="tl-duration">${r.minutes || ""}分钟</div>
        `;
        item.addEventListener("click", () => {
            container.querySelectorAll(".tl-item").forEach(el => el.classList.remove("active"));
            item.classList.add("active");
            showReport(r);
        });
        container.appendChild(item);
    });

    view.appendChild(container);

    // 时间分配统计
    const allocation = buildAllocation(reports);
    if (Object.keys(allocation).length > 0) {
        const alloc = document.createElement("div");
        alloc.className = "tl-alloc";
        alloc.innerHTML = "<h4>时间分配</h4>";
        const total = Object.values(allocation).reduce((a, b) => a + b, 0);
        Object.entries(allocation)
            .sort((a, b) => b[1] - a[1])
            .forEach(([app, mins], ci) => {
                const pct = total > 0 ? Math.round(mins / total * 100) : 0;
                const color = COLORS[ci % COLORS.length];
                alloc.innerHTML += `
                    <div class="tl-alloc-bar">
                        <span class="tl-alloc-label">${app}</span>
                        <div class="tl-alloc-track">
                            <div class="tl-alloc-fill" style="width:${pct}%;background:${color}"></div>
                        </div>
                        <span class="tl-alloc-pct">${pct}%</span>
                    </div>`;
            });
        view.appendChild(alloc);
    }

    // Load metadata for each report
    reports.forEach(r => loadReportMeta(r, view));
    showReport(reports[0]);
}

function buildAllocation(reports) {
    const alloc = {};
    reports.forEach(r => {
        if (r.app && r.minutes) {
            alloc[r.app] = (alloc[r.app] || 0) + r.minutes;
        }
    });
    return alloc;
}

async function loadReportMeta(report, container) {
    const data = await api(`/api/report-content?path=${encodeURIComponent(report.path)}`);
    const parsed = parseReport(data.content);
    report.app = parsed.app;
    report.title = parsed.title;
    report.minutes = parsed.minutes;
    report.aiContent = parsed.aiContent;
    report.timeline = parsed.timeline;
    report.allocation = parsed.allocation;
    report.activityFlow = parsed.activityFlow;
    report.articleSummary = parsed.articleSummary;
    report.keyContent = parsed.keyContent;
    report.intentAnalysis = parsed.intentAnalysis;
    report.highlights = parsed.highlights;
    report.improvements = parsed.improvements;
    report.suggestions = parsed.suggestions;

    // Update timeline items with metadata
    const items = container.querySelectorAll(".tl-item");
    const idx = todayReports.indexOf(report);
    if (idx >= 0 && items[idx]) {
        items[idx].querySelector(".tl-app").textContent = report.app || "未知";
        items[idx].querySelector(".tl-title").textContent = report.title || "";
        items[idx].querySelector(".tl-duration").textContent = report.minutes ? `${report.minutes}分钟` : "";
    }

    // Re-render allocation with real data
    const alloc = buildAllocation(todayReports.filter(r => r.app));
    const allocEl = container.querySelector(".tl-alloc");
    if (allocEl && Object.keys(alloc).length > 0) {
        const total = Object.values(alloc).reduce((a, b) => a + b, 0);
        let html = "<h4>时间分配</h4>";
        Object.entries(alloc)
            .sort((a, b) => b[1] - a[1])
            .forEach(([app, mins], ci) => {
                const pct = total > 0 ? Math.round(mins / total * 100) : 0;
                const color = COLORS[ci % COLORS.length];
                html += `
                    <div class="tl-alloc-bar">
                        <span class="tl-alloc-label">${app}</span>
                        <div class="tl-alloc-track">
                            <div class="tl-alloc-fill" style="width:${pct}%;background:${color}"></div>
                        </div>
                        <span class="tl-alloc-pct">${pct}%</span>
                    </div>`;
            });
        allocEl.innerHTML = html;
    }

    // Show first report's AI content
    if (idx === 0) {
        showReport(report);
    }
}

function parseReport(text) {
    if (!text) return { app: "", title: "", minutes: 0, aiContent: "", timeline: "", allocation: "", highlights: "", improvements: "", suggestions: "" };

    // Extract timeline entries
    const timelineMatch = text.match(/## 活动时间线\n([\s\S]*?)(?=\n##|$)/);
    const timeline = timelineMatch ? timelineMatch[1].trim() : "";

    // First app from timeline
    const firstEntry = timeline.split("\n")[0] || "";
    const appMatch = firstEntry.match(/\d{2}:\d{2}-\d{2}:\d{2}\s+(.+?)(?:\s+-|$)/);
    const app = appMatch ? appMatch[1].trim() : "";

    // Title from first entry
    const titleMatch = firstEntry.match(/-\s+(.+)$/);
    const title = titleMatch ? titleMatch[1].trim() : "";

    // Extract allocation for minutes
    const allocMatch = text.match(/## 时间分配\n([\s\S]*?)(?=\n##|$)/);
    const allocation = allocMatch ? allocMatch[1].trim() : "";
    const minutesMatch = allocation.match(/(\d+)分钟/);
    const minutes = minutesMatch ? parseInt(minutesMatch[1]) : 0;

    // Extract subsections by ### headings (inside 具体内容) or ## headings (top level)
    const activityFlow = extractSection(text, "活动流");
    const articleSummary = extractSection(text, "文章内容总结");
    const keyContent = extractSection(text, "关键内容");
    const intentAnalysis = extractSection(text, "意图分析");
    const highlights = extractSection(text, "亮点") || extractSection(text, "今日亮点") || "";
    const improvements = extractSection(text, "可改进") || extractSection(text, "可改进处") || "";
    const suggestions = extractSection(text, "建议") || extractSection(text, "明日建议") || "";

    // Fallback: if no ### sections found, use 具体内容 as aiContent
    const aiContent = activityFlow || keyContent || intentAnalysis
        ? [activityFlow, articleSummary, keyContent, intentAnalysis].filter(Boolean).join("\n\n")
        : (text.match(/## 具体内容\n([\s\S]*)/)?.[1]?.trim() || "");

    return { app, title, minutes, aiContent, timeline, allocation, activityFlow, articleSummary, keyContent, intentAnalysis, highlights, improvements, suggestions };
}

function extractSection(text, sectionName) {
    // Match ### heading (preferred) or ## heading, stop at next same-level heading
    const regex = new RegExp(`###\\s+${sectionName}\\n([\\s\\S]*?)(?=\\n###\\s|\\n##\\s|$)`, 'i');
    let match = text.match(regex);
    if (match) return match[1].trim();
    // Fallback to ## heading
    const regex2 = new RegExp(`##\\s+${sectionName}\\n([\\s\\S]*?)(?=\\n##\\s|$)`, 'i');
    match = text.match(regex2);
    return match ? match[1].trim() : "";
}

// --- Show AI content ---
async function showReport(report) {
    currentReport = report;
    const view = document.getElementById("content-view");
    const title = document.getElementById("content-title");

    if (!report.aiContent) {
        const data = await api(`/api/report-content?path=${encodeURIComponent(report.path)}`);
        const parsed = parseReport(data.content);
        Object.assign(report, parsed);
    }

    title.textContent = `${report.date} ${report.hour}:00`;

    let html = "";

    // Screenshots section (collapsed by default)
    html += `<div id="screenshots-area" data-date="${report.date}" data-hour="${report.hour}">
        <div class="screenshots-toggle" onclick="toggleScreenshots()">▶ 查看截图（点击展开）</div>
        <div id="screenshots-content" class="hidden"></div>
    </div>`;

    // Section 1: 活动流
    if (report.activityFlow) {
        html += `<div class="ai-section">
            <h2>活动流</h2>
            ${renderMarkdownInline(report.activityFlow)}
        </div>`;
    }

    // Section 2: 文章内容总结
    if (report.articleSummary && !report.articleSummary.includes("本时段无长文阅读")) {
        html += `<div class="ai-section" style="border-left-color:#e76f51">
            <h2 style="color:#e76f51">文章内容总结</h2>
            ${renderMarkdownInline(report.articleSummary)}
        </div>`;
    }

    // Section 3: 关键内容
    if (report.keyContent) {
        html += `<div class="ai-section" style="border-left-color:#4cc9f0">
            <h2 style="color:#4cc9f0">关键内容</h2>
            ${renderMarkdownInline(report.keyContent)}
        </div>`;
    }

    // Section 3: 意图分析
    if (report.intentAnalysis) {
        html += `<div class="ai-section" style="border-left-color:#7209b7">
            <h2 style="color:#7209b7">意图分析</h2>
            ${renderMarkdownInline(report.intentAnalysis)}
        </div>`;
    }

    // Section 4: 时间分配
    if (report.allocation) {
        html += `<div class="ai-section">
            <h2>时间分配</h2>
            ${renderMarkdownInline(report.allocation)}
        </div>`;
    }

    // Section 5: 亮点
    if (report.highlights) {
        html += `<div class="ai-section" style="border-left-color:#2d6a4f">
            <h2 style="color:#2d6a4f">亮点</h2>
            ${renderMarkdownInline(report.highlights)}
        </div>`;
    }

    // Section 6: 可改进
    if (report.improvements) {
        html += `<div class="ai-section" style="border-left-color:#f72585">
            <h2 style="color:#f72585">可改进</h2>
            ${renderMarkdownInline(report.improvements)}
        </div>`;
    }

    // Fallback: if no sections parsed, show raw aiContent
    if (!html && report.aiContent) {
        html = `<div class="ai-section"><h2>AI 分析</h2>${renderMarkdownInline(report.aiContent)}</div>`;
    }

    view.innerHTML = html;
    loadScreenshots(report.date, report.hour);
    updateAnnotationDisplay(report);
}

async function loadScreenshots(date, hour) {
    if (!date || !hour) return;
    const content = document.getElementById("screenshots-content");
    if (!content) return;
    try {
        const shots = await api(`/api/screenshots?date=${date}&hour=${hour}`);
        if (!shots || shots.length === 0) {
            content.innerHTML = '<p style="color:#555;font-size:12px;padding:8px">该时段无截图</p>';
            return;
        }
        let html = `<div class="screenshots-grid">`;
        shots.forEach(s => {
            html += `
                <div class="screenshot-card" onclick="this.classList.toggle('expanded')">
                    <img src="${s.url}" alt="${s.time}" loading="lazy">
                    <div class="screenshot-info">
                        <span class="screenshot-time">${s.time}</span>
                        <span class="screenshot-app">${s.app}</span>
                        ${s.title ? `<span class="screenshot-title">${s.title}</span>` : ''}
                    </div>
                </div>`;
        });
        html += '</div>';
        content.innerHTML = html;
        // Update toggle text with count
        const toggle = document.querySelector(".screenshots-toggle");
        if (toggle) toggle.textContent = `▶ 查看截图（${shots.length}张，点击展开）`;
    } catch (e) {
        content.innerHTML = '';
    }
}

function toggleScreenshots() {
    const content = document.getElementById("screenshots-content");
    const toggle = document.querySelector(".screenshots-toggle");
    if (!content) return;
    if (content.classList.contains("hidden")) {
        content.classList.remove("hidden");
        toggle.textContent = toggle.textContent.replace("▶", "▼");
        // Load screenshots on first expand
        const area = document.getElementById("screenshots-area");
        if (area && !content.innerHTML) {
            loadScreenshots(area.dataset.date, area.dataset.hour);
        }
    } else {
        content.classList.add("hidden");
        toggle.textContent = toggle.textContent.replace("▼", "▶");
    }
}

function renderAIContent(text) {
    if (!text) return '<p style="color:#555">暂无 AI 分析内容</p>';
    return renderMarkdownInline(text);
}

function renderMarkdownInline(text) {
    if (!text) return '<p style="color:#555">暂无</p>';
    let html = text
        .replace(/^### (.+)$/gm, "<h3>$1</h3>")
        .replace(/^## (.+)$/gm, "<h2>$1</h2>")
        .replace(/^# (.+)$/gm, "<h1>$1</h1>")
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/^- (.+)$/gm, "<li>$1</li>")
        .replace(/\n{2,}/g, "<br><br>")
        .replace(/\n/g, "<br>");
    html = html.replace(/((?:<li>.*?<\/li>(?:<br>)?)+)/g, "<ul>$1</ul>");
    return html;
}

// --- Load for other types (daily, weekly, monthly) ---
async function loadReportsList() {
    const reports = await api(`/api/reports?type=${currentType}`);
    const view = document.getElementById("timeline-view");
    view.innerHTML = "";
    document.getElementById("date-selector").innerHTML = "";

    if (reports.length === 0) {
        view.innerHTML = '<p style="color:#555;padding:20px;">暂无报告</p>';
        document.getElementById("content-view").innerHTML = "";
        return;
    }

    const container = document.createElement("div");
    container.className = "tl-container";

    reports.forEach((r, i) => {
        const item = document.createElement("div");
        item.className = "tl-item" + (i === 0 ? " active" : "");
        const typeLabel = { daily: "日总结", weekly: "周报", monthly: "月报" }[r.type] || "";
        item.innerHTML = `
            <div class="tl-time">${typeLabel}</div>
            <div class="tl-app">${r.label}</div>
        `;
        item.addEventListener("click", () => {
            container.querySelectorAll(".tl-item").forEach(el => el.classList.remove("active"));
            item.classList.add("active");
            showFullReport(r);
        });
        container.appendChild(item);
    });

    view.appendChild(container);
    showFullReport(reports[0]);
}

async function showFullReport(report) {
    currentReport = report;
    const data = await api(`/api/report-content?path=${encodeURIComponent(report.path)}`);
    const title = document.getElementById("content-title");
    title.textContent = report.label;
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
    html = html.replace(/((?:<li>.*?<\/li>(?:<br>)?)+)/g, "<ul>$1</ul>");
    return `<div class="ai-section">${html}</div>`;
}

// --- Timeline loader ---
async function loadTimeline() {
    allReports = await api("/api/reports?type=hourly");
    allDates = [...new Set(allReports.map(r => r.date))].sort().reverse();
    buildDateSelector();
    if (allDates.length > 0) {
        filterByDate(allDates[0]);
    }
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
        try {
            const res = await fetch("/api/control/analyze", { method: "POST" });
            const data = await res.json();
            if (data.ok) {
                btn.textContent = "✓ 成功";
                setTimeout(() => { btn.textContent = "⟳ 触发分析"; btn.disabled = false; loadTimeline(); }, 1500);
            } else {
                btn.textContent = "✗ 失败";
                alert("分析失败: " + (data.message || "未知错误"));
                btn.textContent = "⟳ 触发分析";
                btn.disabled = false;
            }
        } catch (e) {
            btn.textContent = "✗ 失败";
            alert("请求失败: " + e.message);
            btn.textContent = "⟳ 触发分析";
            btn.disabled = false;
        }
    });
}

// --- Search ---
function initSearch() {
    const input = document.getElementById("search-input");
    const btn = document.getElementById("search-btn");
    const doSearch = async () => {
        const q = input.value.trim();
        if (!q) { loadTimeline(); return; }
        const results = await api(`/api/search?q=${encodeURIComponent(q)}`);
        const view = document.getElementById("timeline-view");
        document.getElementById("date-selector").innerHTML = "";
        view.innerHTML = "";

        if (results.length === 0) {
            view.innerHTML = '<p style="color:#555;padding:20px;">无搜索结果</p>';
            return;
        }

        const container = document.createElement("div");
        container.className = "tl-container";
        results.forEach((r, i) => {
            const item = document.createElement("div");
            item.className = "tl-item" + (i === 0 ? " active" : "");
            item.innerHTML = `
                <div class="tl-time">${r.type === "hourly" ? r.hour + ":00" : r.type}</div>
                <div class="tl-app">${r.label}</div>
            `;
            item.addEventListener("click", () => {
                container.querySelectorAll(".tl-item").forEach(el => el.classList.remove("active"));
                item.classList.add("active");
                showFullReport(r);
            });
            container.appendChild(item);
        });
        view.appendChild(container);
        showFullReport(results[0]);
    };
    btn.addEventListener("click", doSearch);
    input.addEventListener("keydown", e => {
        if (e.key === "Enter") doSearch();
        if (e.key === "Escape") { input.value = ""; loadTimeline(); }
    });
}

// --- Annotations ---
async function loadAnnotations() {
    annotations = await api("/api/annotations");
}

function updateAnnotationDisplay(report) {
    const display = document.getElementById("annotation-display");
    if (!report) { display.innerHTML = ""; return; }
    const ann = annotations[report.date]?.[report.hour];
    if (ann) {
        const tags = (ann.tags || []).map(t => `<span class="tag">${t}</span>`).join("");
        const statusIcon = { done: "✓", todo: "◎", none: "○" }[ann.status] || "○";
        let html = `<span class="status-${ann.status}">${statusIcon} ${ann.status}</span>`;
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
        const tags = document.getElementById("ann-tags").value.split(",").map(t => t.trim()).filter(Boolean);
        const note = document.getElementById("ann-note").value;
        const status = document.getElementById("ann-status").value;
        await api("/api/annotations", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ date: currentReport.date, hour: currentReport.hour, annotation: { tags, note, status } }),
        });
        await loadAnnotations();
        updateAnnotationDisplay(currentReport);
        document.getElementById("annotation-modal").classList.add("hidden");
    });

    document.getElementById("ann-cancel").addEventListener("click", () => {
        document.getElementById("annotation-modal").classList.add("hidden");
    });
}

// --- Auto-refresh ---
setInterval(loadStatus, 30000);

// --- Init ---
document.addEventListener("DOMContentLoaded", async () => {
    initMenu();
    initControls();
    initSearch();
    initAnnotations();
    await loadAnnotations();
    await loadStatus();
    await loadTimeline();
});
