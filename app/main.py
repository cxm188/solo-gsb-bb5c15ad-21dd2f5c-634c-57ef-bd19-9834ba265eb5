"""FastAPI 应用入口。

接口一览：
- POST /namespaces/{namespace}/names        注册显示名（201 / 409 / 422）
- POST /namespaces/{namespace}/names/batch  批量注册（201 / 409 / 422，单事务）
- POST /namespaces/{namespace}/check        只分析不落库（200）
- GET  /namespaces/{namespace}/names        列出命名空间内已注册名称
- GET  /health                              健康检查
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, status

from . import database, service
from .schemas import AnalysisResult, BatchNameRequest, BatchRegisterResponse, NameRecord, NameRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时自动初始化数据库（建库建表，幂等）
    database.init_db()
    yield


app = FastAPI(
    title="Unicode Display Name Anti-Spoofing API",
    version="1.0.0",
    description=(
        "命名空间内的显示名防仿冒：NFKC+casefold 规范名查重、"
        "confusables 同形字符判定、Latin/Cyrillic/Greek 混脚本拦截。"
    ),
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = database.get_connection()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/namespaces/{namespace}/names",
    status_code=status.HTTP_201_CREATED,
    response_model=NameRecord,
    responses={
        409: {"description": "规范名相同或与已有名称同形（confusable）"},
        422: {"description": "混用 Latin/Cyrillic/Greek 中两种及以上脚本"},
    },
)
def register_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "mixed_script_violation",
                "message": (
                    "名称同时使用了 Latin/Cyrillic/Greek 中的 "
                    f"{len(analysis['restricted_scripts_used'])} 种脚本，"
                    "存在仿冒风险，已拒绝注册"
                ),
                "restricted_scripts_used": analysis["restricted_scripts_used"],
                "script_details": analysis["script_details"],
            },
        )

    if analysis["conflicts"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "规范名相同或与命名空间内已有名称同形",
                "normalized_name": analysis["normalized_name"],
                "conflicts": analysis["conflicts"],
            },
        )

    try:
        return service.insert_name(
            conn, namespace, payload.name, analysis["normalized_name"]
        )
    except service.NameConflictError:
        # 并发下唯一约束兜底：重新查出冲突记录返回
        conflicts = service.find_conflicts(conn, namespace, analysis["normalized_name"])
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "规范名相同或与命名空间内已有名称同形",
                "normalized_name": analysis["normalized_name"],
                "conflicts": conflicts,
            },
        )


@app.post(
    "/namespaces/{namespace}/names/batch",
    status_code=status.HTTP_201_CREATED,
    response_model=BatchRegisterResponse,
    responses={
        409: {"description": "并发竞争使事务判定失效：已回滚，返回重新分析后的冲突明细"},
        422: {"description": "批次中存在混脚本或库内/批内冲突：汇总明细，未写入任何记录"},
    },
)
def register_names_batch(
    namespace: str, payload: BatchNameRequest, conn=Depends(get_db)
) -> dict:
    # 整批与库内已有名称联合分析：库内冲突 + 批内两两冲突
    analyses = service.analyze_batch(conn, namespace, payload.names)

    failures = [
        item
        for item in analyses
        if item["mixed_script_violation"] or item["conflicts"] or item["batch_conflicts"]
    ]
    if failures:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "batch_validation_failed",
                "message": (
                    f"批次中 {len(failures)}/{len(analyses)} 个名称存在混脚本或冲突问题，"
                    "未写入任何记录"
                ),
                "items": failures,
            },
        )

    try:
        records = service.insert_batch(conn, namespace, payload.names)
    except service.NameConflictError:
        # 并发竞争：事务已回滚，重新分析后返回 409 冲突明细
        refreshed = service.analyze_batch(conn, namespace, payload.names)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "并发写入使批次判定失效，事务已回滚；以下为重新分析后的冲突明细",
                "items": [
                    item
                    for item in refreshed
                    if item["conflicts"] or item["batch_conflicts"]
                ],
            },
        )

    return {
        "namespace": namespace,
        "items": [
            {"index": index, **record} for index, record in enumerate(records)
        ],
    }


@app.post(
    "/namespaces/{namespace}/check",
    response_model=AnalysisResult,
)
def check_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    """只分析不落库：返回规范名、脚本分析与潜在冲突。"""
    return service.analyze_name(conn, namespace, payload.name)


@app.get(
    "/namespaces/{namespace}/names",
    response_model=list[NameRecord],
)
def list_names(namespace: str, conn=Depends(get_db)) -> list[dict]:
    return service.list_names(conn, namespace)
