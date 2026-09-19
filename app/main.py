"""FastAPI 应用入口。

接口一览：
- POST /namespaces/{namespace}/names                   注册显示名（201 / 409 / 422）
- POST /namespaces/{namespace}/names/batch             批量注册（201 / 409 / 422，单事务）
- POST /namespaces/{namespace}/reservations            预占显示名（201 / 409 / 422）
- POST /namespaces/{namespace}/reservations/{token}/claim
                                                        确认预占并注册（201 / 404 / 409 / 410 / 422）
- POST /namespaces/{namespace}/check                   只分析不落库（200）
- GET  /namespaces/{namespace}/names                   列出命名空间内已注册名称
- GET  /health                                         健康检查
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, status

from . import database, service
from .schemas import (
    AnalysisResult,
    BatchNameRequest,
    BatchRegisterResponse,
    NameRecord,
    NameRequest,
    ReservationRecord,
    ReservationRequest,
)


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


def _mixed_script_error(analysis: dict) -> HTTPException:
    """混脚本违规统一错误体（422）。"""
    return HTTPException(
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


def _name_conflict_error(analysis: dict, conflicts: list[dict] | None = None) -> HTTPException:
    """名称冲突统一错误体（409，含已注册名称与有效预占两种来源）。"""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "name_conflict",
            "message": "规范名相同或与命名空间内已注册名称/未过期预占同形",
            "normalized_name": analysis["normalized_name"],
            "conflicts": conflicts if conflicts is not None else analysis["conflicts"],
        },
    )


@app.post(
    "/namespaces/{namespace}/names",
    status_code=status.HTTP_201_CREATED,
    response_model=NameRecord,
    responses={
        409: {"description": "规范名相同或与已注册名称/未过期预占同形（confusable）"},
        422: {"description": "混用 Latin/Cyrillic/Greek 中两种及以上脚本"},
    },
)
def register_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise _mixed_script_error(analysis)

    if analysis["conflicts"]:
        raise _name_conflict_error(analysis)

    try:
        return service.insert_name(
            conn, namespace, payload.name, analysis["normalized_name"]
        )
    except service.NameConflictError:
        # 并发下唯一约束兜底：重新查出冲突记录返回
        conflicts = service.find_conflicts(conn, namespace, analysis["normalized_name"])
        raise _name_conflict_error(analysis, conflicts)


@app.post(
    "/namespaces/{namespace}/reservations",
    status_code=status.HTTP_201_CREATED,
    response_model=ReservationRecord,
    responses={
        409: {"description": "规范名相同或与已注册名称/其他未过期预占同形"},
        422: {"description": "混用 Latin/Cyrillic/Greek 中两种及以上脚本，或 ttl_seconds 超出 30–600"},
    },
)
def reserve_name(namespace: str, payload: ReservationRequest, conn=Depends(get_db)) -> dict:
    """预占显示名：复用规范化、混脚本与同形判定，返回不可猜测 token。

    未过期预占与已注册名称一样参与单条注册、批量注册及新预占的冲突判断；
    过期记录在此惰性清理。
    """
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise _mixed_script_error(analysis)

    if analysis["conflicts"]:
        raise _name_conflict_error(analysis)

    try:
        return service.create_reservation(
            conn,
            namespace,
            payload.name,
            analysis["normalized_name"],
            payload.ttl_seconds,
        )
    except service.NameConflictError:
        # 并发竞争（预占事务内复查或唯一索引兜底）：重新查出冲突记录返回
        conflicts = service.find_conflicts(conn, namespace, analysis["normalized_name"])
        raise _name_conflict_error(analysis, conflicts)


@app.post(
    "/namespaces/{namespace}/reservations/{token}/claim",
    status_code=status.HTTP_201_CREATED,
    response_model=NameRecord,
    responses={
        404: {"description": "token 不存在或不属于该命名空间"},
        409: {"description": "预占已消费（重复/并发确认）或当前存在名称冲突；冲突时不写入"},
        410: {"description": "预占已过期"},
    },
)
def claim_reservation(namespace: str, token: str, conn=Depends(get_db)) -> dict:
    """确认预占：单事务校验命名空间、有效期、消费状态与当前冲突后注册。

    同一 token 并发或重复确认只能一次成功；过期返回 410；
    冲突或重复消费返回 409 且不写入 display_names。
    """
    try:
        return service.claim_reservation(conn, namespace, token)
    except service.ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "reservation_not_found",
                "message": "预占不存在或不属于该命名空间",
            },
        )
    except service.ReservationExpiredError:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": "reservation_expired",
                "message": "预占已过期，未执行注册",
            },
        )
    except service.ReservationConsumedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "reservation_consumed",
                "message": "预占已被确认消费，同一 token 只能成功注册一次",
            },
        )
    except service.NameConflictError as exc:
        # 事务内复查发现冲突（持锁后最新状态）：不写入，返回冲突明细
        conflicts = service.find_conflicts(conn, namespace, exc.normalized_name or "")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "确认预占时与已注册名称或其他未过期预占冲突，未执行注册",
                "normalized_name": exc.normalized_name,
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
