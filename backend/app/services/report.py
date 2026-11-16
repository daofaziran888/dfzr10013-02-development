"""经营分析报告业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "report"
REQUIRED_FIELDS = ["报告编号", "报告主题", "分析周期"]
STATUS_ORDER = ["草稿", "已生成", "已分发", "已归档"]
ACTION_RULES = {"生成报告": "草稿", "分发报告": "已生成", "归档报告": "已分发"}
NEGATIVE_ACTIONS = []


class ReportService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于经营分析报告可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"分析报告已{action}"

    # ---------------------------------------------------------- 跨模块：科目对账附件
    def build_budget_attachment(self, entry_id: int, basis_id: int | None = None) -> tuple[dict[str, Any] | None, str]:
        """科目报告端消费预算执行差异数据集，生成并绑定科目对账附件。

        附件绑定核算依据 id：后续预算更正产生新依据时，旧报告仍指向旧依据快照，
        使用原来的预算和凭证集合，不被新版本覆盖。
        """
        from app.services.budget import BudgetService

        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析报告 {entry_id} 不存在或已归档"
        budget_service = BudgetService()
        try:
            attachment = budget_service.build_attachment(basis_id=basis_id)
        except ValueError as exc:
            return None, str(exc)
        bound = entry.setdefault("预算对账附件", [])
        bound.append({
            "附件编号": attachment["附件编号"],
            "附件id": attachment["id"],
            "核算依据编号": attachment["核算依据编号"],
            "依据id": attachment["依据id"],
            "标题": attachment["标题"],
            "生成时间": attachment["生成时间"],
        })
        # 数据附件是报告既有字段，同步写入便于列表页展示。
        entry["数据附件"] = "、".join(item["附件编号"] for item in bound)
        return attachment, ""
