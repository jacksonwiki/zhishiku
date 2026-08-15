"""用户认证接口：注册 / 登录 / 当前用户 / 访客 id。

设计：
- 密码用 bcrypt 哈希存储，不可逆。
- 登录成功返回 JWT（HS256），前端存 localStorage，请求带 `Authorization: Bearer <token>`。
- 访客模式：前端生成 `guest_<随机串>` id；无登录态但能使用客服功能，会话数据归属访客 id。
- 依赖注入：`get_current_user` 解析 JWT；未带 token 返回 401；前端可改用访客模式。
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field

from app.config import settings
from app.rag.user_store import User, get_user_store, new_guest_id

router = APIRouter(prefix="/api/auth", tags=["auth"])


# —— 请求 / 响应模型 ——

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, description="用户名")
    password: str = Field(..., min_length=4, max_length=128, description="密码")
    display_name: Optional[str] = Field(None, max_length=32, description="昵称")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=4, max_length=128)


class UserInfo(BaseModel):
    id: str
    username: str
    display_name: str


class AuthResponse(BaseModel):
    token: str
    user: UserInfo
    expires_in: int = Field(..., description="token 有效期（秒）")


class GuestResponse(BaseModel):
    user_id: str
    display_name: str


# —— JWT 工具 ——

def create_token(user: User) -> str:
    """生成 JWT token。"""
    now = int(time.time())
    payload = {
        "sub": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "iat": now,
        "exp": now + settings.jwt_expire_hours * 3600,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algo)


def decode_token(token: str) -> dict:
    """解码 JWT；失败抛 HTTPException(401)。"""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algo])
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="无效的登录凭证") from e


def _extract_token(authorization: Optional[str]) -> str:
    """从 Authorization 头提取 token。"""
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供登录凭证")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authorization 头格式错误，应为 'Bearer <token>'")
    return parts[1].strip()


def get_current_user(authorization: Optional[str] = Header(None)) -> User:
    """FastAPI 依赖：校验 JWT 并返回当前登录用户。

    用法：`user: User = Depends(get_current_user)`。
    """
    token = _extract_token(authorization)
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="token 中缺少用户信息")
    user = get_user_store().get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在或已被删除")
    return user


def get_optional_user(authorization: Optional[str] = Header(None)) -> Optional[User]:
    """FastAPI 依赖：已登录返回 User，未登录返回 None（访客模式）。"""
    if not authorization:
        return None
    try:
        token = _extract_token(authorization)
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            return None
        return get_user_store().get_user(user_id)
    except HTTPException:
        return None


# —— 接口 ——

@router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest) -> AuthResponse:
    """注册新用户并返回 token。"""
    store = get_user_store()
    try:
        user = store.create_user(req.username, req.password, req.display_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = create_token(user)
    return AuthResponse(
        token=token,
        user=UserInfo(id=user.id, username=user.username, display_name=user.display_name),
        expires_in=settings.jwt_expire_hours * 3600,
    )


@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest) -> AuthResponse:
    """用户名 + 密码登录，返回 JWT。"""
    store = get_user_store()
    user = store.authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_token(user)
    return AuthResponse(
        token=token,
        user=UserInfo(id=user.id, username=user.username, display_name=user.display_name),
        expires_in=settings.jwt_expire_hours * 3600,
    )


@router.get("/me", response_model=UserInfo)
def me(user: User = Depends(get_current_user)) -> UserInfo:
    """返回当前登录用户信息（需带 token）。"""
    return UserInfo(id=user.id, username=user.username, display_name=user.display_name)


@router.post("/guest", response_model=GuestResponse)
def guest() -> GuestResponse:
    """生成访客 id（无需注册登录即可使用客服）。"""
    gid = new_guest_id()
    return GuestResponse(user_id=gid, display_name="访客")
