"""研报 PDF 生成模块（生产级 · 纯 reportlab 4.x 实现，无系统依赖）。

链路：
    Markdown 文本
      →（Python-markdown）→ HTML 片段
      →（自研轻量 HTML → platypus Flowables 转换器）→ reportlab Flowables 列表
      →（BaseDocTemplate + 两套 PageTemplate：封面 / 正文）
      → 最终 PDF 文件

生产级特性：
* 封面页：紫蓝纯色背景 + 品牌标 + 报告大标题 + 元信息 2×2 卡片 + 风险提示
* 正文页：页眉（左"股票研报助手 · AI" / 右 股票标题）、页脚（居中 "第 N / M 页"）
* Markdown 渲染：h1~h4 / p / strong / em / ul / ol / table / pre / code / blockquote / a / sup
* 参考来源：自动将 `{title, source(url)}` 渲染为编号列表，URL 为可点击链接
* 免责声明：末尾附独立高亮区块
* 中文字体：自动发现并注册 macOS / Linux / Windows 常见 CJK 字体
  （PingFang / STHeiti / Hiragino Sans GB / Noto CJK / 微软雅黑 / SimHei / Arial Unicode）
* 文件名：sanitize + 日期 + hash 防覆盖
* 输出目录：`data/pdfs`，自动创建；下载 URL 前缀 `/api/stock_research/download/`
"""

from __future__ import annotations

import functools
import hashlib
import html as _html
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from app.config import settings, BASE_DIR


# —— 文件名安全 ——

# 允许中文、字母、数字、下划线、连字符、括号、点号
_SAFE_FILENAME_RE = re.compile(r"[^\w\u4e00-\u9fff\-\(\)\.]+", flags=re.UNICODE)


def _extract_title_from_markdown(md: str) -> str:
    """从 Markdown 正文提取首个 H1 标题作为报告名。仅匹配 '# ' 开头的一级标题。"""
    if not md:
        return ""
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            if title:
                return title
    return ""


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """清理文件名：保留中英文、数字、括号、连字符，其余替换为下划线。"""
    cleaned = _SAFE_FILENAME_RE.sub("_", name).strip("_")[:max_len]
    if not cleaned:
        cleaned = "report"
    # 去除连续下划线
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    suffix = datetime.now().strftime("%Y%m%d")
    return f"{cleaned}_{suffix}.pdf"


# —— 中文字体发现与注册（reportlab 4.x）——

_CJK_FONT_CANDIDATES = [
    # —— macOS ——
    ("/System/Library/Fonts/PingFang.ttc", "PingFangSC"),
    ("/System/Library/Fonts/STHeiti Medium.ttc", "STHeiti"),
    ("/System/Library/Fonts/STHeiti Light.ttc", "STHeitiLight"),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", "HiraginoSansGB"),
    ("/Library/Fonts/Supplemental/Arial Unicode.ttf", "ArialUnicode"),
    ("/Library/Fonts/Arial Unicode.ttf", "ArialUnicode"),
    # —— Linux ——
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "NotoSansCJK"),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "NotoSansCJK"),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "WenQuanYi"),
    # —— Windows ——
    ("C:/Windows/Fonts/msyh.ttc", "MicrosoftYaHei"),
    ("C:/Windows/Fonts/msyh.ttf", "MicrosoftYaHei"),
    ("C:/Windows/Fonts/simhei.ttf", "SimHei"),
]


@dataclass
class _ResolvedFont:
    family_name: str
    font_path: str


def _discover_cjk_font() -> _ResolvedFont | None:
    for fp, name in _CJK_FONT_CANDIDATES:
        if Path(fp).is_file():
            return _ResolvedFont(family_name=name, font_path=fp)
    return None


