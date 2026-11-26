from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class RestoreForm(BaseModel):
    bundle: dict[str, Any]
    # 具体格式（64 位十六进制）由恢复服务校验，以返回业务口径的中文错误。
    credential: str = Field(min_length=1, max_length=128)
