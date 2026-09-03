#!/usr/bin/env python3
"""自检：装完之后到底哪一环没通。

写这个是因为这套东西的失败模式几乎全是静默的 ——
没有屏幕录制权限就截出一张纯桌面壁纸，没有辅助功能权限就只拿到窗口标题，
没有 API key 就每小时静静地失败一次。看日志才发现，往往已经过了三天。

权限按「可执行文件」授予，不是按项目授予：重建 venv 之后
python 的路径变了，之前授的权限就不算数。所以这里把实际路径打出来。
"""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

import env_file  # noqa: E402

env_file.load()

OK, BAD, WARN = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m"


def check_deps() -> bool:
    missing = []
    for mod, pkg in (
        ("yaml", "pyyaml"), ("schedule", "schedule"), ("PIL", "pillow"),
        ("httpx", "httpx"), ("AppKit", "pyobjc-framework-Cocoa"),
        ("ApplicationServices", "pyobjc-framework-ApplicationServices"),
        ("Quartz", "pyobjc-framework-Quartz"), ("Vision", "pyobjc-framework-Vision"),
        ("mcp", "mcp"),
    ):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"{BAD} 依赖缺失: {', '.join(missing)}")
        print(f"    修复: {sys.executable} -m pip install -r requirements.txt")
        return False
    print(f"{OK} 依赖齐全")
    return True


def check_screen_recording() -> bool:
    try:
        import Quartz
    except ImportError:
        return False
    if Quartz.CGPreflightScreenCaptureAccess():
        print(f"{OK} 屏幕录制权限")
        return True
    print(f"{BAD} 屏幕录制权限 —— 没有它只会截到桌面壁纸，看不见任何窗口")
    print("    系统设置 → 隐私与安全性 → 屏幕录制 → 添加下面这个可执行文件：")
    print(f"      {sys.executable}")
    return False


def check_accessibility() -> bool:
    try:
        import accessibility
    except ImportError:
        return False
    if accessibility.is_trusted():
        print(f"{OK} 辅助功能权限")
        return True
    print(f"{BAD} 辅助功能权限 —— 没有它读不到窗口内文本，只能退回 OCR（更慢更不准）")
    print("    系统设置 → 隐私与安全性 → 辅助功能 → 添加下面这个可执行文件：")
    print(f"      {sys.executable}")
    return False


def check_text_extraction() -> bool:
    """真的抓一次，光有权限不等于抓得到。"""
    try:
        import accessibility
        text, frags = accessibility.capture_screen_text()
    except Exception as e:
        print(f"{BAD} 无障碍文本抽取异常: {e}")
        return False
    if frags == 0:
        print(f"{WARN} 无障碍文本抽取: 当前前台窗口 0 个片段"
              "（可能是窗口已最小化，换个应用到前台再跑一次）")
        return False
    print(f"{OK} 无障碍文本抽取: {frags} 个片段 / {len(text)} 字")
    return True


def check_api_key() -> bool:
    import yaml
    cfg = yaml.safe_load((SCRIPT_DIR / "config.yaml").read_text())
    provider = cfg.get("api", {}).get("provider", "claude")

    if provider == "claude":
        import shutil
        import subprocess
        cli = shutil.which("claude")
        if not cli:
            # SDK 自带一份 CLI，找不到系统的那份不一定是问题
            try:
                import claude_agent_sdk  # noqa: F401
                print(f"{OK} 模型后端 claude（用 SDK 自带的 CLI）")
                return True
            except ImportError:
                print(f"{BAD} claude-agent-sdk 未安装")
                print(f"    修复: {sys.executable} -m pip install claude-agent-sdk")
                return False
        try:
            ver = subprocess.run([cli, "--version"], capture_output=True,
                                 text=True, timeout=15).stdout.strip()
        except Exception:
            ver = "?"
        print(f"{OK} 模型后端 claude（{ver}，用 Claude Code 订阅鉴权，无需 API key）")
        return True

    key = os.environ.get("ZHIPUAI_API_KEY") or os.environ.get("ZHIPU_API_KEY", "")
    if not key:
        print(f"{BAD} provider=zhipu 但未找到 ZHIPU_API_KEY —— 采集照常，分析会失败")
        print(f"    修复: 把 ZHIPU_API_KEY=你的key 写进 {SCRIPT_DIR / '.env'}")
        return False
    # 只回显首尾各 4 位，中间一律遮蔽
    print(f"{OK} 模型后端 zhipu（key {key[:4]}…{key[-4:]}）")
    return True


