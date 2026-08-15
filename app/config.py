"""应用配置：从环境变量 / .env 读取。

支持多种模型 provider：
- dashscope：阿里云百炼（千问），需要 DASHSCOPE_API_KEY
- ollama：本地 Ollama 服务，无需 API Key
- deepseek：深度求索，需要 DEEPSEEK_API_KEY
通过 LLM_PROVIDER / EMBEDDING_PROVIDER 切换。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 加载 .env
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    dashscope_api_key: str
    deepseek_api_key: str
    llm_model: str
    embedding_model: str
    chroma_persist_dir: Path
    chroma_collection_name: str
    chat_db: Path
    jwt_secret: str
    jwt_algo: str
    jwt_expire_hours: int
    app_host: str
    app_port: int
    # provider 选择：dashscope | ollama | deepseek
    llm_provider: str
    embedding_provider: str
    ollama_base_url: str
    # Tavily Web Search API Key（可选；未配置则不启用网络搜索工具）
    tavily_api_key: str
    # AkShare 股票数据缓存 TTL（秒）
    akshare_cache_ttl: int

    @classmethod
    def from_env(cls) -> "Settings":
        persist_dir = Path(os.getenv("CHROMA_PERSIST_DIR", "./data/chroma"))
        if not persist_dir.is_absolute():
            persist_dir = BASE_DIR / persist_dir
        persist_dir.mkdir(parents=True, exist_ok=True)
        chat_db = Path(os.getenv("CHAT_DB", "./data/chat.db"))
        if not chat_db.is_absolute():
            chat_db = BASE_DIR / chat_db
        chat_db.parent.mkdir(parents=True, exist_ok=True)
        return cls(
            dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", "qwen-plus"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-v2"),
            chroma_persist_dir=persist_dir,
            chroma_collection_name=os.getenv("CHROMA_COLLECTION_NAME", "zhishiku"),
            chat_db=chat_db,
            jwt_secret=os.getenv("JWT_SECRET", "change-me-in-production-please"),
            jwt_algo=os.getenv("JWT_ALGO", "HS256"),
            jwt_expire_hours=int(os.getenv("JWT_EXPIRE_HOURS", "168")),
            app_host=os.getenv("APP_HOST", "0.0.0.0"),
            app_port=int(os.getenv("APP_PORT", "8000")),
            llm_provider=os.getenv("LLM_PROVIDER", "dashscope").lower(),
            embedding_provider=os.getenv("EMBEDDING_PROVIDER", "dashscope").lower(),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
            akshare_cache_ttl=int(os.getenv("AKSHARE_CACHE_TTL", "60")),
        )


settings = Settings.from_env()


def model_ready() -> bool:
    """模型是否就绪：ollama 模式无需 key；dashscope/deepseek 模式需配置真实 key。"""
    if settings.llm_provider == "ollama" or settings.embedding_provider == "ollama":
        return True
    if settings.llm_provider == "deepseek":
        return bool(settings.deepseek_api_key) and not settings.deepseek_api_key.startswith(
            "sk-your"
        )
    return bool(settings.dashscope_api_key) and not settings.dashscope_api_key.startswith(
        "sk-your"
    )