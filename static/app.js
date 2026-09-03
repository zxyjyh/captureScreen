// State
let currentType = "hourly";
let currentReport = null;
let allReports = [];
let todayReports = [];
let annotations = {};
let allDates = [];

// 时间分配条：同一强调色的明度梯度，不是彩虹。
// 条目本来就按时长排序，深浅已经表达了排名；换成六个不同色相反而要
// 先记住「哪个颜色是哪个应用」才读得懂。
const COLORS = ["#4a5578", "#656e8d", "#808aa3", "#9ba3b8", "#b7bdd0", "#ced3e0"];

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
            // 存储视图要整幅宽度放指标卡和表格，中列让出来
            document.getElementById("timeline-column").style.display =
                currentType === "storage" ? "none" : "";
            if (currentType === "storage") {
                loadStorage();
            } else if (currentType === "hourly") {
                loadTimeline();
            } else {
                loadReportsList();
            }
        });
    });
    // 按 data-type 定位，不按下标 —— 菜单增删项时下标会错位
    items.forEach(i => i.classList.toggle("active", i.dataset.type === "hourly"));
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
        html += `<div class="ai-section" >
            <h2>文章内容总结</h2>
            ${renderMarkdownInline(report.articleSummary)}
        </div>`;
    }

    // Section 3: 关键内容
    if (report.keyContent) {
        html += `<div class="ai-section" >
            <h2>关键内容</h2>
            ${renderMarkdownInline(report.keyContent)}
        </div>`;
    }

    // Section 3: 意图分析
    if (report.intentAnalysis) {
        html += `<div class="ai-section" >
            <h2>意图分析</h2>
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
        html += `<div class="ai-section" >
            <h2>亮点</h2>
            ${renderMarkdownInline(report.highlights)}
        </div>`;
    }

    // Section 6: 可改进
    if (report.improvements) {
        html += `<div class="ai-section" >
            <h2>可改进</h2>
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

    // 周报月报不自动生成，日总结每天一次自动跑 —— 这里给个手动补跑的入口
    const label = { daily: "日总结", weekly: "本周周报", monthly: "本月月报" }[currentType];
    if (label) {
        const gen = document.createElement("button");
        gen.className = "gen-btn";
        gen.textContent = `生成${label}`;
        gen.addEventListener("click", () => runSummary(currentType, gen, label));
        view.appendChild(gen);
    }

    if (reports.length === 0) {
        const empty = document.createElement("p");
        empty.className = "hint";
        empty.style.padding = "0 22px";
        empty.textContent = currentType === "daily"
            ? "还没有日总结。每天 23:50 自动生成，也可以现在手动跑一次。"
            : "还没有报告。周报和月报只在点击时生成。";
        view.appendChild(empty);
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
    // 状态条的颜色和圆点交给 CSS（.running / .stopped），
    // 这里只决定文案 —— 换主题时不用回来改 JS
    const pill = s.running
        ? `<div class="running">采集中<span style="margin-left:auto;font-family:var(--font-num);opacity:.75;">${s.today_latest || ""}</span></div>`
        : `<div class="stopped">未运行</div>`;
    area.innerHTML = pill +
        `今日 <span class="num">${s.today_count}</span> 张` +
        (s.running ? ` · PID <span class="num">${s.pid}</span>` : "");
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
                setTimeout(() => { btn.textContent = "⟳ 分析上一小时"; btn.disabled = false; loadTimeline(); }, 1500);
            } else {
                btn.textContent = "✗ 失败";
                alert("分析失败: " + (data.message || "未知错误"));
                btn.textContent = "⟳ 分析上一小时";
                btn.disabled = false;
            }
        } catch (e) {
            btn.textContent = "✗ 失败";
            alert("请求失败: " + e.message);
            btn.textContent = "⟳ 分析上一小时";
            btn.disabled = false;
        }
    });
}

// --- Search ---
function initSearch() {
    const input = document.getElementById("search-input");
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
    // 回车即搜，Esc 清空 —— 一个输入框比「输入框 + 按钮」少一次点击
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


// --- 存储管理 ---
// 图片和文本分开显示是有意义的：实测图片占 99.9%、文本占 0.1%。
// 只删图片能省下几乎全部空间，而「那天我在干什么」照样答得上来 ——
// 把这两个数并排放，人才知道该删哪个。
function fmtSize(n) {
    if (!n) return "—";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i >= 2 ? 1 : 0) + " " + u[i];
}

let storageData = null;