def check_privacy() -> bool:
    """隐私配置有没有真的生效。

    这些同样是静默失败：术语表没填就照样跑，只是人名直接出网；
    目录权限松了也不会报错，只是同机器的其他进程都能读。
    """
    import yaml
    ok_all = True
    cfg = yaml.safe_load((SCRIPT_DIR / "config.yaml").read_text())
    privacy = cfg.get("privacy", {})

    if not privacy.get("enabled"):
        print(f"{BAD} privacy.enabled 为 false —— 敏感窗口过滤整个关闭了")
        ok_all = False

    local_only = privacy.get("local_only_apps") or []
    print(f"{OK} 仅本地应用 {len(local_only)} 个（内容不发给模型）"
          if local_only else f"{WARN} 未配置 local_only_apps")

    import redact
    terms = redact.load_terms()
    n = sum(len(v) for v in terms.values())
    if n:
        print(f"{OK} 脱敏术语 {n} 个（{'、'.join(terms)}）")
    else:
        print(f"{WARN} 脱敏术语表为空 —— 人名、公司名会原样发给模型")
        print(f"    发现候选: {sys.executable} {SCRIPT_DIR / 'redact.py'}")
        print(f"    填进: {redact.TERMS_FILE}")
        ok_all = False

    loose = []
    for name in ("screenshots", "reports", "chroma_db", ".env", "redact.local.yaml"):
        target = SCRIPT_DIR / name
        if target.exists() and (target.stat().st_mode & 0o077):
            loose.append(name)
    if loose:
        print(f"{BAD} 权限过松，同机器其他进程可读: {', '.join(loose)}")
        print(f"    修复: chmod -R go-rwx {' '.join(loose)}")
        ok_all = False
    else:
        print(f"{OK} 数据目录权限仅自己可读")

    left = 0
    try:
        import capture
        left = capture.paused_seconds()
    except Exception:
        pass
    if left:
        print(f"{WARN} 采集已暂停，{left // 60} 分钟后恢复")
    return ok_all


def check_agent() -> bool:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    line = next((l for l in out.splitlines() if "gleaner" in l.lower() or "capturescreen" in l.lower()), None)
    if not line:
        print(f"{WARN} 采集服务未加载 —— 运行 bash start.sh 启动")
        return False
    pid, status, label = (line.split("\t") + ["", "", ""])[:3]
    if pid == "-":
        print(f"{BAD} 采集服务已注册但未在运行（上次退出码 {status}）")
        print(f"    看日志: tail -50 {SCRIPT_DIR / 'capture.log'}")
        return False
    print(f"{OK} 采集服务运行中 (PID={pid})")
    return True


def check_data() -> bool:
    shots = SCRIPT_DIR / "screenshots"
    if not shots.exists():
        print(f"{WARN} 还没有任何截图 —— 刚装好是正常的，等一个采集间隔")
        return False
    days = sorted(d for d in shots.iterdir() if d.is_dir())
    png = sum(len(list(d.glob("*.png"))) for d in days)
    txt = sum(len(list(d.glob("*.txt"))) for d in days)
    reports = len(list((SCRIPT_DIR / "reports").rglob("*.md"))) if (SCRIPT_DIR / "reports").exists() else 0
    print(f"{OK} 数据: {len(days)} 天 / {png} 张截图 / {txt} 份无障碍文本 / {reports} 份报告")
    if png and not txt:
        print(f"    {WARN} 有截图但没有文本 —— 辅助功能权限多半没生效，分析会退回图片模式（贵）")

    # 图片删掉之前必须已经被抽成文本，否则内容就永久没了
    try:
        import capture
        imgs = [f for f in shots.rglob("*") if f.suffix in (".png", ".jpg")]
        lack = [f for f in imgs if capture.needs_ocr(f)]
        img_mb = sum(f.stat().st_size for f in imgs) / 1024 / 1024
        txt_kb = sum(f.stat().st_size for f in shots.rglob("*")
                     if f.suffix in (".txt", ".ocr")) / 1024
        if lack:
            print(f"    {WARN} {len(lack)}/{len(imgs)} 张图还没抽成文本"
                  f"（每小时后台补 {40} 张；删图前也会兜底补一次）")
        else:
            print(f"    {OK} 文本覆盖完整：图片 {img_mb:.0f} MB / 文本 {txt_kb:.0f} KB，"
                  f"删图可省 {img_mb * 1024 / (img_mb * 1024 + txt_kb) * 100:.1f}%")
    except Exception:
        pass

    # 索引是加速层，不是数据 —— 落后了搜索会漏，但重建只要几秒
    try:
        import store
        db = store.connect()
        st = store.stats(db)
        db.close()
        stems = len({f.stem for f in shots.rglob("*")
                     if f.suffix in (".txt", ".ocr", ".meta")})
        size = store.DB_PATH.stat().st_size / 1024 / 1024 if store.DB_PATH.exists() else 0
        if stems and st["frames"] < stems * 0.9:
            print(f"    {WARN} 索引落后：{st['frames']}/{stems} 帧"
                  f"（重建: {sys.executable} {SCRIPT_DIR / 'store.py'}）")
        else:
            print(f"    {OK} 检索索引：{st['frames']} 帧 / {st['chars'] // 1000}k 字 / {size:.1f} MB")
    except Exception as e:
        print(f"    {WARN} 索引不可用，搜索会退回逐文件扫描：{e}")
    return True


def main() -> int:
    print(f"拾遗 · Gleaner 自检\n路径: {SCRIPT_DIR}\nPython: {sys.executable}\n")
    results = [
        check_deps(), check_screen_recording(), check_accessibility(),
        check_text_extraction(), check_api_key(), check_privacy(),
        check_agent(), check_data(),
    ]
    failed = results.count(False)
    print()
    print("全部通过。" if not failed else f"{failed} 项需要处理，照上面的提示逐条修。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
