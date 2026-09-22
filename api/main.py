"""FastAPI 路由：负责 HTTP 边界，所有业务动作交给 Application Service。"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Annotated, Callable

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .application import ProductApiError, ProductApplicationService
from .schemas import (
    ApiEnvelope,
    CompetitorCreate,
    CompetitorUpdate,
    ProductConfigurationUpdate,
    ProductError,
    ResearchStart,
)


LOGGER = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_FILE_SUFFIXES = {".docx", ".pdf", ".xlsx", ".md", ".txt"}


def _default_state_directory() -> Path:
    """从进程环境读取状态目录；缺省目录已被 Git 忽略，不读取前端环境变量。"""
    configured = os.getenv("PIA_STATE_DIR")
    return Path(configured) if configured else Path.cwd() / ".pia-state"


async def _copy_upload(upload: UploadFile) -> Path:
    """把上传流复制到一次性文件，限制大小并保留受支持后缀。

    文件名只取 basename，防止客户端路径参与本机路径拼接；临时文件只用于调用正式
    Document Service，路由响应从不返回其位置。调用方必须在 finally 中删除。
    """
    filename = Path(upload.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_FILE_SUFFIXES:
        raise ProductApiError("unsupported_file", "当前文件格式不受支持。", 415)
    handle = tempfile.NamedTemporaryFile(prefix="pia-upload-", suffix=suffix, delete=False)
    path = Path(handle.name)
    size = 0
    try:
        with handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ProductApiError("file_too_large", "文件大小超过限制。", 413)
                handle.write(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def create_app(
    service: ProductApplicationService | None = None,
    *, service_factory: Callable[[], ProductApplicationService] | None = None,
) -> FastAPI:
    """创建可注入服务的 API App，生产环境延迟初始化本地状态仓库。"""
    api = FastAPI(title="产品情报助手 API", version="0.1.0")
    origins = [item.strip() for item in os.getenv(
        "PIA_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",") if item.strip()]
    api.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Content-Type"],
    )
    cached = service

    def application() -> ProductApplicationService:
        nonlocal cached
        if cached is None:
            cached = (service_factory or (lambda: ProductApplicationService(_default_state_directory())))()
        return cached

    @api.exception_handler(ProductApiError)
    async def product_error_handler(_request: Request, exc: ProductApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=ProductError(code=exc.code, message=exc.message).model_dump(),
        )

    @api.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception):
        LOGGER.exception("API request failed", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=ProductError(code="request_failed", message="请求未完成，请重试。").model_dump(),
        )

    @api.get("/api/health", response_model=ApiEnvelope)
    async def health():
        return {"data": {"status": "ok"}}

    @api.get("/api/workspaces/{workspace_id}/overview", response_model=ApiEnvelope)
    async def overview(workspace_id: str):
        return {"data": application().overview(workspace_id)}

    @api.post("/api/workspaces/{workspace_id}/scan", response_model=ApiEnvelope)
    async def scan(workspace_id: str):
        return {"data": await application().scan(workspace_id)}

    @api.get("/api/workspaces/{workspace_id}/competitors", response_model=ApiEnvelope)
    async def competitors(workspace_id: str):
        return {"data": application().list_competitors(workspace_id)}

    @api.post("/api/workspaces/{workspace_id}/competitors", response_model=ApiEnvelope)
    async def add_competitor(workspace_id: str, body: CompetitorCreate):
        return {"data": await application().add_competitor(workspace_id, body.model_dump())}

    @api.put(
        "/api/workspaces/{workspace_id}/competitors/{competitor_name}",
        response_model=ApiEnvelope,
    )
    async def update_competitor(
        workspace_id: str, competitor_name: str, body: CompetitorUpdate
    ):
        return {"data": await application().update_competitor(
            workspace_id, competitor_name, body.model_dump()
        )}

    @api.get(
        "/api/workspaces/{workspace_id}/events/{event_id}/research",
        response_model=ApiEnvelope,
    )
    async def research(workspace_id: str, event_id: str):
        return {"data": application().research(workspace_id, event_id)}

    @api.post(
        "/api/workspaces/{workspace_id}/events/{event_id}/research",
        response_model=ApiEnvelope,
    )
    async def start_research(workspace_id: str, event_id: str, body: ResearchStart):
        return {"data": application().start_research(
            workspace_id, event_id, retry=body.retry
        )}

    @api.get("/api/workspaces/{workspace_id}/own-product", response_model=ApiEnvelope)
    async def own_product(workspace_id: str, product_id: str):
        return {"data": application().own_product(workspace_id, product_id)}

    @api.put("/api/workspaces/{workspace_id}/own-product", response_model=ApiEnvelope)
    async def update_own_product(
        workspace_id: str, product_id: str, body: ProductConfigurationUpdate
    ):
        return {"data": application().update_own_product(
            workspace_id,
            product_id,
            body.model_dump(exclude_unset=True, exclude_none=True),
        )}

    @api.post("/api/workspaces/{workspace_id}/own-product/documents", response_model=ApiEnvelope)
    async def add_document(
        workspace_id: str,
        product_id: Annotated[str, Form(min_length=1)],
        file: Annotated[UploadFile, File()],
        display_name: Annotated[str | None, Form()] = None,
    ):
        path = await _copy_upload(file)
        try:
            name = (display_name or Path(file.filename or "资料").name).strip()
            return {"data": application().add_document(workspace_id, product_id, path, name)}
        finally:
            path.unlink(missing_ok=True)

    @api.put(
        "/api/workspaces/{workspace_id}/own-product/documents/{document_id}",
        response_model=ApiEnvelope,
    )
    async def replace_document(
        workspace_id: str,
        document_id: str,
        product_id: Annotated[str, Form(min_length=1)],
        file: Annotated[UploadFile, File()],
    ):
        path = await _copy_upload(file)
        try:
            return {"data": application().replace_document(
                workspace_id, product_id, document_id, path
            )}
        finally:
            path.unlink(missing_ok=True)

    return api


app = create_app()
