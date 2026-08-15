"""API 层公共工具：错误处理等。"""

from __future__ import annotations

from fastapi import HTTPException


def wrap_llm_error(e: Exception) -> HTTPException:
    """把 LLM/向量库调用异常转成对前端友好的 HTTPException。

    千问账户欠费、额度不足、Key 无效等场景会抛 ValueError，内容包含
    status_code / code / message，这里解析后返回可读提示。
    """
    msg = str(e)
    # 常见千问错误码
    hints = {
        "Arrearage": "阿里云百炼账户欠费，请充值后重试",
        "InvalidApiKey": "DASHSCOPE_API_KEY 无效，请检查 .env 配置",
        "AccessDenied": "阿里云百炼访问被拒绝，请检查账户权限",
        "Throttling": "请求被限流，请稍后重试",
    }
    for code, hint in hints.items():
        if code in msg:
            return HTTPException(status_code=503, detail=f"{hint}（{code}）")
    # 兜底
    return HTTPException(status_code=503, detail=f"模型调用失败：{msg[:200]}")
