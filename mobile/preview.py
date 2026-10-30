"""
用真实 390×844 视口把 H5 页面渲染成截图，便于在 Windows 上检查手机版式。

为什么用 iframe
---------------
Edge/Chrome 的 headless 模式在本机会忽略 ``--window-size`` 的宽度（实测布局视口
恒为 474px），直接截图会把右侧切掉。把页面放进一个固定 390px 宽的 iframe 里，
iframe 内部的布局视口就严格是 390px，再按 iframe 区域裁剪即可得到真实手机效果。

用法
----
python mobile/preview.py                 # 全部 5 页 × 深浅两套
python mobile/preview.py --tabs today    # 只截某一页
python mobile/preview.py --tabs holdings --seed seed.json   # 预置 localStorage 再截图

``--seed`` 用来验证「有数据时」的版式。file:// 下 localStorage 往往被禁用，
所以 seed 失败时页面会退回空态，看到空态就说明注入没生效，不是界面坏了。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WWW = ROOT / "mobile" / "app" / "assets" / "www"
SHOTS = ROOT / "mobile" / "shots"

EDGE_CANDIDATES = [
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
]

W, H = 390, 844
TALL = 2000


def find_browser() -> str:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("未找到 Edge / Chrome")


def shoot(browser: str, tab: str, theme: str, out: Path, h: int = H, seed: dict | None = None) -> None:
    src = f"index.html?theme={theme}#{tab}"
    seeder = None
    if seed is not None:
        import json
        seeder = WWW / "_seeder.html"
        seeder.write_text(
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body><script>\n"
            "try{ localStorage.setItem('etfq-hold', JSON.stringify(%s));\n"
            "     localStorage.setItem('etfq-theme','%s'); }catch(e){}\n"
            "location.replace('%s');\n</script></body></html>"
            % (json.dumps(seed, ensure_ascii=False), theme, src),
            encoding="utf-8",
        )
        src = "_seeder.html"
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;background:#20242c;}}
  iframe{{display:block;width:{W}px;height:{h}px;border:0;}}
</style></head><body>
<iframe src="{src}"></iframe>
</body></html>"""
    with tempfile.TemporaryDirectory() as td:
        wrap = Path(td) / "wrap.html"
        wrap.write_text(html, encoding="utf-8")
        # iframe 的 src 是相对路径，必须放在 www 目录下才能解析
        target = WWW / "_wrap.html"
        target.write_text(html, encoding="utf-8")
        try:
            raw = Path(td) / "raw.png"
            subprocess.run(
                [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 "--force-prefers-reduced-motion", "--virtual-time-budget=4000",
                 f"--window-size={W + 20},{h + 40}", f"--screenshot={raw}",
                 target.resolve().as_uri()],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not raw.exists():
                print(f"[ERR] 截图失败 {tab}/{theme}")
                return
            from PIL import Image
            im = Image.open(raw).convert("RGB")
            im.crop((0, 0, min(W, im.width), min(h, im.height))).save(out)
            print(f"[OK] {out.relative_to(ROOT)}  {im.size}")
        finally:
            target.unlink(missing_ok=True)
            if seeder is not None:
                seeder.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tabs", default="overview,today,holdings,research,more")
    ap.add_argument("--themes", default="dark,light")
    ap.add_argument("--height", type=int, default=H,
                    help="844 = 首屏；2000 = 整页长图")
    ap.add_argument("--seed", default="", help="JSON 文件：预置 localStorage 的 etfq-hold 后截图")
    args = ap.parse_args()

    browser = find_browser()
    SHOTS.mkdir(parents=True, exist_ok=True)
    seed = None
    if args.seed:
        import json
        seed = json.loads(Path(args.seed).read_text(encoding="utf-8"))
    for tab in args.tabs.split(","):
        for theme in args.themes.split(","):
            suffix = "" if args.height == H else "-full"
            shoot(browser, tab.strip(), theme.strip(),
                  SHOTS / f"{tab.strip()}-{theme.strip()}{suffix}.png", args.height, seed)


if __name__ == "__main__":
    main()
