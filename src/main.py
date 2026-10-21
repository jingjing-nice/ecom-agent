"""FastAPI 入口：python -m uvicorn src.main:app --port 8090。"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.agents.checkpoint import aclose_checkpointer, configure_checkpointer
from src.core.database import dispose_engines
from src.core.exceptions import register_exception_handlers
from src.core.logger import setup_logger
from src.middlewares.logging import LoggingMiddleware
from src.api.routes import router
from src.api.uploads import router as upload_router
from src.api.knowledge import router as knowledge_router


@asynccontextmanager
async def lifespan(app):
    setup_logger()
    # LangGraph 检查点：跨进程人工审批与恢复依赖持久化后端。
    try:
        await configure_checkpointer()
        yield
    finally:
        try:
            await aclose_checkpointer()
        finally:
            await dispose_engines()


app = FastAPI(title="电商智能客服与运营平台", version="0.2.0", lifespan=lifespan)
app.add_middleware(LoggingMiddleware)
register_exception_handlers(app)
app.include_router(router, prefix="/v1")
app.include_router(upload_router, prefix="/v1")
app.include_router(knowledge_router, prefix="/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "environment": "sandbox"}
