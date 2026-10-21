"""全局配置：从环境变量 / .env 加载，提供业务库与分析库连接串。"""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。所有字段均可通过环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ---------- 应用 ----------
    APP_NAME: str = "ecom-agent"
    APP_ENV: str = "dev"
    APP_DEBUG: bool = True
    API_PORT: int = 8090
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"

    # ---------- PostgreSQL（业务库） ----------
    DB_HOST: str = "localhost"
    DB_PORT: int = 5433
    DB_USER: str = "ecom"
    DB_PASSWORD: str = "ecom123"
    DB_NAME: str = "ecom_db"

    # ---------- PostgreSQL（分析库，独立低权限账号） ----------
    ANALYTICS_DB_HOST: str = "localhost"
    ANALYTICS_DB_PORT: int = 5433
    ANALYTICS_DB_USER: str = "ecom_analytics_agent"
    ANALYTICS_DB_PASSWORD: str = "analytics_agent123"
    ANALYTICS_DB_NAME: str = "ecom_analytics_db"

    # ---------- Redis ----------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6380
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0

    # ---------- Neo4j ----------
    NEO4J_URI: str = "bolt://localhost:7688"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "ecom123"

    # ---------- MinIO ----------
    MINIO_ENDPOINT: str = "localhost:9100"
    MINIO_PUBLIC_ENDPOINT: str = "localhost:9100"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET: str = "ecom-evidence"
    MINIO_SECURE: bool = False

    # ---------- 认证 ----------
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720

    # ---------- 模型网关 ----------
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    CHAT_MODEL: str = "deepseek-chat"
    # 兼容端点的显式思考开关；None 不发送供应商特有参数。
    CHAT_ENABLE_THINKING: bool | None = None
    CHAT_TRANSPORT_TIMEOUT_SECONDS: float = Field(default=30, gt=0, le=120)

    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    EMBEDDING_MODEL: str = "text-embedding-v3"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_DIM: int = Field(default=1024, validation_alias=AliasChoices("EMBEDDING_DIMENSION", "EMBEDDING_DIM"), ge=1)

    @property
    def embedding_api_key(self) -> str:
        return self.EMBEDDING_API_KEY or self.DASHSCOPE_API_KEY

    @property
    def embedding_base_url(self) -> str:
        return self.EMBEDDING_BASE_URL or self.DASHSCOPE_BASE_URL

    VISION_MODEL: str = "qwen-vl-max"

    # ---------- 检索与生成约束 ----------
    RETRIEVAL_TOP_K: int = 5
    RETRIEVAL_RRF_K: int = 60
    CHUNK_MAX_CHARS: int = Field(default=500, ge=1)
    RAG_CHUNK_MODEL: str = ""  # 留空复用 CHAT_MODEL，使用同一网关凭证
    RAG_CHUNK_WINDOW_CHARS: int = Field(default=6000, ge=128, le=12000)
    RAG_CHUNK_MAX_MODEL_CALLS: int = Field(default=40, ge=1, le=100)
    RAG_INGEST_TIMEOUT_SECONDS: int = Field(default=300, ge=1)
    HYDE_ENABLED: bool = False

    # ---------- Agent 执行预算 ----------
    AGENT_ROUTING_MODE: Literal["auto", "model", "rules"] = "auto"
    AGENT_MAX_TOOL_ROUNDS: int = 3
    TASK_MAX_MODEL_CALLS: int = 8
    TASK_DEADLINE_SECONDS: int = 40
    EXTERNAL_SEARCH_ENABLED: bool = False
    TAVILY_API_KEY: str = ""
    # 人工审批：开启后 LangGraph 在待确认结果处 interrupt() 暂停，需 Command(resume=...) 继续。
    AGENT_HUMAN_APPROVAL: bool = False
    # 答复合成：模型模式下用 create_agent 将已验证事实改写成自然答复，仅流式输出（§4 文案不得改数值）。
    AGENT_REPLY_COMPOSER: bool = True
    COMPOSER_SUMMARY_TRIGGER_TOKENS: int = 6000
    COMPOSER_KEEP_MESSAGES: int = 12

    # ---------- LangGraph 检查点（人工审批与恢复） ----------
    # auto：Redis 可用则持久化，否则回退进程内；redis：强制持久化，失败即启动报错；
    # postgres：复用业务库实例持久化（langgraph-checkpoint-postgres，失败即启动报错）；memory：仅进程内。
    CHECKPOINT_BACKEND: Literal["auto", "redis", "memory", "postgres"] = "auto"
    # 检查点保留策略：Redis TTL（分钟，读取续期）；Postgres 需单独运维清理；0 表示不过期（需运维手动清理）。
    CHECKPOINT_TTL_MINUTES: int = 43200

    AFTERSALE_AUTO_REVIEW: bool = True

    # ---------- 外部售后平台适配（故障注入演练） ----------
    # none：沙箱适配器；timeout/unknown：前 N 次创建注入对应故障（仅演练），逐次消耗。
    PLATFORM_FAULT_MODE: Literal["none", "timeout", "unknown"] = "none"
    PLATFORM_FAULT_TIMES: int = 1
    # UNKNOWN 后按稳定请求号核对外部状态的注入结局；supports_idempotency=False 走人工核对分支。
    PLATFORM_PROBE_RESULT: Literal["created", "missing", "timeout"] = "missing"
    PLATFORM_SUPPORTS_IDEMPOTENCY: bool = True

    # ---------- NL2SQL 限制 ----------
    SQL_MODEL_TIMEOUT_SECONDS: int = Field(default=60, ge=1, le=120)
    SQL_STATEMENT_TIMEOUT_MS: int = 5000
    SQL_MAX_ROWS: int = 1000
    SQL_MAX_CONCURRENT_PER_USER: int = 2

    # ---------- 派生属性 ----------
    @property
    def db_url(self) -> str:
        """业务库异步连接串。"""
        return (
            f"postgresql+asyncpg://{quote(self.DB_USER, safe='')}:{quote(self.DB_PASSWORD, safe='')}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def db_url_sync(self) -> str:
        """业务库同步连接串（Alembic 迁移使用）。"""
        return (
            f"postgresql+psycopg2://{quote(self.DB_USER, safe='')}:{quote(self.DB_PASSWORD, safe='')}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def analytics_db_url(self) -> str:
        """分析库连接串（低权限只读账号，NL2SQL 执行专用）。"""
        return (
            f"postgresql+asyncpg://{quote(self.ANALYTICS_DB_USER, safe='')}:{quote(self.ANALYTICS_DB_PASSWORD, safe='')}"
            f"@{self.ANALYTICS_DB_HOST}:{self.ANALYTICS_DB_PORT}/{self.ANALYTICS_DB_NAME}"
        )

    @property
    def redis_url(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def checkpoint_pg_url(self) -> str:
        """检查点 PostgreSQL 连接串（psycopg3 口径，复用业务库实例）。"""
        return (
            f"postgresql://{quote(self.DB_USER, safe='')}:{quote(self.DB_PASSWORD, safe='')}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def redacted_checkpoint_pg_url(self) -> str:
        """日志用：隐藏密码的检查点 PG 地址（§8 日志不写敏感值）。"""
        return f"postgresql://{quote(self.DB_USER, safe='')}:***@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def redact_redis_url(self) -> str:
        """日志用：隐藏密码的 Redis 地址（§8 日志不写敏感值）。"""
        auth = ":***@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


@lru_cache
def get_settings() -> Settings:
    """进程级单例配置。"""
    return Settings()
