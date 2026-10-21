"""FastAPI 依赖：认证主体（ActorScope）、角色校验、会话绑定。

数据范围规则（方案 §7）：
- buyer           只访问自己的会话、订单与证据
- staff           只访问授权店铺内的工单
- ops             只访问聚合数据和批准维度
- knowledge_admin 负责发布，不默认拥有订单读取权限
工具执行前仍会以 ActorScope 再次鉴权，不依赖入口路由或提示词约束。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.exceptions import AuthError, PermissionDenied
from src.core.security import decode_access_token
from src.models.identity import User


@dataclass(frozen=True)
class ActorScope:
    """当前请求的认证主体与授权范围快照。"""

    user_id: str
    tenant_id: str
    role: str
    shop_ids: list[str] = field(default_factory=list)
    display_name: str = ""

    @property
    def is_buyer(self) -> bool:
        return self.role == "buyer"

    @property
    def is_staff(self) -> bool:
        return self.role == "staff"

    @property
    def is_ops(self) -> bool:
        return self.role == "ops"

    @property
    def is_knowledge_admin(self) -> bool:
        return self.role == "knowledge_admin"

    def ensure_shop_access(self, shop_id: str) -> None:
        """校验店铺数据范围；超出范围抛 PermissionDenied。"""
        if shop_id not in self.shop_ids:
            raise PermissionDenied("当前账号无权访问该店铺的数据范围")

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "role": self.role,
            "shop_ids": self.shop_ids,
        }


def _extract_bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise AuthError("缺少访问令牌")
    return auth.removeprefix("Bearer ").strip()


async def get_current_actor(
    request: Request, db: AsyncSession = Depends(get_db)
) -> ActorScope:
    """解析令牌并加载用户，构造 ActorScope。"""
    token = _extract_bearer_token(request)
    payload = decode_access_token(token)

    try:
        user_id = UUID(payload.get("sub", ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AuthError("无效的访问令牌") from exc
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("账号不存在或已被停用")
    if payload.get("tenant_id") != user.tenant_id:
        raise AuthError("账号租户已变更，请重新登录")

    return ActorScope(
        user_id=str(user.id),
        tenant_id=user.tenant_id,
        role=user.role,
        shop_ids=list(user.shop_ids or []),
        display_name=user.display_name,
    )


def require_roles(*roles: str):
    """角色校验依赖工厂。"""

    async def _checker(actor: ActorScope = Depends(get_current_actor)) -> ActorScope:
        if actor.role not in roles:
            raise PermissionDenied(f"该操作需要角色：{'/'.join(roles)}")
        return actor

    return _checker
