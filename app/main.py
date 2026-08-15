"""FastAPI 应用入口。

启动方式：
    uvicorn app.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import knowledge as knowledge_api
from app.api import auth as auth_api
from app.api import customer_service as customer_service_api
from app.api import search as search_api
from app.api import stock_research as stock_research_api
from app.config import settings

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="知识库 RAG 系统",
    description="基于 LangChain + 千问 + Chroma 的 RAG 知识库",
    version="0.1.0",
)

# 注册 API 路由
app.include_router(knowledge_api.router)
app.include_router(search_api.router)
app.include_router(auth_api.router)
app.include_router(customer_service_api.router)
app.include_router(stock_research_api.router)

# 初始化预置提示词模板
from app.rag.prompt_manager import ensure_default_templates as _ensure_prompts
_ensure_prompts()

# 静态资源
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index_page() -> FileResponse:
    """默认进入知识库维护页。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/search")
def search_page() -> FileResponse:
    """检索问答页。"""
    return FileResponse(STATIC_DIR / "search.html")


@app.get("/customer_service")
def customer_service_page() -> FileResponse:
    """智能客服页。"""
    return FileResponse(STATIC_DIR / "customer_service.html")


@app.get("/login")
def login_page() -> FileResponse:
    """登录页。"""
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/stock_research")
def stock_research_page() -> FileResponse:
    """股票研报助手页。"""
    return FileResponse(STATIC_DIR / "stock_research.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查与配置预览。"""
    from app.config import model_ready

    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "embedding_provider": settings.embedding_provider,
        "llm_model": settings.llm_model,
        "embedding_model": settings.embedding_model,
        "chroma_dir": str(settings.chroma_persist_dir),
        "collection": settings.chroma_collection_name,
        "model_ready": model_ready(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
