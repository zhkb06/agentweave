# -*- coding: utf-8 -*-
"""把 docs/方案书.md 渲染为带封面/目录/排版的 HTML，再交给 Edge 打印为 PDF。

用法::

    py -3.14 scripts/build_pdf.py            # 生成 docs/方案书.html + docs/方案书.pdf
    py -3.14 scripts/build_pdf.py --no-pdf   # 只生成 HTML（便于浏览器预览）

依赖：markdown（仅构建期使用，不属于作品运行时依赖）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MD_PATH = DOCS / "方案书.md"
HTML_PATH = DOCS / "方案书.html"
PDF_PATH = DOCS / "方案书.pdf"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

CSS = """
@page { size: A4; margin: 17mm 16mm 18mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  font-size: 10.6pt; line-height: 1.72; color: #1f2937; margin: 0;
}
.cover { page-break-after: always; text-align: center; padding-top: 34mm; }
.cover .badge { display: inline-block; padding: 3px 14px; border-radius: 999px;
  background: #ecfdf5; color: #047857; font-size: 10pt; letter-spacing: 1px; }
.cover h1 { font-size: 25pt; line-height: 1.45; margin: 20mm 8mm 8mm; color: #0f172a; border: 0; }
.cover .sub { color: #475569; font-size: 12pt; margin-bottom: 14mm; }
.cover table { margin: 0 auto; width: 78%; border-collapse: collapse; font-size: 10.5pt; }
.cover table td { border: 0; border-bottom: 1px solid #e2e8f0; padding: 7px 10px; text-align: left; }
.cover table td:first-child { color: #64748b; width: 92px; text-align: right; }
.cover .foot { margin-top: 22mm; color: #94a3b8; font-size: 9.5pt; }
.toc { page-break-after: always; }
.toc h2 { border: 0; text-align: center; letter-spacing: 6px; page-break-before: avoid; }
.toc ol { list-style: none; padding-left: 0; }
.toc ol ol { padding-left: 18px; }
.toc li { margin: 3px 0; font-size: 10.5pt; }
.toc a { color: #1f2937; text-decoration: none; display: block; }
.toc .lvl2 > a { font-weight: 700; }
h1, h2, h3, h4 { color: #0f172a; line-height: 1.4; }
h1 { font-size: 21pt; }
h2 { font-size: 16pt; border-left: 5px solid #0ea5e9; padding-left: 10px;
     margin: 22px 0 12px; page-break-before: always; }
h3 { font-size: 13pt; margin: 18px 0 8px; color: #0c4a6e; }
h4 { font-size: 11.5pt; margin: 14px 0 6px; }
p { margin: 7px 0; text-align: justify; }
strong { color: #075985; }
a { color: #0369a1; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 14px; font-size: 9.6pt;
  page-break-inside: avoid; }
th, td { border: 1px solid #cbd5e1; padding: 5px 7px; vertical-align: top; }
th { background: #f1f5f9; color: #0f172a; }
tr:nth-child(even) td { background: #f8fafc; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 9pt;
  background: #f1f5f9; padding: 1px 4px; border-radius: 3px; }
pre { background: #0f172a; color: #e2e8f0; padding: 10px 12px; border-radius: 6px;
  white-space: pre-wrap; word-break: break-word; font-size: 8.8pt; line-height: 1.55;
  page-break-inside: avoid; }
pre code { background: transparent; color: inherit; padding: 0; }
blockquote { border-left: 4px solid #38bdf8; background: #f0f9ff; margin: 10px 0;
  padding: 8px 12px; color: #334155; }
img { max-width: 100%; border: 1px solid #e2e8f0; border-radius: 6px; margin: 6px 0; }
hr { border: 0; border-top: 1px solid #e2e8f0; margin: 18px 0; }
.footnote { color: #94a3b8; font-size: 9pt; }
"""


def find_browser() -> str | None:
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def build_toc(md, tokens: list[dict]) -> str:
    items: list[str] = []

    def walk(nodes: list[dict], depth: int) -> None:
        for node in nodes:
            if depth > 2:
                continue
            anchor = node.get("id") or ""
            text = html.escape(node["name"])
            items.append(
                f'<li class="lvl{node["level"]}"><a href="#{anchor}">{text}</a>'
            )
            if node.get("children"):
                items.append("<ol>")
                walk(node["children"], depth + 1)
                items.append("</ol>")
            items.append("</li>")

    walk(tokens, 1)
    return '<div class="toc"><h2>目 录</h2><ol>' + "".join(items) + "</ol></div>"


def wrap_cover(
    body: str,
    badge: str = "2026 第五届「未来杯」创新大赛 · 赛题一",
    footer: str = "方案书 · 生成于 {date} · 纯 Python 标准库实现，零第三方运行时依赖",
) -> str:
    """把标题、副标题与元信息表包成封面。"""
    match = re.search(
        r"<h1[^>]*>(.*?)</h1>\s*<p>(.*?)</p>\s*(<table>.*?</table>)",
        body,
        re.S,
    )
    if not match:
        return body
    title, subtitle, table = match.groups()
    cover = (
        '<div class="cover">'
        f'<div class="badge">{html.escape(badge)}</div>'
        f"<h1>{title}</h1>"
        f'<div class="sub">{subtitle}</div>'
        f"{table}"
        f'<div class="foot">{html.escape(footer.format(date=_dt.date.today().isoformat()))}</div>'
        "</div>"
    )
    return body[: match.start()] + cover + body[match.end():]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染 Markdown 为带封面的 HTML / PDF")
    parser.add_argument("--no-pdf", action="store_true", help="只生成 HTML")
    parser.add_argument("--md", default=None, help="源 Markdown（默认 docs/方案书.md）")
    parser.add_argument("--html", default=None, help="输出 HTML（默认与 md 同名 .html）")
    parser.add_argument("--pdf", default=None, help="输出 PDF（默认与 md 同名 .pdf）")
    parser.add_argument("--badge", default="2026 第五届「未来杯」创新大赛 · 赛题一", help="封面徽标文字")
    parser.add_argument("--footer", default="方案书 · 生成于 {date} · 纯 Python 标准库实现，零第三方运行时依赖", help="封面页脚文字，{date} 会被替换")
    parser.add_argument("--doc-title", default="织流 AgentWeave · 方案书", help="HTML <title>")
    args = parser.parse_args(argv)

    md_path = Path(args.md) if args.md else MD_PATH
    if not md_path.is_absolute():
        md_path = (ROOT / md_path).resolve()
    html_path = Path(args.html) if args.html else md_path.with_suffix(".html")
    pdf_path = Path(args.pdf) if args.pdf else md_path.with_suffix(".pdf")
    if not html_path.is_absolute():
        html_path = (ROOT / html_path).resolve()
    if not pdf_path.is_absolute():
        pdf_path = (ROOT / pdf_path).resolve()

    try:
        import markdown
    except ImportError:
        print("缺少 markdown 库：py -3.14 -m pip install markdown", file=sys.stderr)
        return 2

    source = md_path.read_text(encoding="utf-8")
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists", "md_in_html"],
        extension_configs={"toc": {"permalink": False, "toc_depth": "2-3"}},
    )
    body = md.convert(source)
    body = wrap_cover(body, badge=args.badge, footer=args.footer)
    toc = build_toc(md, md.toc_tokens)

    # 目录紧跟封面之后：插到第一个章节 h2 之前
    first_h2 = body.find("<h2")
    if first_h2 > 0:
        body = body[:first_h2] + toc + body[first_h2:]

    # 注意：HTML 放在 docs/ 下，图片相对路径 ../screenshots/... 可直接解析
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(args.doc_title)}</title>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""
    html_path.write_text(page, encoding="utf-8")
    print(f"HTML 已生成：{html_path}（{len(page) // 1024} KB）")

    if args.no_pdf:
        return 0

    browser = find_browser()
    if not browser:
        print("未找到 Edge/Chrome，已跳过 PDF。", file=sys.stderr)
        return 1
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        "--virtual-time-budget=12000",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]
    print("打印 PDF：", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not pdf_path.is_file():
        print(result.stdout[-2000:], result.stderr[-2000:], file=sys.stderr)
        return 1
    print(f"PDF 已生成：{pdf_path}（{pdf_path.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
