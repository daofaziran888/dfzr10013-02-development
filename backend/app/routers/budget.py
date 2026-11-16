"""预算核算接口：预算条目、费用/冲销导入、核算引擎与对账附件。

路由挂在财务多维分析（/api/finance）之下，体现预算核算是财务模块的核算能力；
路由层只做参数校验与结果包装，业务判断全部在 BudgetService 内完成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, PageResult
from app.services.budget import BudgetService

router = APIRouter(prefix="/api/finance/budget", tags=["财务预算核算"])

service = BudgetService()


class ExpenseImportPayload(BaseModel):
    """费用凭证导入：同一凭证号多行即多个分摊项。"""

    批次号: str | None = None
    费用明细: list[dict[str, Any]] = Field(default_factory=list)


class ReversalImportPayload(BaseModel):
    """冲销记录导入：冲销行通过原凭证号指向费用凭证。"""

    批次号: str | None = None
    冲销记录: list[dict[str, Any]] = Field(default_factory=list)


class BudgetItemPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- 预算条目
@router.get("/items", response_model=PageResult[dict])
def list_budget_items(
    period: str | None = Query(default=None, description="按预算周期过滤，如 2026-Q4"),
    account: str | None = Query(default=None, description="按科目编号过滤"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = service.list_items(period=period, account=account)
    total = len(rows)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=total, page=page, size=size)


@router.post("/items", response_model=ActionResult)
def create_budget_item(payload: BudgetItemPayload) -> ActionResult:
    entry, message = service.create_item(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="预算条目已登记", entry=entry)


# ---------------------------------------------------------------- 数据导入
@router.post("/import/expenses")
def import_expenses(payload: ExpenseImportPayload) -> dict[str, Any]:
    """导入费用凭证；重复导入按内容指纹只记一次，异常行单列原因。"""
    result = service.import_expenses(payload.model_dump())
    return {"ok": True, **result}


@router.post("/import/reversals")
def import_reversals(payload: ReversalImportPayload) -> dict[str, Any]:
    """导入冲销记录；缺少原凭证、比例不符、金额无效等行单列原因。"""
    result = service.import_reversals(payload.model_dump())
    return {"ok": True, **result}


@router.get("/import/batches")
def list_batches() -> dict[str, Any]:
    rows = service.list_batches()
    return {"total": len(rows), "items": rows}


# ---------------------------------------------------------------- 凭证与冲销
@router.get("/vouchers", response_model=PageResult[dict])
def list_vouchers(
    keyword: str | None = Query(default=None, description="按凭证号检索"),
    status: str | None = Query(default=None, description="有效、无效"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_vouchers(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/vouchers/{voucher_id}/reconciliation")
def voucher_reconciliation(voucher_id: int) -> dict[str, Any]:
    """逐笔费用对账：分摊、冲销、剩余金额逐项可核，附件金额与原始费用闭合。"""
    result = service.voucher_reconciliation(voucher_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"费用凭证 {voucher_id} 不存在")
    return result


@router.get("/reversals")
def list_reversals(
    keyword: str | None = Query(default=None, description="按冲销号/原凭证号检索"),
    status: str | None = Query(default=None, description="已完成、未完成"),
) -> dict[str, Any]:
    rows = service.list_reversals(keyword=keyword, status=status)
    return {"total": len(rows), "items": rows}


@router.get("/exceptions")
def list_exceptions() -> dict[str, Any]:
    """异常行清单：缺少原凭证、比例合计不符、金额无效等均带原因。"""
    rows = service.list_exceptions()
    return {"total": len(rows), "items": rows}


# ---------------------------------------------------------------- 核算引擎
@router.post("/accounting/run")
def run_accounting(remark: str = Query(default="", description="本次核算备注")) -> ActionResult:
    """执行预算核算，生成核算依据快照与预算执行差异数据集。"""
    basis = service.run_accounting(trigger="核算", remark=remark)
    return ActionResult(ok=True, message=f"核算完成，依据编号 {basis['依据编号']}", entry={
        k: v for k, v in basis.items() if k != "快照"
    })


@router.post("/accounting/recover")
def recover_accounting() -> ActionResult:
    """核算恢复：数据未变复用依据，数据变化才重新核算，不重复分摊。"""
    result = service.recover_accounting()
    return ActionResult(ok=True, message=result.pop("提示", "核算恢复完成"), entry=result)


@router.post("/accounting/correct")
def correct_accounting(remark: str = Query(default="凭证或预算更正后重新核算")) -> ActionResult:
    """更正生成新核算依据，旧报告仍绑定旧依据对应的预算与凭证集合。"""
    basis = service.correct_accounting(remark=remark)
    return ActionResult(ok=True, message=f"更正核算完成，新依据编号 {basis['依据编号']}", entry={
        k: v for k, v in basis.items() if k != "快照"
    })


@router.get("/bases")
def list_bases() -> dict[str, Any]:
    """核算依据版本列表（快照内容较大，列表不返回快照）。"""
    rows = service.list_bases()
    return {"total": len(rows), "items": rows}


@router.get("/bases/{basis_id}")
def get_basis(basis_id: int) -> dict[str, Any]:
    basis = service.get_basis(basis_id)
    if basis is None:
        raise HTTPException(status_code=404, detail=f"核算依据 {basis_id} 不存在")
    return basis


@router.get("/execution")
def list_execution(
    basis_id: int | None = Query(default=None, description="核算依据 id，缺省取最新版本"),
    account: str | None = Query(default=None, description="按科目编号过滤"),
    department: str | None = Query(default=None, description="按部门过滤"),
) -> dict[str, Any]:
    """预算执行差异数据集：供科目报告端按科目与部门取数。"""
    if basis_id is not None and service.get_basis(basis_id) is None:
        raise HTTPException(status_code=404, detail=f"核算依据 {basis_id} 不存在")
    rows = service.list_execution(basis_id=basis_id, account=account, department=department)
    return {"basis_id": basis_id or (service.latest_basis() or {}).get("id"),
            "total": len(rows), "items": rows}


@router.post("/attachments")
def build_attachment(
    basis_id: int | None = Query(default=None, description="依据 id，缺省取最新；旧报告传旧 id"),
) -> ActionResult:
    """生成按科目与部门展开的预算、实际、差额和分摊来源对账附件。"""
    try:
        attachment = service.build_attachment(basis_id=basis_id)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"对账附件 {attachment['附件编号']} 已生成", entry=attachment)


@router.get("/attachments")
def list_attachments() -> dict[str, Any]:
    from app.store import store

    from app.services.budget import T_ATTACHMENTS

    rows = store.rows(T_ATTACHMENTS)
    return {"total": len(rows), "items": [{k: v for k, v in row.items()
                                           if k not in ("主体", "凭证闭合校验", "异常清单")} for row in rows]}


@router.get("/attachments/{attachment_id}")
def get_attachment(attachment_id: int) -> dict[str, Any]:
    from app.store import store

    from app.services.budget import T_ATTACHMENTS

    for row in store.rows(T_ATTACHMENTS):
        if int(row.get("id", 0)) == attachment_id:
            return row
    raise HTTPException(status_code=404, detail=f"对账附件 {attachment_id} 不存在")
