"""动态提示词管理：SQLite 存储 prompt 模板，支持 CRUD + 变量插值。

预置 3 个默认模板：
1. 快速分析 — 简要行情 + 操作建议（~300字）
2. 深度研报 — 完整六模块研报（摘要/基本面/技术面/资金面/新闻/预测建议）
3. 行业对比 — 行业概况 + 竞争格局 + 对标分析
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings

_DB_PATH = settings.chat_db
_lock = threading.Lock()

# —— 数据模型 ——


@dataclass
class PromptTemplate:
    id: str
    name: str
    category: str  # quick | deep | industry | custom
    content: str
    variables: list[str]
    created_at: int
    updated_at: int

    def to_dict(self) -> dict[str, Any]:
        import json
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "content": self.content,
            "variables": self.variables,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# —— 数据库初始化 ——


def _init_db() -> None:
    with _lock:
        conn = _conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS prompt_templates (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    category    TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    variables   TEXT NOT NULL DEFAULT '[]',
                    created_at  INTEGER NOT NULL,
                    updated_at  INTEGER NOT NULL
                );
            """)
            conn.commit()
        finally:
            conn.close()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# —— CRUD 接口 ——


def create_template(name: str, category: str, content: str, variables: list[str] | None = None) -> PromptTemplate:
    """创建提示词模板。"""
    _init_db()
    template_id = f"tpl_{uuid.uuid4().hex[:8]}"
    now = int(time.time())
    import json
    tpl = PromptTemplate(
        id=template_id,
        name=name,
        category=category,
        content=content,
        variables=variables or _extract_variables(content),
        created_at=now,
        updated_at=now,
    )
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO prompt_templates (id, name, category, content, variables, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tpl.id, tpl.name, tpl.category, tpl.content, json.dumps(tpl.variables), tpl.created_at, tpl.updated_at),
            )
            conn.commit()
        finally:
            conn.close()
    return tpl


def list_templates(category: str | None = None) -> list[PromptTemplate]:
    """列出所有模板，可按分类过滤。"""
    _init_db()
    with _lock:
        conn = _conn()
        try:
            if category:
                rows = conn.execute(
                    "SELECT * FROM prompt_templates WHERE category = ? ORDER BY updated_at DESC",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM prompt_templates ORDER BY updated_at DESC"
                ).fetchall()
        finally:
            conn.close()
    import json
    return [_row_to_template(r, json) for r in rows]


def get_template(template_id: str) -> PromptTemplate | None:
    """获取单个模板。"""
    _init_db()
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT * FROM prompt_templates WHERE id = ?", (template_id,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    import json
    return _row_to_template(row, json)


def update_template(template_id: str, name: str | None = None, category: str | None = None,
                    content: str | None = None) -> PromptTemplate | None:
    """更新模板。"""
    _init_db()
    now = int(time.time())
    sets = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if category is not None:
        sets.append("category = ?")
        params.append(category)
    if content is not None:
        sets.append("content = ?")
        params.append(content)
        sets.append("variables = ?")
        import json
        params.append(json.dumps(_extract_variables(content)))
    if not sets:
        return get_template(template_id)
    sets.append("updated_at = ?")
    params.append(now)
    params.append(template_id)

    with _lock:
        conn = _conn()
        try:
            conn.execute(f"UPDATE prompt_templates SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
        finally:
            conn.close()
    return get_template(template_id)


def delete_template(template_id: str) -> bool:
    """删除模板。"""
    _init_db()
    with _lock:
        conn = _conn()
        try:
            cursor = conn.execute("DELETE FROM prompt_templates WHERE id = ?", (template_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def render_template(template_id: str, variables: dict[str, str]) -> str | None:
    """渲染模板：将 {{variable}} 替换为实际值。"""
    tpl = get_template(template_id)
    if not tpl:
        return None
    content = tpl.content
    for key, value in variables.items():
        content = content.replace(f"{{{{{key}}}}}", str(value))
    return content


# —— 预置默认模板 ——


_DEFAULT_TEMPLATES = [
    {
        "name": "快速分析",
        "category": "quick",
        "content": """你是专业的股票分析师。用户想了解 {{stock_code}}（{{stock_name}}）的快速分析。

请调用工具获取该股票的实时行情和最近K线数据，然后输出一份简洁分析（约300字）：

## 行情摘要
- 当前价格、涨跌幅、成交量概况

## 技术面简评
- MA均线趋势判断
- KDJ/MACD信号简述

## 操作建议
- 短期操作方向（偏多/偏空/观望）
- 关键支撑位和压力位

注意：使用 [来源1] [来源2] 标注数据来源。""",
    },
    {
        "name": "深度研报",
        "category": "deep",
        "content": """你是资深证券分析师，需要为 {{stock_code}}（{{stock_name}}）撰写一份深度研究报告。

请依次调用工具获取：实时行情、60日K线、财务指标、资金流向、最新新闻，然后生成完整研报：

## 一、行情摘要
当前价格、涨跌幅、成交量、换手率、市值概况。

## 二、基本面分析
- PE/PB 估值水平评估
- ROE、营收增速、利润增速等核心财务指标
- 与行业平均对比

## 三、技术面分析
- K线形态分析（趋势线、形态判断）
- MA5/MA10/MA20/MA60 均线排列
- MACD 金叉/死叉状态
- KDJ 超买/超卖判断
- 关键支撑位和压力位

## 四、资金面分析
- 主力资金净流入/流出趋势
- 超大单/大单动向
- 散户资金行为

## 五、新闻舆情
- 近期重大消息
- 利好/利空因素

## 六、预测与建议
- 短期趋势预判（1-2周）
- 中期趋势预判（1-3月）
- 投资建议（买入/持有/卖出/观望）
- 风险提示

注意：使用 [来源1] [来源2] 标注数据来源。""",
    },
    {
        "name": "行业对比",
        "category": "industry",
        "content": """你是行业研究员，需要对 {{stock_code}}（{{stock_name}}）进行行业对比分析。

请调用工具获取该股票的行情和财务数据，然后输出行业对比报告：

## 行业概况
- 该股票所属行业整体表现
- 行业景气度判断

## 竞争格局
- 行业主要竞争对手
- {{stock_name}} 在行业中的地位

## 对标分析
- 与行业龙头对比：PE/PB/ROE/营收增速
- 估值优势/劣势

## 投资建议
- 相对于行业，该股票是否被低估/高估
- 配置建议

注意：使用 [来源1] [来源2] 标注数据来源。""",
    },
]


def ensure_default_templates() -> None:
    """首次启动时创建预置模板（已存在同名模板则跳过）。"""
    _init_db()
    existing = {t.name for t in list_templates()}
    for tpl_data in _DEFAULT_TEMPLATES:
        if tpl_data["name"] not in existing:
            create_template(
                name=tpl_data["name"],
                category=tpl_data["category"],
                content=tpl_data["content"],
            )


# —— 辅助函数 ——


def _extract_variables(content: str) -> list[str]:
    """从模板内容中提取 {{variable}} 变量名。"""
    return list(set(re.findall(r"\{\{(\w+)\}\}", content)))


def _row_to_template(row, json_mod) -> PromptTemplate:
    return PromptTemplate(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        content=row["content"],
        variables=json_mod.loads(row["variables"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