async function loadStorage() {
    const content = document.getElementById("content-view");
    document.getElementById("content-title").textContent = "存储管理";
    document.getElementById("date-selector").innerHTML = "";
    content.innerHTML = '<div class="loading">读取中…</div>';

    storageData = await api("/api/storage");
    const d = storageData;

    const images = d.rows.reduce((a, r) => a + r.images, 0);
    const text = d.rows.reduce((a, r) => a + r.text, 0);
    const imgPct = images + text ? (images / (images + text) * 100) : 0;
    const capPct = d.max_disk_mb ? Math.min(100, d.total / (d.max_disk_mb * 1024 * 1024) * 100) : 0;

    // 概览用卡，逐条数据用表：卡片一眼看总量，表格同屏放得下更多天
    let main = '<div class="metric-grid">';
    main += metric("图片", images, `占总量 ${imgPct.toFixed(1)}% · 可随时删`);
    main += metric("文本", text, "记忆本体 · 长期保留", true);
    main += d.max_disk_mb
        ? `<div class="metric"><div class="metric-label">磁盘上限</div>` +
          `<div class="metric-value">${capPct.toFixed(0)}<span class="metric-unit">%</span></div>` +
          `<div class="metric-track"><i style="width:${Math.max(capPct, 1)}%"></i></div></div>`
        : metric("已用", d.total, "未设上限");
    main += metric("磁盘可用", d.disk_free, d.max_disk_mb ? "超上限自动清最旧图片" : "");
    main += "</div>";

    main += '<table class="storage-table"><thead><tr>' +
            '<th><input type="checkbox" id="st-all"></th><th>日期</th>' +
            "<th>图片</th><th>文本</th><th>报告</th></tr></thead><tbody>";
    for (const r of d.rows) {
        const files = r.file_count ? `<span class="day-meta">${r.file_count} 个文件</span>` : "";
        main += `<tr data-date="${r.date}"><td><input type="checkbox" class="st-pick" value="${r.date}"></td>` +
                `<td>${r.date}${files}</td><td>${fmtSize(r.images)}</td>` +
                `<td>${fmtSize(r.text)}</td>` +
                `<td>${r.has_report ? fmtSize(r.reports) : "—"}</td></tr>`;
    }
    main += "</tbody></table>";

    // 保留这个空元素：勾选之后由 sync() 填「已选 N 天 · X MB」——
    // 那不是说明文字，是不可逆操作前必须看到的事实
    let side = '<p class="hint" id="st-picked"></p>';
    side += '<button id="st-del-img" class="danger-soft">只删图片，保留文本</button>';
    side += '<button id="st-del-all" class="danger">全部删除</button>';

    if (d.backups.length) {
        side += '<hr><h4 style="font-size:14px;font-weight:600;margin:16px 0 4px;">备份目录</h4>';
        side += '<p class="hint">重建 git 历史时留下的，里面可能有旧截图。看板不会动它，要删请在终端执行：</p>';
        for (const b of d.backups) {
            side += `<pre class="cmd">rm -rf ${b.name}</pre><p class="hint">${fmtSize(b.size)}</p>`;
        }
    }

    content.innerHTML =
        '<div style="display:grid;grid-template-columns:minmax(0,1fr) 292px;gap:30px;align-items:start;">' +
        `<div>${main}</div><div>${side}</div></div>`;

    const picked = () => [...document.querySelectorAll(".st-pick:checked")].map(c => c.value);

    function sync() {
        const sel = picked();
        document.querySelectorAll(".storage-table tbody tr").forEach(tr => {
            tr.classList.toggle("picked", sel.includes(tr.dataset.date));
        });
        const bytes = d.rows.filter(r => sel.includes(r.date))
                            .reduce((a, r) => a + r.images + r.text + r.reports, 0);
        document.getElementById("st-picked").innerHTML = sel.length
            ? `已选 <b>${sel.length}</b> 天 · <b>${fmtSize(bytes)}</b>`
            : "";
    }

    document.getElementById("st-all").addEventListener("change", (e) => {
        document.querySelectorAll(".st-pick").forEach(c => c.checked = e.target.checked);
        sync();
    });
    document.querySelectorAll(".st-pick").forEach(c => c.addEventListener("change", sync));

    document.getElementById("st-del-img").addEventListener("click", () => doPurge(picked(), true));
    document.getElementById("st-del-all").addEventListener("click", () => doPurge(picked(), false));
}

function metric(label, bytes, note, accent) {
    const s = fmtSize(bytes);
    const [num, unit] = s === "—" ? ["0", "B"] : s.split(" ");
    return `<div class="metric"><div class="metric-label">${label}</div>` +
           `<div class="metric-value${accent ? " accent" : ""}">${num}<span class="metric-unit">${unit}</span></div>` +
           (note ? `<div class="metric-note">${note}</div>` : "") + "</div>";
}

async function doPurge(dates, keepText) {
    if (!dates.length) { alert("请先勾选日期"); return; }
    const what = keepText ? "图片（保留文本与报告）" : "全部数据（含报告与向量索引）";
    if (!confirm(`将删除 ${dates.length} 天的${what}：\n\n${dates.join("、")}\n\n此操作不可撤销，继续？`)) return;

    const res = await fetch("/api/purge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dates, keep_text: keepText }),
    }).then(r => r.json());

    if (res.error) { alert("失败：" + res.error); return; }
    alert(`已释放 ${fmtSize(res.freed)}` + (res.vectors ? `，清除向量 ${res.vectors} 条` : ""));
    loadStorage();
}


// --- 按需生成 ---
// 小时报告不再自动分析：每小时一次模型调用，而绝大多数小时没人会看。
// 要看哪个小时就点哪个。周报月报同理。
async function runSummary(kind, btn, label) {
    const original = btn.textContent;
    btn.textContent = "生成中…";
    btn.disabled = true;
    try {
        const res = await fetch("/api/control/summarize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kind }),
        }).then(r => r.json());
        if (!res.ok) { alert(`${label}生成失败：${res.message || "未知错误"}`); return; }
        btn.textContent = "✓ 已生成";
        setTimeout(() => loadReportsList(), 900);
    } catch (e) {
        alert(`${label}生成失败：${e.message}`);
    } finally {
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 900);
    }
}

async function analyzeHour(date, hour, btn) {
    const original = btn.textContent;
    btn.textContent = "分析中…";
    btn.disabled = true;
    try {
        const res = await fetch("/api/control/analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ date, hour }),
        }).then(r => r.json());
        if (!res.ok) { alert("分析失败：" + (res.message || "未知错误")); return; }
        if (!res.generated) { alert(res.message); return; }
        loadTimeline();
    } catch (e) {
        alert("分析失败：" + e.message);
    } finally {
        btn.textContent = original;
        btn.disabled = false;
    }
}
