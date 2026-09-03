// State
let currentType = "hourly";
let currentReport = null;
let allReports = [];
let todayReports = [];
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
        return;
    }

    // 时间线
    const container = document.createElement("div");
    container.className = "tl-container";

    reports.forEach((r, i) => {
        const item = document.createElement("div");
        item.className = "tl-item" + (i === 0 ? " active" : "");
        // 只留时段。应用名和时长在右边的报告里都有，
        // 这一列的职责是「选哪个小时」，不是展示内容
        const hh = String(r.hour).padStart(2, "0");
        const next = String((Number(r.hour) + 1) % 24).padStart(2, "0");
        item.innerHTML = `<div class="tl-time">${hh}:00 - ${next}:00</div>`;
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
        const rows = Object.entries(allocation).map(([app, min]) => ({ app, min }));
        alloc.innerHTML = "<h4>今日时间分配</h4>" + donutChart(rows, true);
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
    report.taskAlloc = parsed.taskAlloc;
    report.kindAlloc = parsed.kindAlloc;
    report.activityFlow = parsed.activityFlow;
    report.articleSummary = parsed.articleSummary;
    report.keyContent = parsed.keyContent;
    report.intentAnalysis = parsed.intentAnalysis;
    report.highlights = parsed.highlights;
    report.improvements = parsed.improvements;
    report.suggestions = parsed.suggestions;

    // Re-render allocation with real data
    const alloc = buildAllocation(todayReports.filter(r => r.app));
    const allocEl = container.querySelector(".tl-alloc");
    if (allocEl && Object.keys(alloc).length > 0) {
        const rows = Object.entries(alloc).map(([app, min]) => ({ app, min }));
        allocEl.innerHTML = "<h4>今日时间分配</h4>" + donutChart(rows, true);
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

    // 任务与工作性质 —— 按应用统计回答不了「花在哪个项目上多少时间」
    const taskAlloc = (text.match(/## 任务分配\n([\s\S]*?)(?=\n##|$)/) || [])[1]?.trim() || "";
    const kindAlloc = (text.match(/## 工作性质\n([\s\S]*?)(?=\n##|$)/) || [])[1]?.trim() || "";

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

    return { app, title, minutes, aiContent, timeline, allocation, taskAlloc, kindAlloc, activityFlow, articleSummary, keyContent, intentAnalysis, highlights, improvements, suggestions };
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
        <div class="screenshots-toggle" onclick="toggleScreenshots()">▶ 查看截图</div>
        <div id="screenshots-content" class="hidden"></div>
    </div>`;

    // Section 1: 活动流
    if (report.activityFlow) {
        html += `<div class="ai-section">
            <h2>活动流</h2>
            ${renderActivityFlow(report.activityFlow)}
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

    // Section 4: 时间分配 —— 三个维度并列
    // 应用回答「用了什么」，任务回答「做了哪个项目」，性质回答「做的哪类事」。
    // 只有第一个是原来就有的，而它恰恰是三个里最不重要的。
    const dims = [
        ["任务", report.taskAlloc],
        ["工作性质", report.kindAlloc],
        ["应用", report.allocation],
    ].filter(([, v]) => v);
    if (dims.length) {
        html += `<div class="ai-section">
            <h2>时间分配</h2>
            <div class="alloc-tabs">${dims.map(([n], i) =>
                `<button class="alloc-tab${i ? "" : " active"}" data-dim="${i}">${n}</button>`).join("")}</div>
            ${dims.map(([, v], i) =>
                `<div class="alloc-pane${i ? " hidden" : ""}" data-dim="${i}">${renderAllocation(v)}</div>`).join("")}
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
    initAllocTabs();
    loadScreenshots(report.date, report.hour);
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
        // 只改数量，别动箭头 —— 这个函数是展开之后才跑的，
        // 整句覆写会把刚翻过来的 ▼ 又写回 ▶
        const toggle = document.querySelector(".screenshots-toggle");
        if (toggle) {
            const open = !document.getElementById("screenshots-content").classList.contains("hidden");
            toggle.textContent = `${open ? "▼" : "▶"} 截图 ${shots.length} 张`;
        }
    } catch (e) {
        content.innerHTML = '';
    }
}

function toggleScreenshots() {
    const content = document.getElementById("screenshots-content");
    const toggle = document.querySelector(".screenshots-toggle");
    if (!content) return;
    const open = content.classList.contains("hidden");
    content.classList.toggle("hidden", !open);
    toggle.textContent = (open ? "▼" : "▶") + toggle.textContent.slice(1);
    if (open) {
        // 第一次展开才去加载
        const area = document.getElementById("screenshots-area");
        if (area && !content.innerHTML) {
            loadScreenshots(area.dataset.date, area.dataset.hour);
        }
    }
}

function renderAIContent(text) {
    if (!text) return '<p style="color:#555">暂无 AI 分析内容</p>';
    return renderMarkdownInline(text);
}

function renderMarkdownInline(text) {
    if (!text) return '<p style="color:#555">暂无</p>';
    return renderBlocks(text);
}

// 按行的块级渲染。原来是一串正则：把所有换行换成 <br>，于是
// 「**项目与主题**」单独一行也只是行内粗体，不是标题 —— 层级就是这样丢的。
// 列表项之间还会插进多余的 <br>。
function renderBlocks(text) {
    const out = [];
    let list = null;      // 当前列表的缓冲
    let para = [];        // 当前段落的缓冲

    const flushPara = () => {
        if (para.length) { out.push(`<p>${para.join("<br>")}</p>`); para = []; }
    };
    const flushList = () => {
        if (list) {
            flushSub(list);
            out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`);
            list = null;
        }
    };
    const flush = () => { flushPara(); flushList(); };

    for (const raw of (text || "").split("\n")) {
        const line = raw.trim();
        if (!line) { flush(); continue; }
        // 缩进决定层级。原来直接 trim，二级列表被压成一级，
        // 「文件与路径」下面按项目分的那几组就全平掉了
        const indent = raw.length - raw.replace(/^\s+/, "").length;

        let m;
        if ((m = line.match(/^(#{1,4})\s+(.+)$/))) {
            flush();
            const lv = Math.min(m[1].length + 1, 4);   // 小节内的标题降一级
            out.push(`<h${lv}>${inline(m[2])}</h${lv}>`);
        } else if ((m = line.match(/^\*\*(.+?)\*\*[:：]?$/))) {
            // 独占一行的加粗当小标题 —— 模型常用它分组，如「**项目与主题**」
            flush();
            out.push(`<h4>${inline(m[1])}</h4>`);
        } else if ((m = line.match(/^\*\*(.+?)\*\*\s*[—–:：]\s*(.+)$/))) {
            // 「**1｜标题** — 一大段正文」：周报「主线」的写法。
            // 不拆开的话标题就淹在正文里，整节看不出有几条
            flush();
            out.push(`<h4>${inline(stripIndex(m[1]))}</h4><p>${inline(m[2])}</p>`);
        } else if ((m = line.match(/^[-*]\s+(.+)$/))) {
            flushPara();
            if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [], sub: null }; }
            pushItem(list, indent, `<li>${inline(m[1])}</li>`);
        } else if ((m = line.match(/^\d+[.)]\s+(.+)$/))) {
            flushPara();
            if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [], sub: null }; }
            pushItem(list, indent, `<li>${inline(m[1])}</li>`);
        } else {
            flushList();
            para.push(inline(line));
        }
    }
    flush();
    return out.join("");
}

// 缩进 >= 2 的进子列表，挂在上一条下面
function pushItem(list, indent, html) {
    if (indent >= 2 && list.items.length) {
        list.sub = list.sub || [];
        list.sub.push(html);
        return;
    }
    flushSub(list);
    list.items.push(html);
}

function flushSub(list) {
    if (list.sub && list.sub.length) {
        const last = list.items.length - 1;
        list.items[last] = list.items[last].replace(/<\/li>$/,
            `<ul class="sub">${list.sub.join("")}</ul></li>`);
        list.sub = null;
    }
}

// 去掉模型自己加的序号前缀（1｜、2. 、三、）。
// 条目已经是分开的块，前面再顶个数字是多余的噪声。
function stripIndex(t) {
    return (t || "").replace(/^\s*(?:\d+|[一二三四五六七八九十]+)\s*[｜|、.．)）:：\-–—]\s*/, "");
}

function inline(t) {
    return (t || "")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}


// --- Load for other types (daily, weekly, monthly) ---
async function loadReportsList() {
    const reports = await api(`/api/reports?type=${currentType}`);
    const view = document.getElementById("timeline-view");
    view.innerHTML = "";
    document.getElementById("date-selector").innerHTML = "";

    // 周报月报不自动生成，日总结每天一次自动跑 —— 这里给个手动补跑的入口。
    // 三种都能指定范围：补一份两周前的周报是常见需求，不该只能生成「本周」
    const gen = buildGenerator(currentType);
    if (gen) view.appendChild(gen);

    if (reports.length === 0) {
        const empty = document.createElement("p");
        empty.className = "hint";
        empty.style.padding = "0 22px";
        empty.textContent = currentType === "daily"
            ? "还没有日录。每天 23:50 自动生成，也可以现在手动跑一次。"
            : "还没有记录。周录和月录只在需要时生成。";
        view.appendChild(empty);
        document.getElementById("content-view").innerHTML = "";
        return;
    }

    const container = document.createElement("div");
    container.className = "tl-container";

    reports.forEach((r, i) => {
        const item = document.createElement("div");
        item.className = "tl-item" + (i === 0 ? " active" : "");
        // label 本身就是「2026-09-02 日总结」，类型再写一遍是重复；
        // 左边菜单也已经选中了「日总结」，这一列只需要回答「哪一天」
        const typeWord = { daily: "日总结", weekly: "周总结", monthly: "月总结" }[r.type] || "";
        const when = typeWord ? r.label.replace(typeWord, "").trim() : r.label;
        item.innerHTML = `<div class="tl-time">${when || r.label}</div>`;
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
    // 标题栏已经显示了报告名，正文里的一级标题是第三次重复，剥掉
    const body = (data.content || "").replace(/^\s*#\s+[^\n]*\n+/, "");
    document.getElementById("content-view").innerHTML = renderSections(body);
    initAllocTabs();
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

// --- Auto-refresh ---
setInterval(loadStatus, 30000);

// --- Init ---
document.addEventListener("DOMContentLoaded", async () => {
    initMenu();
    initControls();
    initSearch();
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
    const capPct = d.max_disk_mb ? Math.min(100, d.total / (d.max_disk_mb * 1024 * 1024) * 100) : 0;

    // 概览用卡，逐条数据用表：卡片一眼看总量，表格同屏放得下更多天
    let main = '<div class="metric-grid">';
    main += metric("图片", images, "");
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
async function runSummary(kind, btn, label, range) {
    const original = btn.textContent;
    btn.textContent = "生成中…";
    btn.disabled = true;
    try {
        const res = await fetch("/api/control/summarize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ kind, ...(range || {}) }),
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


// --- 时间分配：环形图 ---
// 用环形而不是实心饼：中间那块空白可以放总时长，而且比较扇形角度时
// 人眼靠的是弧长，实心的圆心区域反而干扰判断。
function renderAllocation(md) {
    const rows = [];
    for (const line of (md || "").split("\n")) {
        const m = line.match(/^\s*[-*]\s*(.+?)\s*[:：]\s*(\d+)\s*分钟/);
        if (m) rows.push({ app: m[1].trim(), min: parseInt(m[2], 10) });
    }
    if (!rows.length) return renderMarkdownInline(md);
    return donutChart(rows);
}

// 环形图核心。报告里和时间线列底部共用同一套渲染 ——
// 两处画同一种数据却长得不一样，读的人要重新学一遍怎么看。
function donutChart(rows, compact) {
    rows = rows.slice().sort((a, b) => b.min - a.min);
    const total = rows.reduce((a, r) => a + r.min, 0) || 1;

    // 尾巴合并成「其他」：七八个 2% 的扇形谁也看不清，只会把图挤花
    const MAIN = 6, MIN_PCT = 3;
    const main = [], tail = [];
    rows.forEach((r, i) => ((i < MAIN && r.min / total * 100 >= MIN_PCT) ? main : tail).push(r));
    if (tail.length) {
        const sum = tail.reduce((a, r) => a + r.min, 0);
        if (sum) main.push({ app: `其他 ${tail.length} 项`, min: sum, isTail: true });
    }

    const R = 54, W = 22, C = 2 * Math.PI * R;
    let offset = 0;
    let arcs = "", legend = "";
    main.forEach((r, i) => {
        const pct = r.min / total;
        const len = pct * C;
        const color = r.isTail ? "var(--line)" : COLORS[i % COLORS.length];
        // -90deg 让第一段从 12 点开始，符合读钟表的直觉
        arcs += `<circle cx="70" cy="70" r="${R}" fill="none" stroke="${color}"
                   stroke-width="${W}" stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}"
                   stroke-dashoffset="${(-offset).toFixed(2)}"
                   transform="rotate(-90 70 70)"><title>${r.app} ${r.min}分钟</title></circle>`;
        offset += len;
        legend += `<div class="alloc-item">
            <i style="background:${color}"></i>
            <span class="alloc-app">${r.app}</span>
            <span class="alloc-min num">${r.min}</span>
            <span class="alloc-pct num">${Math.round(pct * 100)}%</span>
        </div>`;
    });

    return `<div class="alloc${compact ? " alloc-compact" : ""}">
        <svg class="alloc-donut" viewBox="0 0 140 140" role="img" aria-label="时间分配">
            ${arcs}
            <text x="70" y="66" class="alloc-total num">${total}</text>
            <text x="70" y="84" class="alloc-unit">分钟</text>
        </svg>
        <div class="alloc-legend">${legend}</div>
    </div>`;
}

// --- 活动流：时间线 ---
// 模型给的时间写法不统一（**09:59｜应用**、[10:03]、09:59–10:00 都出现过），
// 统一抽出每条开头的第一个 HH:MM 当锚点，抽不到就退回原样渲染。
function renderActivityFlow(md) {
    const text = (md || "").trim();
    if (!text) return "";

    // 按「以时间开头的条目」切段
    const chunks = [];
    let cur = null;
    for (const raw of text.split("\n")) {
        const line = raw.replace(/\s+$/, "");
        // 认三种起头：- 、1. / 1) 、直接以时间开头；时间可能是区间 09:59–10:00
        const m = line.match(/^\s*(?:[-*]\s*|\d+[.)]\s*)?(?:\*\*)?\[?(\d{1,2}:\d{2})/);
        if (m) {
            if (cur) chunks.push(cur);
            cur = { time: m[1], head: "", body: [] };
            let rest = line.slice(line.indexOf(m[1]) + m[1].length);
            // 区间的后半段（–10:00）也去掉，时间列只显示起点
            rest = rest.replace(/^\s*[–—~-]\s*\d{1,2}:\d{2}/, "")
                       .replace(/^[\]）)｜|：:—–\-\s]+/, "");
            // 「**标题**：正文」这种把标题和正文拆开 ——
            // 合在一行会让每条都是一堵墙，扫不出重点
            const split = rest.match(/^(.*?\*\*)\s*[：:]\s*([\s\S]+)$/);
            if (split) {
                cur.head = split[1];
                cur.body.push(split[2]);
            } else {
                cur.head = rest;
            }
        } else if (cur) {
            cur.body.push(line);
        } else if (line.trim()) {
            chunks.push({ time: "", head: line, body: [] });
        }
    }
    if (cur) chunks.push(cur);
    if (!chunks.some(c => c.time)) return renderMarkdownInline(md);

    const items = chunks.map(c => {
        const head = renderInline(c.head);
        const body = c.body.join("\n").trim();
        return `<div class="flow-item">
            <div class="flow-time num">${c.time}</div>
            <div class="flow-dot"></div>
            <div class="flow-body">
                ${head ? `<div class="flow-head">${head}</div>` : ""}
                ${body ? renderMarkdownInline(body) : ""}
            </div>
        </div>`;
    }).join("");
    return `<div class="flow">${items}</div>`;
}

function renderInline(t) {
    return (t || "")
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/`([^`]+)`/g, "<code>$1</code>");
}


// 三个维度切换。用标签页而不是三张图并排：一屏放三个环形图谁也看不清，
// 而且大多数时候你只想看其中一个。
function initAllocTabs() {
    document.querySelectorAll(".alloc-tab").forEach(btn => {
        btn.addEventListener("click", () => {
            const box = btn.closest(".ai-section");
            box.querySelectorAll(".alloc-tab").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            box.querySelectorAll(".alloc-pane").forEach(p =>
                p.classList.toggle("hidden", p.dataset.dim !== btn.dataset.dim));
        });
    });
}


// --- 完整报告：按小节分派渲染 ---
// 原来整篇丢给 renderMarkdown，它把换行统统换成 <br>、不解析编号列表，
// 于是日总结塌成一大坨。现在每个 ## 单独成卡，按小节名选渲染方式。
function renderSections(md) {
    const text = (md || "").trim();
    if (!text) return "<p>无内容</p>";

    const parts = text.split(/^##\s+/m).filter(x => x.trim());
    if (parts.length < 2) return renderMarkdown(text);

    // 时间的三个维度合成一张带标签页的卡，不各占一块
    const dims = [];
    const cards = [];
    for (const part of parts) {
        const nl = part.indexOf("\n");
        const name = (nl < 0 ? part : part.slice(0, nl)).trim();
        const body = (nl < 0 ? "" : part.slice(nl + 1)).trim();
        if (!body) continue;

        if (name === "任务分配") { dims.push(["任务", body]); continue; }
        if (name === "工作性质") { dims.push(["工作性质", body]); continue; }
        if (name === "时间分配") { dims.push(["应用", body]); continue; }
        // 「具体内容」只是个容器，它下面的小节才是内容
        if (name === "具体内容") { cards.push(...splitInner(body)); continue; }
        cards.push([name, body]);
    }

    let html = "";
    if (dims.length) {
        // 任务放第一个 —— 它回答「做了哪个项目」，比「用了什么应用」有用
        dims.sort((a, b) => ["任务", "工作性质", "应用"].indexOf(a[0])
                          - ["任务", "工作性质", "应用"].indexOf(b[0]));
        html += `<div class="ai-section"><h2>时间分配</h2>
            <div class="alloc-tabs">${dims.map(([n], i) =>
                `<button class="alloc-tab${i ? "" : " active"}" data-dim="${i}">${n}</button>`).join("")}</div>
            ${dims.map(([, v], i) =>
                `<div class="alloc-pane${i ? " hidden" : ""}" data-dim="${i}">${renderAllocation(v)}</div>`).join("")}
        </div>`;
    }
    cards.sort((a, b) => sectionRank(a[0]) - sectionRank(b[0]));
    for (const [name, body] of cards) {
        html += `<div class="ai-section"><h2>${name}</h2>${renderSectionBody(name, body)}</div>`;
    }
    return html || renderMarkdown(text);
}

function splitInner(body) {
    const parts = body.split(/^##\s+/m).filter(x => x.trim());
    if (parts.length < 2 && !/^##\s/m.test(body)) return [["具体内容", body]];
    return parts.map(part => {
        const nl = part.indexOf("\n");
        return [(nl < 0 ? part : part.slice(0, nl)).trim(),
                (nl < 0 ? "" : part.slice(nl + 1)).trim()];
    }).filter(([, b]) => b);
}

function renderSectionBody(name, body) {
    // 带时间的小节走时间线：日总结的「这一天做了什么」和小时报告的
    // 「活动流」是同一种东西，不该长得不一样
    if (/(活动流|这一天做了什么|活动时间线)/.test(name)) return renderActivityFlow(body);
    // 待办和悬而未决是要「拿走去做」的，给方框标记和普通列表区分开
    if (/(待办|未完成|悬而未决)/.test(name)) {
        return `<div class="todo">${renderMarkdownInline(body)}</div>`;
    }
    return renderMarkdownInline(body);
}

// 小节的展示顺序。待办排在最前 —— 一天的总结里，
// 「明天要做什么」比「今天做了什么」更需要被先看到。
// 待办排最前 —— 一份总结里「接下来要做什么」比「已经做了什么」更需要先看到。
// 周报月报用的是「未完成」和「主线」，一并列进来，否则它们会被排到最后。
const SECTION_ORDER = [
    "待办", "未完成", "悬而未决",
    "这一天做了什么", "主线", "活动流",
    "关键事实", "阅读",
];

function sectionRank(name) {
    const i = SECTION_ORDER.findIndex(s => name.includes(s));
    return i < 0 ? SECTION_ORDER.length : i;
}


// --- 按需生成：范围选择 ---
// 三种周期共用一套外壳，只是选择器不同。默认值给最近一个完整周期，
// 想补历史就自己改日期。
function buildGenerator(type) {
    const today = new Date();
    const iso = d => d.toISOString().slice(0, 10);
    const box = document.createElement("div");
    box.className = "gen-box";

    let fields = "", label = "";
    if (type === "daily") {
        label = "日录";
        fields = `<input type="date" id="gen-date" value="${iso(today)}" max="${iso(today)}">`;
    } else if (type === "weekly") {
        label = "周录";
        const from = new Date(today); from.setDate(from.getDate() - 6);
        fields = `<input type="date" id="gen-start" value="${iso(from)}" max="${iso(today)}">
                  <span class="gen-sep">至</span>
                  <input type="date" id="gen-end" value="${iso(today)}" max="${iso(today)}">`;
    } else if (type === "monthly") {
        label = "月录";
        fields = `<input type="month" id="gen-month" value="${iso(today).slice(0, 7)}">`;
    } else {
        return null;
    }

    box.innerHTML = `<div class="gen-fields">${fields}</div>
                     <button class="gen-btn">生成${label}</button>`;
    box.querySelector(".gen-btn").addEventListener("click", e =>
        runSummary(type, e.target, label, collectRange(type)));
    return box;
}

function collectRange(type) {
    const val = id => document.getElementById(id)?.value || "";
    if (type === "daily") return { date: val("gen-date") };
    if (type === "weekly") return { start: val("gen-start"), end: val("gen-end") };
    if (type === "monthly") {
        const [year, month] = val("gen-month").split("-");
        return { year, month };
    }
    return {};
}
