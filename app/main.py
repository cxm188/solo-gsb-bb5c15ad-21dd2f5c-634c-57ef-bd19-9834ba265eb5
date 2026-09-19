"""FastAPI 应用入口。

接口一览：
- POST /namespaces/{namespace}/names                              注册显示名（201 / 409 / 422）
- POST /namespaces/{namespace}/names/batch                        批量注册（201 / 409 / 422，单事务）
- POST /namespaces/{namespace}/reservations                       预占名称（201 / 409 / 422）
- POST /namespaces/{namespace}/reservations/{token}/claim         确认预占并注册（201 / 404 / 409 / 410）
- POST /namespaces/{namespace}/check                              只分析不落库（200）
- GET  /namespaces/{namespace}/names                              列出命名空间内已注册名称
- GET  /health                                                    健康检查
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
    ClaimedNameRecord,
    NameRecord,
    NameRequest,
    ReservationRecord,
    ReservationRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时自动初始化数据库（建库建表，幂等，含向新表的自动迁移）
    database.init_db()
    yield


app = FastAPI(
    title="Unicode Display Name Anti-Spoofing API",
    version="1.1.0",
    description=(
        "命名空间内的显示名防仿冒：NFKC+casefold 规范名查重、"
        "confusables 同形字符判定、Latin/Cyrillic/Greek 混脚本拦截；"
        "支持预占后确认注册（reservations / claim），未过期预占与已注册"
        "名称一样参与全部冲突判定。"
    ),
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = database.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _mixed_script_error(analysis: dict) -> HTTPException:
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


def _conflict_error(normalized_name: str, conflicts: list[dict], message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "name_conflict",
            "message": message,
            "normalized_name": normalized_name,
            "conflicts": conflicts,
        },
    )


CONFLICT_MESSAGE = "规范名相同或与命名空间内已有名称（含未过期预占）同形"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/namespaces/{namespace}/names",
    status_code=status.HTTP_201_CREATED,
    response_model=NameRecord,
    responses={
        409: {"description": "规范名相同或与已有名称/有效预占同形（confusable）"},
        422: {"description": "混用 Latin/Cyrillic/Greek 中两种及以上脚本"},
    },
)
def register_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise _mixed_script_error(analysis)

    if analysis["conflicts"]:
        raise _conflict_error(
            analysis["normalized_name"], analysis["conflicts"], CONFLICT_MESSAGE
        )

    try:
        return service.register_name(
            conn, namespace, payload.name, analysis["normalized_name"]
        )
    except service.NameConflictError as exc:
        # 并发下唯一约束/事务复查兜底：优先用事务内复查结果，否则重新查出冲突
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, analysis["normalized_name"]
        )
        raise _conflict_error(
            analysis["normalized_name"], conflicts, CONFLICT_MESSAGE
        )


@app.post(
    "/namespaces/{namespace}/names/batch",
    status_code=status.HTTP_201_CREATED,
    response_model=BatchRegisterResponse,
    responses={
        409: {"description": "并发竞争使事务判定失效：已回滚，返回重新分析后的冲突明细"},
        422: {"description": "批次中存在混脚本或库内/预占/批内冲突：汇总明细，未写入任何记录"},
    },
)
def register_names_batch(
    namespace: str, payload: BatchNameRequest, conn=Depends(get_db)
) -> dict:
    # 整批与库内已有名称及有效预占联合分析：冲突 + 批内两两冲突
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
    "/namespaces/{namespace}/reservations",
    status_code=status.HTTP_201_CREATED,
    response_model=ReservationRecord,
    responses={
        409: {"description": "规范名相同或与已有名称/有效预占同形（confusable）"},
        422: {"description": "混脚本违规，或 ttl_seconds 超出 30–600 范围"},
    },
)
def reserve_name(
    namespace: str, payload: ReservationRequest, conn=Depends(get_db)
) -> dict:
    """预占名称：复用规范化 / 混脚本 / 同形判定，返回不可猜测 token。

    未过期、未消费的预占与已注册名称一样阻止后续单条注册、批量注册
    及新预占；预占记录在确认（claim）后才真正写入 display_names。
    """
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise _mixed_script_error(analysis)

    if analysis["conflicts"]:
        raise _conflict_error(
            analysis["normalized_name"], analysis["conflicts"], CONFLICT_MESSAGE
        )

    try:
        return service.create_reservation(
            conn, namespace, payload.name, payload.ttl_seconds
        )
    except service.NameConflictError as exc:
        # 并发：取写锁后复查发现冲突（含其他请求刚刚创建的有效预占）
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, analysis["normalized_name"]
        )
        raise _conflict_error(
            analysis["normalized_name"], conflicts, CONFLICT_MESSAGE
        )


@app.post(
    "/namespaces/{namespace}/reservations/{token}/claim",
    status_code=status.HTTP_201_CREATED,
    response_model=ClaimedNameRecord,
    responses={
        404: {"description": "token 不存在或不属于该命名空间"},
        409: {"description": "预占已被消费（重复/并发确认），或当前与已有名称/其他有效预占冲突"},
        410: {"description": "预占已过期（记录被惰性清理）"},
    },
)
def claim_reservation(namespace: str, token: str, conn=Depends(get_db)) -> dict:
    """在单个 BEGIN IMMEDIATE 事务中校验并确认预占：

    命名空间匹配 -> 有效期（过期 410）-> 消费状态（重复确认 409）
    -> 当前冲突（409，不写入）；全部通过才写 display_names 并标记已消费。
    """
    try:
        return service.claim_reservation(conn, namespace, token)
    except service.ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "reservation_not_found",
                "message": "预占不存在，或不属于该命名空间",
            },
        )
    except service.ReservationExpiredError:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": "reservation_expired",
                "message": "预占已过期且未确认，已失效",
            },
        )
    except service.ReservationConsumedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "reservation_already_claimed",
                "message": "该预占已确认注册，不能重复消费",
            },
        )
    except service.NameConflictError as exc:
        # 事务内复查发现冲突：未写入任何记录。回滚后重新查询保证返回最新明细
        reservation = conn.execute(
            "SELECT normalized_name FROM name_reservations WHERE token = ?",
            (token,),
        ).fetchone()
        normalized_name = reservation["normalized_name"] if reservation else ""
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, normalized_name
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "确认时名称与已注册名称或其他有效预占冲突，未写入",
                "normalized_name": normalized_name,
                "conflicts": conflicts or [],
            },
        )


@app.post(
    "/namespaces/{namespace}/check",
    response_model=AnalysisResult,
)
def check_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    """只分析不落库：返回规范名、脚本分析与潜在冲突（含未过期预占）。"""
    return service.analyze_name(conn, namespace, payload.name)


@app.get(
    "/namespaces/{namespace}/names",
    response_model=list[NameRecord],
)
def list_names(namespace: str, conn=Depends(get_db)) -> list[dict]:
    return service.list_names(conn, namespace)