@functools.lru_cache(maxsize=1)
def _register_cjk_font() -> str:
    """注册中文字体到 reportlab。返回 CSS/Paragraph 中可使用的 fontName。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    resolved = _discover_cjk_font()
    if resolved is None:
        return "Helvetica"

    if resolved.family_name in set(pdfmetrics.getRegisteredFontNames()):
        return resolved.family_name

    # reportlab 4.x 的 TTFont 对 TTC 支持 faceIndex / subfontIndex 不同名
    is_ttc = resolved.font_path.lower().endswith(".ttc")
    last_err: Exception | None = None
    for kw in ({"faceIndex": 0}, {"subfontIndex": 0}, {"faceIndex": 1}, {}):
        if not is_ttc and kw:
            # TTF 不要 index 参数
            continue
        try:
            if kw:
                pdfmetrics.registerFont(TTFont(resolved.family_name, resolved.font_path, **kw))
            else:
                pdfmetrics.registerFont(TTFont(resolved.family_name, resolved.font_path))
            return resolved.family_name
        except Exception as e:  # noqa: PERF203
            last_err = e
    # 所有 index 尝试失败
    if last_err:
        print(f"[pdf_report] 注册中文字体失败：{resolved.font_path} {last_err}")
    return "Helvetica"


# —— 主题色 / 字体 / 尺寸常量 ——

class _Theme:
    primary = (0x4F / 255, 0x46 / 255, 0xE5 / 255)   # #4f46e5
    primary_light = (0xEE / 255, 0xF2 / 255, 1.0)
    primary_text = (0x1E / 255, 0x1B / 255, 0x4E / 255)
    warn = (0xF5 / 255, 0x9E / 255, 0x0B / 255)
    warn_bg = (1.0, 0xFB / 255, 0xEB / 255)
    warn_text = (0x78 / 255, 0x35 / 255, 0x0F / 255)
    disclaimer_bg = (1.0, 0xF7 / 255, 0xED / 255)
    disclaimer_border = (0xFD / 255, 0xBA / 255, 0x74 / 255)
    disclaimer_text = (0x7C / 255, 0x2D / 255, 0x12 / 255)
    sources_bg = (0xF8 / 255, 0xFA / 255, 0xFC / 255)
    sources_border = (0xE2 / 255, 0xE8 / 255, 0xF0 / 255)
    table_border = (0xCB / 255, 0xD5 / 255, 0xE1 / 255)
    table_head_bg = primary_light
    table_alt_bg = (0xF8 / 255, 0xFA / 255, 0xFC / 255)
    muted = (0x64 / 255, 0x74 / 255, 0x8B / 255)
    link = (0x25 / 255, 0x63 / 255, 0xEB / 255)
    white = (1, 1, 1)
    white_soft = (0xE0 / 255, 0xE7 / 255, 0xFF / 255)
    white_dim = (0xC7 / 255, 0xD2 / 255, 0xFE / 255)


_A4_W = 595.28   # points
_A4_H = 841.89

PAGE_MARGIN_L = 45   # ~16mm
PAGE_MARGIN_R = 45
PAGE_MARGIN_T = 56   # ~20mm
PAGE_MARGIN_B = 68   # ~24mm


@dataclass
class PdfReportResult:
    file_path: Path
    file_name: str
    download_url: str
    file_size_kb: float


# —— 结构化板块生成 ——

def _build_quote_snapshot(tool_data: list[dict[str, Any]], *, cjk: str) -> list:
    """从工具调用数据中提取行情快照（当前价、涨跌幅等），生成概览卡片。"""
    from reportlab.platypus import Paragraph, Table, TableStyle, Spacer, KeepTogether
    from reportlab.lib.styles import ParagraphStyle

    quote_info: dict[str, Any] = {}
    for td in tool_data:
        if td.get("tool") == "get_stock_quote":
            try:
                out = td.get("output", "")
                if out:
                    import json as _json
                    d = _json.loads(out) if isinstance(out, str) and out.strip().startswith("{") else {}
                    if isinstance(d, dict):
                        quote_info = d
            except Exception:
                pass

    if not quote_info:
        return []

    fields = []
    label_map = {
        "name": "名称", "code": "代码", "current_price": "最新价",
        "change_percent": "涨跌幅", "open_price": "开盘价",
        "high_price": "最高", "low_price": "最低", "prev_close": "昨收",
        "volume": "成交量", "turnover": "成交额", "market_cap": "总市值",
        "pe_ratio": "市盈率", "pb_ratio": "市净率",
    }
    for k, label in label_map.items():
        if k in quote_info and quote_info[k] is not None:
            v = quote_info[k]
            if isinstance(v, float):
                v = f"{v:.2f}"
            elif isinstance(v, (int,)) and k in ("volume", "turnover", "market_cap"):
                if v >= 1e8:
                    v = f"{v / 1e8:.2f}亿"
                elif v >= 1e4:
                    v = f"{v / 1e4:.2f}万"
                else:
                    v = str(v)
            elif k == "change_percent" and isinstance(v, (int, float)):
                v = f"{v:+.2f}%"
            fields.append((label, str(v)))

    if not fields:
        return []

    title_style = ParagraphStyle("qsnap", fontName=cjk, fontSize=12, leading=18, textColor=_Theme.primary_text)
    label_style = ParagraphStyle("qlab", fontName=cjk, fontSize=9, leading=13, textColor=_Theme.muted)
    val_style = ParagraphStyle("qval", fontName=cjk, fontSize=11, leading=16, textColor=_Theme.primary_text)

    title = Paragraph("📊 行情快照", title_style)
    cells = [title]
    cells.append(Spacer(1, 6))

    col_count = 3
    row_cells = []
    for i in range(0, len(fields), col_count):
        group = fields[i:i + col_count]
        row = []
        for label, val in group:
            label_p = Paragraph(_escape_rl_text(label), label_style)
            val_p = Paragraph(f'<b>{_escape_rl_text(val)}</b>', val_style)
            pair = Table(
                [[label_p], [val_p]],
                colWidths=[_usable_width() / col_count - 10],
            )
            pair.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            row.append(pair)
        while len(row) < col_count:
            row.append("")
        row_cells.append(row)

    grid = Table(row_cells, colWidths=[_usable_width() / col_count] * col_count)
    grid.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _Theme.sources_bg),
        ("BOX", (0, 0), (-1, -1), 0.5, _Theme.sources_border),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, _Theme.sources_border),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    cells.append(grid)
    return [KeepTogether(Table([[cells]], colWidths=[_usable_width()]))]


def _build_data_sources_section(tool_data: list[dict[str, Any]], *, cjk: str) -> list:
    """生成「数据来源说明」板块：列出本次使用的工具数据。"""
    from reportlab.platypus import Paragraph, Table, TableStyle, Spacer, KeepTogether
    from reportlab.lib.styles import ParagraphStyle

    if not tool_data:
        return []

    tool_names = sorted({td.get("tool", "") for td in tool_data if td.get("tool")})
    if not tool_names:
        return []

    title_style = ParagraphStyle("dss", fontName=cjk, fontSize=10.5, leading=15, textColor=_Theme.primary)
    item_style = ParagraphStyle("dsi", fontName=cjk, fontSize=9, leading=14, textColor=_Theme.muted)

    items = [Paragraph("📋 数据来源说明", title_style), Spacer(1, 4)]
    tool_labels = {
        "get_stock_quote": "实时行情",
        "get_stock_kline": "K线技术指标",
        "get_financial_data": "财务数据",
        "get_stock_news": "新闻舆情",
        "get_money_flow": "资金流向",
        "get_stock_info": "公司基本信息",
        "search_stock_code": "股票检索",
    }
    for name in tool_names:
        label = tool_labels.get(name, name)
        items.append(Paragraph(f"· {label}  [来源: {name}]", item_style))

    t = Table([[items]], colWidths=[_usable_width()])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _Theme.sources_bg),
        ("BOX", (0, 0), (-1, -1), 0.6, _Theme.sources_border),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return [KeepTogether(t)]


def _build_tool_data_table(tool_data: list[dict[str, Any]], *, cjk: str) -> list:
    """将工具调用数据摘要以表格形式呈现（输入 → 输出概述）。"""
    from reportlab.platypus import Paragraph, Table, TableStyle, Spacer, KeepTogether
    from reportlab.lib.styles import ParagraphStyle

    if not tool_data:
        return []

    title_style = ParagraphStyle("tdt", fontName=cjk, fontSize=12, leading=18, textColor=_Theme.primary_text)
    cell_style = ParagraphStyle("tdc", fontName=cjk, fontSize=8.5, leading=13)
    cell_bold = ParagraphStyle("tdcb", fontName=cjk, fontSize=8.5, leading=13)

    header = [
        Paragraph("<b>工具</b>", cell_bold),
        Paragraph("<b>输入参数</b>", cell_bold),
        Paragraph("<b>结果摘要</b>", cell_bold),
    ]
    rows = [header]
    MAX_ROWS = 8
    for td in tool_data[:MAX_ROWS]:
        tool_name = td.get("tool", "")
        tool_labels = {
            "get_stock_quote": "📈 实时行情",
            "get_stock_kline": "📊 K线数据",
            "get_financial_data": "💰 财务数据",
            "get_stock_news": "📰 新闻舆情",
            "get_money_flow": "💹 资金流向",
            "get_stock_info": "🏢 公司信息",
            "search_stock_code": "🔍 股票检索",
        }
        label = tool_labels.get(tool_name, tool_name)
        inp = _escape_rl_text((td.get("input") or "")[:80])
        out = _escape_rl_text((td.get("output") or "")[:120])
        rows.append([
            Paragraph(f'<b>{label}</b>', cell_style),
            Paragraph(inp, cell_style),
            Paragraph(out, cell_style),
        ])

    if len(tool_data) > MAX_ROWS:
        rows.append([
            Paragraph(f"...（共 {len(tool_data)} 条工具调用，仅展示前 {MAX_ROWS} 条）", cell_style),
            "", "",
        ])

    title = Paragraph("🔧 工具调用摘要", title_style)
    col_widths = [_usable_width() * 0.22, _usable_width() * 0.30, _usable_width() * 0.48]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), cjk),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.4, _Theme.table_border),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, 0), _Theme.table_head_bg),
        ("FONTNAME", (0, 0), (-1, 0), cjk),
        ("FONTNAME", (0, 1), (-1, -1), cjk),
    ]))
    return [title, Spacer(1, 4), KeepTogether(t), Spacer(1, 10)]


# —— 轻量 HTML → reportlab platypus Flowables 转换器 ——
# 支持标签：h1/h2/h3/h4, p, div, ul/ol/li, table/thead/tbody/tr/th/td,
#           pre/code, blockquote, a, strong/b, em/i, sup, br, span
# 不支持：嵌套表格、img（以后可扩展）、style 属性里的复杂 CSS

class _MdHtmlToFlowables(HTMLParser):
    def __init__(self, *, cjk_font: str):
        super().__init__(convert_charrefs=True)
        self.cjk = cjk_font
        self.flowables: list[Any] = []
        # 嵌套栈：每个元素一层 {tag, attrs, text_parts[], kids[]}
        # 顶层我们把 p/div/inline 混排压成 Paragraph；ul/ol/table 有专门的 kid 累积
        self._stack: list[dict] = []
        # 当前正在拼接的 inline buffer（用于段落级标签内部）
        # 用 reportlab 的 mini XML：<b> <i> <super> <font> <a href>
        self._buf: list[str] = []
        # 段落级元素的标签
        self._block_tags = {"h1", "h2", "h3", "h4", "p", "div", "blockquote", "pre", "li"}
        self._list_tags = {"ul", "ol"}
        self._table_tags = {"table", "thead", "tbody", "tr", "th", "td"}

    # —— 工具：把当前 inline buffer 拼成 mini XML 字符串 ——
    def _flush_buf(self) -> str:
        s = "".join(self._buf).strip("\n")
        # 去掉多余的空白行
        s = re.sub(r"\n{3,}", "\n\n", s)
        self._buf = []
        return s

    # —— inline 样式辅助 ——
    @staticmethod
    def _font_open(color: tuple | None = None, size: int | None = None, face: str | None = None) -> str:
        attrs = []
        if face:
            attrs.append(f'face="{face}"')
        if size is not None:
            attrs.append(f'size="{size}"')
        if color is not None:
            r, g, b = color
            hexcolor = f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
            attrs.append(f'color="{hexcolor}"')
        if not attrs:
            return ""
        return "<font " + " ".join(attrs) + ">"

    @staticmethod
    def _font_close() -> str:
        return "</font>"

    # —— HTMLParser 钩子 ——
    def handle_starttag(self, tag: str, attrs_list):
        attrs = dict(attrs_list)
        tag = tag.lower()

        # —— Table 分支 ——
        if tag in self._table_tags:
            self._start_table_tag(tag, attrs)
            return

        # —— List 分支：ul/ol ——
        if tag in self._list_tags:
            # 任何打开的段落先 flush 为 paragraph
            self._flush_paragraph()
            self._stack.append({"tag": tag, "attrs": attrs, "items": []})
            return

        # —— List item ——
        if tag == "li":
            self._flush_paragraph()
            self._stack.append({"tag": "li", "attrs": attrs, "buf": []})
            # 给 item 内部准备一个新的 inline 上下文
            # 我们把 self._buf 暂存到栈？简化：用 stack[-1]['buf'] 代替 self._buf 写；读也从 stack[-1]['buf']
            # 所以重定向：push 当前 self._buf 到 stack[-2] 里暂存
            parent = self._stack[-2] if len(self._stack) >= 2 else None
            saved = {"tag": "__saved_buf__", "buf": self._buf}
            self._stack.append(saved)
            self._buf = []
            return

        # —— Blockquote ——
        if tag == "blockquote":
            self._flush_paragraph()
            self._stack.append({"tag": "blockquote", "attrs": attrs, "buf": [], "paragraphs": []})
            saved = {"tag": "__saved_buf__", "buf": self._buf}
            self._stack.append(saved)
            self._buf = []
            return

        # —— Pre / Code ——
        if tag == "pre":
            self._flush_paragraph()
            self._stack.append({"tag": "pre", "attrs": attrs, "buf": []})
            saved = {"tag": "__saved_buf__", "buf": self._buf}
            self._stack.append(saved)
            self._buf = []
            return

        if tag == "code":
            # <code> inside <pre>：不套额外样式，交给 pre 的字体
            # standalone <code>：用 Courier 等宽 + 深色
            in_pre = any(s.get("tag") == "pre" for s in self._stack if isinstance(s, dict))
            if in_pre:
                return
            self._buf.append('<font face="Courier" size="9" color="#0f172a">')
            return

        # —— Headings / Paragraph ——
        if tag in {"h1", "h2", "h3", "h4", "p", "div"}:
            self._flush_paragraph()
            self._stack.append({"tag": tag, "attrs": attrs})
            return

        # —— Inline tags ——
        if tag == "strong" or tag == "b":
            self._buf.append(self._font_open(color=_Theme.primary, face=self.cjk))
            self._buf.append("<b>")
            return
        if tag == "em" or tag == "i":
            self._buf.append(self._font_open(color=(0x0F / 255, 0x76 / 255, 0x6E / 255), face=self.cjk))
            # 中文字体没 italic，用加粗模拟强调
            self._buf.append("<b>")
            return
        if tag == "sup":
            self._buf.append("<super>")
            return
        if tag == "a":
            href = attrs.get("href", "")
            if href:
                self._buf.append(f'<a href="{_html.escape(href, quote=True)}" color="#2563eb"><u>')
            else:
                self._buf.append(self._font_open(color=_Theme.link))
            return
        if tag == "br":
            self._buf.append("<br/>")
            return
        if tag == "span":
            # 忽略，不做处理
            return
        # 其他未知 inline：跳过开始标签
        return

    def handle_endtag(self, tag: str):
        tag = tag.lower()

        # —— Table 分支 ——
        if tag in self._table_tags:
            self._end_table_tag(tag)
            return

        # —— List item ——
        if tag == "li":
            # pop __saved_buf__
            while self._stack and self._stack[-1].get("tag") == "__saved_buf__":
                outer = self._stack.pop()
                self._buf = outer["buf"]
            # pop li
            li = None
            if self._stack and self._stack[-1].get("tag") == "li":
                li = self._stack.pop()
            # 取 li 的 inline 内容（其实在上面的 saved 恢复之前已经从 self._buf 读不到了？不对：
            # 简化方案：把上面 push saved 之前那个 li 节点我们就用 stack 上的 li['buf'] 存
            # 但我们上面把 li 写 inline 到了 self._buf，然后 saved 复原…… 这样逻辑太绕。
            # 改为：直接在 self._buf 里累积，当遇到 </li> 时：把 self._buf flush 出来，push 到父 list 的 items。
            text = self._flush_buf()
            # 找到父 ul/ol
            for s in reversed(self._stack):
                if s.get("tag") in self._list_tags:
                    s["items"].append(text)
                    break
            return

        # —— List 结束 ——
        if tag in self._list_tags:
            self._flush_paragraph()
            node = None
            if self._stack and self._stack[-1].get("tag") in self._list_tags:
                node = self._stack.pop()
            if node is None:
                return
            from reportlab.platypus import ListFlowable, ListItem, Paragraph
            from reportlab.lib.styles import ParagraphStyle
            items_flo = []
            bullet = "bulletChar" if node["tag"] == "ul" else "1"
            style = ParagraphStyle(
                f"li-{node['tag']}", fontName=self.cjk, fontSize=10.5, leading=18,
            )
            for item_text in node.get("items", []):
                if not item_text.strip():
                    continue
                items_flo.append(
                    ListItem(Paragraph(_make_safe_rl_xml(item_text, self.cjk), style), leftIndent=0)
                )
            if not items_flo:
                return
            lf = ListFlowable(
                items_flo,
                bulletType=bullet,
                start="1" if node["tag"] == "ol" else "bulletchar",
                bulletFontSize=9,
                leftIndent=18,
            )
            self.flowables.append(lf)
            return

        # —— Blockquote 结束 ——
        if tag == "blockquote":
            # pop saved
            while self._stack and self._stack[-1].get("tag") == "__saved_buf__":
                outer = self._stack.pop()
                self._buf = outer["buf"]
            if self._stack and self._stack[-1].get("tag") == "blockquote":
                _ = self._stack.pop()
            text = self._flush_buf()
            if not text.strip():
                return
            from reportlab.platypus import Paragraph, KeepTogether
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.lib.units import mm
            style = ParagraphStyle(
                "blockq", fontName=self.cjk, fontSize=9.5, leading=17,
                textColor=_Theme.warn_text, backColor=_Theme.warn_bg,
                borderPadding=(8, 10, 8, 14),
                borderColor=_Theme.warn,
                borderWidth=0,
                leftIndent=0,
            )
            # 用 Paragraph + Table 做左边框
            inner = Paragraph(_make_safe_rl_xml(text, self.cjk), style)
            from reportlab.platypus import Table, TableStyle
            t = Table([[inner]], colWidths=[_usable_width()])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), _Theme.warn_bg),
                ("BOX", (0, 0), (-1, -1), 0, _Theme.warn),
                ("LINEBEFORE", (0, 0), (0, -1), 3 * mm / 2.83, _Theme.warn),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]))
            self.flowables.append(KeepTogether(t))
            return

        # —— Pre / Code ——
        if tag == "pre":
            while self._stack and self._stack[-1].get("tag") == "__saved_buf__":
                outer = self._stack.pop()
                self._buf = outer["buf"]
            if self._stack and self._stack[-1].get("tag") == "pre":
                _ = self._stack.pop()
            text_raw = "".join(self._buf)
            self._buf = []
            # 转义 reportlab XML：< / > / &
            safe = _escape_rl_text(text_raw)
            from reportlab.platypus import Paragraph, KeepTogether
            from reportlab.lib.styles import ParagraphStyle
            style = ParagraphStyle(
                "pre", fontName="Courier", fontSize=8.5, leading=13,
                textColor=(0xE2 / 255, 0xE8 / 255, 0xF0 / 255),
                backColor=(0x0F / 255, 0x17 / 255, 0x2A / 255),
                borderPadding=(8, 10),
            )
            # xhtml2pdf 里的 pre 是保留换行的，Paragraph 默认会吃掉换行 → 加 <br/>
            safe_lines = safe.split("\n")
            safe_para = "<br/>".join(safe_lines)
            inner = Paragraph(f'<font face="Courier">{safe_para}</font>', style)
            from reportlab.platypus import Table, TableStyle
            t = Table([[inner]], colWidths=[_usable_width()])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), (0x0F / 255, 0x17 / 255, 0x2A / 255)),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            self.flowables.append(KeepTogether(t))
            return

        if tag == "code":
            in_pre = any(s.get("tag") == "pre" for s in self._stack if isinstance(s, dict))
            if in_pre:
                return
            self._buf.append("</font>")
            return

        # —— Inline 关闭 ——
        if tag in ("strong", "b"):
            self._buf.append("</b></font>")
            return
        if tag in ("em", "i"):
            self._buf.append("</b></font>")
            return
        if tag == "sup":
            self._buf.append("</super>")
            return
        if tag == "a":
            # 简单处理：不管之前 href 是否有，都关闭 u/font/a
            self._buf.append("</u></a>")
            return

        # —— Heading / Paragraph / Div ——
        if tag in {"h1", "h2", "h3", "h4", "p", "div"}:
            self._flush_paragraph(tag_override=tag)
            return

        # 其他：忽略
        return

    def handle_data(self, data: str):
        if not data:
            return
        # Table 上下文内直接交给 table 模块处理（self._append_table_cell_text）
        if any(s.get("tag") in self._table_tags for s in self._stack if isinstance(s, dict)):
            self._append_table_cell_text(data)
            return
        # 转义后入 buffer
        safe = _escape_rl_text(data)
        self._buf.append(safe)

    def handle_entityref(self, name: str):
        mapping = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"'}
        ch = mapping.get(name)
        if ch:
            self.handle_data(ch)

    # —— Table 状态机 ——
    def _start_table_tag(self, tag: str, attrs):
        if tag == "table":
            self._flush_paragraph()
            self._stack.append({
                "tag": "table",
                "attrs": attrs,
                "rows": [],         # list[list[dict{text_parts, is_header, colspan}]]
                "current_row": None,
                "head_rows": 0,
                "in_head": False,
            })
            return
        top = self._stack[-1] if self._stack else None
        if not top or top.get("tag") not in self._table_tags and top.get("tag") != "table":
            return
        if tag == "thead":
            top["in_head"] = True
            return
        if tag == "tbody":
            top["in_head"] = False
            return
        if tag == "tr":
            top["current_row"] = []
            return
        if tag in ("th", "td"):
            colspan = 1
            try:
                c = attrs.get("colspan")
                if c:
                    colspan = max(1, int(c))
            except Exception:
                colspan = 1
            cell = {"text_parts": [], "is_header": (tag == "th"), "colspan": colspan}
            top["current_row"].append(cell)
            top["_current_cell"] = cell
            return

    def _append_table_cell_text(self, data: str):
        if not self._stack:
            return
        top = self._stack[-1]
        cell = top.get("_current_cell") if isinstance(top, dict) else None
        if cell is not None:
            cell["text_parts"].append(data)

    def _end_table_tag(self, tag: str):
        if not self._stack:
            return
        top = self._stack[-1]
        if not isinstance(top, dict):
            return
        if tag == "thead":
            top["in_head"] = False
            return
        if tag == "tbody":
            top["in_head"] = False
            return
        if tag == "tr":
            row = top.get("current_row") or []
            top["rows"].append(row)
            if top.get("in_head"):
                top["head_rows"] = len(top["rows"])
            top["current_row"] = None
            top["_current_cell"] = None
            return
        if tag in ("th", "td"):
            top["_current_cell"] = None
            return
        if tag == "table":
            self._stack.pop()
            rows = top.get("rows", [])
            head_rows = top.get("head_rows", 0)
            if not rows:
                return
            # 计算列数：按每行的 colspan 之和的最大值
            max_cols = 0
            for row in rows:
                cols = sum(c.get("colspan", 1) for c in row)
                max_cols = max(max_cols, cols)
            if max_cols == 0:
                return
            usable_w = _usable_width()
            col_w = usable_w / max_cols
            col_widths = [col_w] * max_cols
            # 把 cells 渲染成 Paragraph / strings
            from reportlab.platypus import Table, TableStyle, Paragraph, KeepTogether
            from reportlab.lib.styles import ParagraphStyle
            style_cell = ParagraphStyle(
                "td", fontName=self.cjk, fontSize=9.5, leading=14,
            )
            style_head = ParagraphStyle(
                "th", fontName=self.cjk, fontSize=9.5, leading=14,
                textColor=_Theme.primary,
            )
            data_rows = []
            for row in rows:
                rendered_cells = []
                for cell in row:
                    text = "".join(cell.get("text_parts", [])).strip()
                    text_safe = _escape_rl_text(text)
                    if cell.get("is_header"):
                        rendered_cells.append(Paragraph(f"<b>{text_safe}</b>" if text_safe else "", style_head))
                    else:
                        rendered_cells.append(Paragraph(text_safe, style_cell))
                # 如果该行 cell 数少于 max_cols，补空字符串
                while len(rendered_cells) < max_cols:
                    rendered_cells.append("")
                data_rows.append(rendered_cells)
            t = Table(data_rows, colWidths=col_widths, repeatRows=max(1, head_rows))
            style_cmds = [
                ("FONTNAME", (0, 0), (-1, -1), self.cjk),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, _Theme.table_border),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
            if head_rows > 0:
                style_cmds.append(("BACKGROUND", (0, 0), (-1, head_rows - 1), _Theme.table_head_bg))
                style_cmds.append(("FONTNAME", (0, 0), (-1, head_rows - 1), self.cjk))
            # 奇偶行底色
            for i in range(head_rows, len(data_rows)):
                if (i - head_rows) % 2 == 1:
                    style_cmds.append(("BACKGROUND", (0, i), (-1, i), _Theme.table_alt_bg))
            t.setStyle(TableStyle(style_cmds))
            self.flowables.append(KeepTogether(t))
            return

    # —— Paragraph flush ——
    def _flush_paragraph(self, tag_override: str | None = None):
        # 如果栈顶是 heading/p/div 这类 block tag，先 pop 决定 style
        tag = tag_override
        if tag is None and self._stack and self._stack[-1].get("tag") in {"h1", "h2", "h3", "h4", "p", "div"}:
            tag = self._stack[-1]["tag"]
            self._stack.pop()
        text = self._flush_buf()
        if not text.strip() and tag != "p":
            return
        from reportlab.platypus import Paragraph
        from reportlab.lib.styles import ParagraphStyle
        if tag == "h1":
            style = ParagraphStyle(
                "h1", fontName=self.cjk, fontSize=20, leading=28,
                textColor=_Theme.primary_text, fontColor=_Theme.primary_text,
                spaceAfter=10, spaceBefore=18,
                borderPadding=(0, 0, 5, 0),
            )
            # 额外：h1 画底部紫色边框，用 Paragraph 之后的 spacer + 一条横线 Table 实现
            p = Paragraph(_h_inline(text, 1, self.cjk), style)
            self.flowables.append(p)
            # 2px 紫色底边线
            from reportlab.platypus import Spacer, Table, TableStyle
            self.flowables.append(Spacer(1, 4))
            line = Table([[""]], colWidths=[_usable_width()], rowHeights=[2])
            line.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _Theme.primary)]))
            self.flowables.append(line)
            self.flowables.append(Spacer(1, 6))
            return
        if tag == "h2":
            style = ParagraphStyle(
                "h2", fontName=self.cjk, fontSize=14, leading=20,
                textColor=_Theme.primary_text, spaceAfter=6, spaceBefore=16,
            )
            # 用 Table 画左 4px 紫色条 + 浅紫背景
            from reportlab.platypus import Table, TableStyle, KeepTogether
            inner = Paragraph(_h_inline(text, 2, self.cjk), style)
            from reportlab.lib.units import mm
            t = Table(
                [["", inner]],
                colWidths=[1.4 * mm, _usable_width() - 1.4 * mm],
            )
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), _Theme.primary),
                ("BACKGROUND", (1, 0), (1, -1), _Theme.primary_light),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (1, 0), (1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            self.flowables.append(KeepTogether(t))
            return
        if tag == "h3":
            style = ParagraphStyle(
                "h3", fontName=self.cjk, fontSize=12, leading=17,
                textColor=_Theme.primary_text, spaceAfter=4, spaceBefore=12,
            )
            self.flowables.append(Paragraph(_h_inline(text, 3, self.cjk), style))
            return
        if tag == "h4":
            style = ParagraphStyle(
                "h4", fontName=self.cjk, fontSize=10.5, leading=15,
                textColor=(0x33 / 255, 0x41 / 255, 0x55 / 255), spaceAfter=3, spaceBefore=10,
            )
            self.flowables.append(Paragraph(_h_inline(text, 4, self.cjk), style))
            return
        # 默认：paragraph
        style = ParagraphStyle(
            "body", fontName=self.cjk, fontSize=10.5, leading=18,
            textColor=_Theme.primary_text, alignment=4,  # justify
            spaceAfter=3,
        )
        if not text.strip():
            return
        self.flowables.append(Paragraph(_make_safe_rl_xml(text, self.cjk), style))

    def finalize(self):
        # 刷新残留段落
        self._flush_paragraph()
        # Table 未关闭的防御：弹出所有剩余栈
        while self._stack:
            s = self._stack.pop()
            t = s.get("tag") if isinstance(s, dict) else None
            if t == "table":
                pass  # 未关闭就丢弃
            elif t == "__saved_buf__":
                self._buf = s.get("buf", [])
            # 其他忽略


def _usable_width() -> float:
    return _A4_W - PAGE_MARGIN_L - PAGE_MARGIN_R


def _escape_rl_text(s: str) -> str:
    """转义 reportlab Paragraph mini XML 的特殊字符。"""
    # 已经是 mini XML（包含 <b>/<font>/<a>/<super>/<br/>）的不做过度转义：
    # 这里只转义纯文本阶段的 & < >，但 HTMLParser 已经把 &lt; 等 charref 还原回字符，
    # 我们需要对 data 再次转义；而对 inline tag 生成环节已直接拼 mini XML 的位置不调用本函数。
    return (s
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;"))


def _h_inline(text: str, level: int, cjk: str) -> str:
    face = f'<font face="{cjk}">' if cjk and cjk != "Helvetica" else ""
    close = "</font>" if face else ""
    # h1/h2 加粗
    if level <= 2:
        return f"{face}<b>{text}</b>{close}"
    return f"{face}{text}{close}"


def _make_safe_rl_xml(xml_like: str, cjk: str) -> str:
    """给最终 paragraph 包一层 CJK 字体兜底。"""
    # 只在最外层包 font face，如果有的标签已包，reportlab 会就近取最近的 font
    if not cjk or cjk == "Helvetica":
        return xml_like
    # 用 size 相对 10.5pt（Paragraph style 定义的）会覆盖，这里只写 face
    return f'<font face="{cjk}">{xml_like}</font>'


# —— Markdown → Flowables 对外函数 ——

def _md_to_flowables(md_text: str, cjk_font: str) -> list:
    import markdown as md_lib

    extensions = ["extra", "toc", "sane_lists"]
    # codehilite 不需要，我们用 <pre><code> 统一渲染
    html_body = md_lib.markdown(md_text, extensions=extensions)
    # 预处理：把已经在正文中存在的 [来源N] 数字引用 → 上标
    html_body = re.sub(
        r"\[来源([A-Za-z0-9]+)\]",
        r'<sup class="sr-cite">[\1]</sup>',
        html_body,
    )
    parser = _MdHtmlToFlowables(cjk_font=cjk_font)
    parser.feed(html_body)
    parser.finalize()
    return parser.flowables


# —— 封面 / 正文页面的 onPage 回调 ——

def _cover_on_page(canvas, doc, *, stock_code: str, stock_name: str):
    w, h = _A4_W, _A4_H
    canvas.saveState()
    # 1. 整页紫蓝背景
    canvas.setFillColorRGB(*_Theme.primary)
    canvas.rect(0, 0, w, h, fill=1, stroke=0)
    # 2. 品牌标（左上）
    canvas.setFillColorRGB(*_Theme.white_dim)
    canvas.setFont("Helvetica", 10.5)
    canvas.drawString(PAGE_MARGIN_L + 4, h - 50, "ZHI SHI KU · AI RESEARCH")
    # 3. 报告标题（大）
    name = stock_name or "股票研究"
    code = stock_code or "—"
    canvas.setFillColorRGB(1, 1, 1)
    # 标题：两档字号，中文找字体会慢，直接用注册的 CJK 字体
    from reportlab.pdfbase import pdfmetrics
    cjk = _register_cjk_font()
    title_font = cjk if cjk in set(pdfmetrics.getRegisteredFontNames()) else "Helvetica-Bold"
    canvas.setFont(title_font, 30)
    canvas.drawString(PAGE_MARGIN_L + 4, h - 210, f"{name} 深度研究报告")
    # 副标题
    canvas.setFillColorRGB(*_Theme.white_soft)
    canvas.setFont(title_font, 13)
    canvas.drawString(PAGE_MARGIN_L + 4, h - 235, f"代码 {code} · 投资评级参考")
    # 4. 元信息卡片（圆角矩形）
    card_x = PAGE_MARGIN_L + 4
    card_y = h - 540
    card_w = w - PAGE_MARGIN_L - PAGE_MARGIN_R - 8
    card_h = 160
    # 半透明白色背景：用低透明度（reportlab 的 fillalpha）
    try:
        canvas.setFillAlpha(0.14)
    except Exception:
        pass
    canvas.setFillColorRGB(1, 1, 1)
    _round_rect(canvas, card_x, card_y, card_w, card_h, 10, fill=1, stroke=0)
    try:
        canvas.setStrokeAlpha(0.3)
    except Exception:
        pass
    canvas.setStrokeColorRGB(1, 1, 1)
    canvas.setLineWidth(0.7)
    _round_rect(canvas, card_x, card_y, card_w, card_h, 10, fill=0, stroke=1)
    try:
        canvas.setFillAlpha(1.0)
    except Exception:
        pass
    # 2×2 网格
    today = datetime.now().strftime("%Y 年 %m 月 %d 日")
    items = [
        ("股票名称", name),
        ("股票代码", code),
        ("报告日期", today),
        ("生成方式", "AI 智能生成"),
    ]
    cols = 2
    cell_w = (card_w - 24) / cols
    cell_h = (card_h - 18) / 2
    label_font = "Helvetica"
    value_font = title_font
    for i, (label, value) in enumerate(items):
        col = i % cols
        row = i // cols
        x = card_x + 12 + col * cell_w
        y = card_y + card_h - 18 - row * cell_h
        # label
        canvas.setFillColorRGB(*_Theme.white_dim)
        canvas.setFont(label_font, 9)
        canvas.drawString(x, y - 2, label)
        # value
        canvas.setFillColorRGB(1, 1, 1)
        canvas.setFont(value_font, 12.5)
        canvas.drawString(x, y - 20, str(value))
    # 5. 页脚风险提示
    canvas.setFillColorRGB(*_Theme.white_dim)
    canvas.setFont("Helvetica", 9)
    footer = "本报告仅供研究参考，不构成投资建议"
    fw = canvas.stringWidth(footer, "Helvetica", 9)
    canvas.drawString(w - PAGE_MARGIN_R - fw - 4, PAGE_MARGIN_B - 18, footer)
    canvas.restoreState()


def _body_on_page(canvas, doc, *, stock_code: str, stock_name: str):
    w, h = _A4_W, _A4_H
    from reportlab.pdfbase import pdfmetrics
    cjk = _register_cjk_font()
    cjk_ok = cjk in set(pdfmetrics.getRegisteredFontNames())
    header_font = cjk if cjk_ok else "Helvetica"
    canvas.saveState()
    # —— 页眉分隔线 ——
    line_y = h - 40
    canvas.setStrokeColorRGB(0xE2 / 255, 0xE8 / 255, 0xF0 / 255)
    canvas.setLineWidth(0.5)
    canvas.line(PAGE_MARGIN_L, line_y, w - PAGE_MARGIN_R, line_y)
    # —— 页眉左：品牌 ——
    canvas.setFillColorRGB(*_Theme.muted)
    canvas.setFont(header_font, 9)
    canvas.drawString(PAGE_MARGIN_L, line_y + 5, "股票研报助手 · AI")
    # —— 页眉右：股票标题 ——
    name = stock_name or "研究报告"
    code = stock_code or ""
    head_right = f"{name}（{code}）分析报告" if code else f"{name}分析报告"
    canvas.setFillColorRGB(0x47 / 255, 0x55 / 255, 0x69 / 255)
    right_txt = head_right
    tw = canvas.stringWidth(right_txt, header_font, 9)
    canvas.drawString(w - PAGE_MARGIN_R - tw, line_y + 5, right_txt)
    # —— 页脚：第 N / M 页 ——
    canvas.setFillColorRGB(0x94 / 255, 0xA3 / 255, 0xB8 / 255)
    canvas.setFont(header_font, 8.5)
    page_num = canvas.getPageNumber()
    total = getattr(doc, "_total_pages_estimated", None)
    # 先画占位，真正的 total 用多 pass 代价大；直接只画 N
    foot = f"第 {page_num} 页"
    if total:
        foot = f"第 {page_num} / {total} 页"
    tw = canvas.stringWidth(foot, header_font, 8.5)
    canvas.drawString((w - tw) / 2, 36, foot)
    canvas.restoreState()


def _round_rect(canvas, x, y, w, h, r, fill=0, stroke=1):
    """画圆角矩形（reportlab 4.x 自带 roundRect，但手写一个兜底）。"""
    try:
        canvas.roundRect(x, y, w, h, r, stroke=stroke, fill=fill)
    except Exception:
        # 降级：直角矩形
        canvas.rect(x, y, w, h, stroke=stroke, fill=fill)


# —— 参考来源 / 免责声明：追加为 Flowables ——

def _append_sources(flowables: list, sources: list[dict[str, Any]], *, cjk: str):
    if not sources:
        return
    from reportlab.platypus import Paragraph, Table, TableStyle, Spacer, ListFlowable, ListItem
    from reportlab.lib.styles import ParagraphStyle
    title_style = ParagraphStyle(
        "srct", fontName=cjk, fontSize=10.5, leading=15, textColor=_Theme.primary,
    )
    item_style = ParagraphStyle(
        "srci", fontName=cjk, fontSize=9, leading=15,
    )
    title_para = Paragraph("📎 参考来源", title_style)

    # 限制来源数量，避免单页溢出
    MAX_SOURCES = 15
    li_items = []
    valid = 0
    truncated = len(sources) > MAX_SOURCES
    for s in sources[:MAX_SOURCES]:
        t = (s.get("title") or "未命名").strip()
        url = (s.get("source") or "").strip()
        safe_t = _escape_rl_text(t)
        if url and url.startswith("http"):
            safe_u = _html.escape(url, quote=True)
            para_text = (
                f'<font face="{cjk}"><a href="{safe_u}" color="#2563eb"><u>{safe_t}</u></a>'
                f'<br/><font color="#64748b" size="8">{_escape_rl_text(url)}</font></font>'
            )
        else:
            desc = t + "（知识库 / 内部资料）"
            para_text = f'<font face="{cjk}" color="#6b7280"><i>{_escape_rl_text(desc)}</i></font>'
        li_items.append(ListItem(Paragraph(para_text, item_style)))
        valid += 1
    if truncated:
        li_items.append(ListItem(
            Paragraph(
                f'<font face="{cjk}" color="#9ca3af" size="8">…还有 {len(sources) - MAX_SOURCES} 条来源未展示</font>',
                item_style,
            )
        ))
    if valid == 0:
        return

    # 标题直接放入 flowables（不进表格，让 ListFlowable 可跨页）
    flowables.append(title_para)
    flowables.append(Spacer(1, 4))

    # 列表直接放入，允许跨页分页
    list_flo = ListFlowable(
        li_items, bulletType="1", start="1", leftIndent=18, bulletFontSize=9,
    )
    flowables.append(list_flo)
    flowables.append(Spacer(1, 10))


def _append_disclaimer(flowables: list, *, cjk: str):
    from reportlab.platypus import Paragraph, Table, TableStyle, KeepTogether
    from reportlab.lib.styles import ParagraphStyle
    style = ParagraphStyle(
        "disc", fontName=cjk, fontSize=9, leading=15, textColor=_Theme.disclaimer_text,
    )
    text = (
        "<b>⚠️ 免责声明：</b>"
        "本报告由 AI 基于公开信息整理生成，仅供研究与学习参考，不构成任何投资建议。"
        "股市有风险，投资需谨慎，投资者应结合自身风险承受能力独立做出决策。"
    )
    inner = Paragraph(f'<font face="{cjk}">{text}</font>', style)
    t = Table([[inner]], colWidths=[_usable_width()])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _Theme.disclaimer_bg),
        ("BOX", (0, 0), (-1, -1), 0.7, _Theme.disclaimer_border),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    flowables.append(KeepTogether(t))


# —— 对外 API：generate_pdf_report ——

def generate_pdf_report(
    *,
    title: str = "",
    stock_code: str = "",
    stock_name: str = "",
    content_md: str = "",
    sources: list[dict[str, Any]] | None = None,
    tool_data: list[dict[str, Any]] | None = None,
    output_dir: Path | None = None,
) -> PdfReportResult:
    """生产级 PDF 生成（纯 reportlab 4.x，无原生库依赖）。

    Args:
        title: 报告标题（优先于 markdown H1）
        stock_code: 股票代码
        stock_name: 股票名称
        content_md: 模型生成的 Markdown 正文
        sources: 参考来源列表 [{title, source}]
        tool_data: 工具调用数据 [{tool, input, output}]，用于生成结构化板块
        output_dir: 输出目录
    """
    from reportlab.platypus import (
        BaseDocTemplate, PageTemplate, Frame, NextPageTemplate, PageBreak, Spacer,
    )

    cjk_font = _register_cjk_font()

    if output_dir is None:
        output_dir = BASE_DIR / "data" / "pdfs"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 构造美化后的文件名
    # 1. 提取标题：优先用 title 参数，否则从 markdown 首个 H1 提取
    report_title = title or _extract_title_from_markdown(content_md)
    # 2. 组合文件名：如果标题已包含股票信息则直接用标题，否则追加
    stock_part = f"{stock_name}({stock_code})" if stock_name and stock_code else (stock_name or stock_code or "")
    if report_title:
        # 如果标题中已包含股票代码/名称，不再重复追加
        already_has_stock = bool(stock_code and stock_code in report_title) or bool(stock_name and stock_name in report_title)
        if already_has_stock:
            raw_name = report_title
        elif stock_part:
            raw_name = f"{report_title}_{stock_part}"
        else:
            raw_name = report_title
    elif stock_part:
        raw_name = f"股票研报_{stock_part}"
    else:
        raw_name = "股票研报"
    file_name = sanitize_filename(raw_name)
    file_path = output_dir / file_name
    if file_path.exists():
        h = hashlib.md5((content_md or "")[:1000].encode("utf-8")).hexdigest()[:6]
        file_path = output_dir / f"{file_path.stem}_{h}.pdf"
        file_name = file_path.name

    # Step 1. Flowables：封面 → 分页 → 正文 → 参考来源 → 免责声明
    cover_flowables: list = [NextPageTemplate("body"), PageBreak()]
    body_flowables: list = []

    # 结构化板块：行情快照 插入正文前面
    tool_data_list = tool_data or []
    pre_flowables: list = []
    pre_flowables.extend(_build_quote_snapshot(tool_data_list, cjk=cjk_font))

    # 正文
    body_flowables: list = []
    body_flowables.extend(pre_flowables)

    if content_md:
        body_flowables.extend(_md_to_flowables(content_md, cjk_font))
    else:
        from reportlab.platypus import Paragraph
        from reportlab.lib.styles import ParagraphStyle
        style = ParagraphStyle("empty", fontName=cjk_font, fontSize=10.5, leading=18)
        body_flowables.append(Paragraph("（无正文）", style))

    # 正文一级标题：如果 markdown 正文已经以 # 开头则不再加
    name = stock_name or title or "股票研究"
    code = stock_code or "—"
    report_title_text = f"{name}（{code}）分析报告"
    already_has_h1 = False
    if content_md.lstrip().startswith("# ") or content_md.lstrip().startswith("#\t"):
        already_has_h1 = True
    if not already_has_h1:
        from reportlab.platypus import Paragraph, KeepTogether
        from reportlab.lib.styles import ParagraphStyle
        style = ParagraphStyle(
            "title-h1", fontName=cjk_font, fontSize=20, leading=28,
            textColor=_Theme.primary_text, spaceAfter=10, spaceBefore=6,
        )
        from reportlab.platypus import Table, TableStyle
        inner = Paragraph(_make_safe_rl_xml(f"<b>{_escape_rl_text(report_title_text)}</b>", cjk_font), style)
        body_flowables.insert(0, inner)
        from reportlab.platypus import Spacer as _S
        line = Table([[""]], colWidths=[_usable_width()], rowHeights=[2])
        line.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _Theme.primary)]))
        body_flowables.insert(1, _S(1, 4))
        body_flowables.insert(2, line)
        body_flowables.insert(3, _S(1, 8))

    # 参考来源
    _append_sources(body_flowables, sources or [], cjk=cjk_font)
    # 免责声明
    _append_disclaimer(body_flowables, cjk=cjk_font)

    all_flowables = cover_flowables + body_flowables

    # Step 2. Build with BaseDocTemplate（cover 用单独的 PageTemplate；body 用带页眉页脚的）
    doc = BaseDocTemplate(
        str(file_path),
        pagesize=(_A4_W, _A4_H),
        leftMargin=PAGE_MARGIN_L,
        rightMargin=PAGE_MARGIN_R,
        topMargin=PAGE_MARGIN_T,
        bottomMargin=PAGE_MARGIN_B,
        title=report_title_text,
        author="ZHI SHI KU · AI 股票研报助手",
        subject="AI Generated Equity Research Report",
        creator="ZHI SHI KU",
    )

    # 封面 frame：占满整页，margin=0
    cover_frame = Frame(0, 0, _A4_W, _A4_H, id="cover", leftPadding=0, rightPadding=0,
                        topPadding=0, bottomPadding=0)
    # 正文 frame
    body_frame = Frame(
        PAGE_MARGIN_L, PAGE_MARGIN_B,
        _A4_W - PAGE_MARGIN_L - PAGE_MARGIN_R,
        _A4_H - PAGE_MARGIN_T - PAGE_MARGIN_B,
        id="body",
    )

    def _cover_page(canvas, doc):
        return _cover_on_page(canvas, doc, stock_code=stock_code, stock_name=stock_name)

    def _body_page(canvas, doc):
        return _body_on_page(canvas, doc, stock_code=stock_code, stock_name=stock_name)

    doc.addPageTemplates([
        # id='cover' 放第一个，因为 NextPageTemplate('body') 切换；但封面页应该默认用 cover
        # 我们在 cover_flowables 开头用 NextPageTemplate('body')，意思是「从下一页开始用 body」，
        # 所以当前（第一页）仍用 doc 的第一个模板。保证第一个模板就是 cover。
        PageTemplate(id="cover", frames=[cover_frame], onPage=_cover_page),
        PageTemplate(id="body", frames=[body_frame], onPage=_body_page),
    ])

    doc.build(all_flowables)

    size_kb = round(Path(file_path).stat().st_size / 1024, 1)
    download_url = f"/api/stock_research/download/{file_name}"
    return PdfReportResult(
        file_path=file_path,
        file_name=file_name,
        download_url=download_url,
        file_size_kb=size_kb,
    )
