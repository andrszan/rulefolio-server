from pydantic import BaseModel, Field


class ApiResponse[T](BaseModel):
    code: int
    message: str
    data: T | None = None


class Page[T](BaseModel):
    items: list[T]
    page: int
    size: int
    total: int


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)
