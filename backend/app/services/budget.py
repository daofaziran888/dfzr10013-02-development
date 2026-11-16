"""预算核算引擎：读取预算条目、费用凭证与冲销记录，关联科目并分摊核算。

设计要点（对应需求口径）：

* 金额内部一律用最小货币单位整数，分摊用最大余数法闭合，尾差逐行留痕；
* 冲销按原凭证各分摊项的实际占比拆分，支持部分冲销与同一凭证多笔冲销；
* 只有「已完成」且发生日期落在预算周期内的冲销才计入实际金额，未完成冲销单列；
* 预算周期与发生日期共同决定计入范围，周期外发生的单据登记异常、不进执行数据集；
* 不同币种分组隔离，任何汇总都不跨币种相加；
* 导入按内容指纹去重（重复导入只记一次），批次号提供整批幂等；
* 每次核算生成不可变的「核算依据」快照，更正产生新版本，旧报告仍绑定旧依据。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.services.budget_constants import (
    ALL_TABLES,
    PERIOD_GRANULARITY,
    REVERSAL_DONE,
    REVERSAL_PENDING,
    T_ALLOCATIONS,
    T_ATTACHMENTS,
    T_BASES,
    T_BATCHES,
    T_BUDGET_ITEMS,
    T_EXCEPTIONS,
    T_EXECUTION,
    T_REVERSALS,
    T_RUNS,
    T_VOUCHERS,
    VOUCHER_INVALID,
    VOUCHER_VALID,
)
from app.services.budget_money import (
    RATIO_QUANT,
    allocate_by_ratio,
    currency_scale,
    format_amount,
    parse_ratio,
    period_bounds,
    resolve_period,
    to_decimal,
    to_minor,
)
from app.store import store


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _fingerprint(payload: Any) -> str:
    """对规范化后的业务内容计算指纹，作为重复导入判据。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class BudgetService:
    # ------------------------------------------------------------------ 预算条目
    def list_items(self, *, period: str | None = None, account: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_BUDGET_ITEMS)
        if period:
            rows = [row for row in rows if row.get("预算周期") == period]
        if account:
            rows = [row for row in rows if account in str(row.get("科目编号", ""))]
        return rows

    def create_item(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记预算条目：关联科目必须存在，周期与金额必须可解析，同口径不重复。"""
        account = str(values.get("科目编号") or "").strip()
        department = str(values.get("部门") or "").strip()
        period = str(values.get("预算周期") or "").strip()
        currency = str(values.get("币种") or "CNY").strip().upper()
        if not account or not department or not period:
            return None, "科目编号、部门、预算周期为必填项"
        if not self._find_account(account):
            return None, f"科目 {account} 不在财务科目表中，无法关联核算"
        if period_bounds(period) is None:
            return None, f"预算周期 {period} 口径无法识别（支持 YYYY / YYYY-Qn / YYYY-MM）"
        minor = to_minor(values.get("预算金额"), currency)
        if minor is None:
            return None, "预算金额无效，需为不小于零的数字"
        for row in store.rows(T_BUDGET_ITEMS):
            if (row.get("预算周期"), row.get("科目编号"), row.get("部门"), row.get("币种")) == (period, account, department, currency):
                return None, "同周期、科目、部门、币种的预算条目已存在，请勿重复登记"
        new_id = _next_id(T_BUDGET_ITEMS)
        entry = {
            "id": new_id,
            "预算条目编号": f"BGT-{new_id:04d}",
            "科目编号": account,
            "科目名称": self._account_name(account),
            "部门": department,
            "预算周期": period,
            "币种": currency,
            "预算金额_最小单位": minor,
            "预算金额": float(to_decimal(minor, currency)),
            "登记时间": _now(),
        }
        store.rows(T_BUDGET_ITEMS).append(entry)
        return entry, ""

    def _find_account(self, account: str) -> dict[str, Any] | None:
        for row in store.rows("finance"):
            if str(row.get("科目编号")) == account:
                return row
        return None

    def _account_name(self, account: str) -> str:
        row = self._find_account(account)
        return str(row.get("科目名称", "")) if row else ""

    # ------------------------------------------------------------------ 费用导入
    def import_expenses(self, payload: dict[str, Any]) -> dict[str, Any]:
        """导入费用凭证。同一凭证号的多行视为多个分摊项，按内容指纹去重。"""
        batch_no = str(payload.get("批次号") or "").strip() or f"IMP-E-{_now().replace(' ', '')}"
        if self._batch_seen(batch_no):
            return self._batch_skip_result(batch_no, "费用")
        lines = payload.get("费用明细") or []
        accepted, duplicated, failed = 0, 0, 0
        fingerprints: list[str] = []
        # 先按凭证号归组，保留导入顺序。
        grouped: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for line in lines:
            voucher_no = str(line.get("凭证号") or "").strip()
            if voucher_no not in grouped:
                grouped[voucher_no] = []
                order.append(voucher_no)
            grouped[voucher_no].append(line)

        for voucher_no in order:
            group = grouped[voucher_no]
            record = self._build_voucher(voucher_no, group)
            fp = record["指纹"]
            fingerprints.append(fp)
            if record["状态"] == VOUCHER_INVALID:
                failed += 1
                continue
            if self._fingerprint_exists(T_VOUCHERS, fp):
                # 内容完全一致的重复导入：只记一次，不产生任何副作用。
                duplicated += 1
                continue
            if self._voucher_no_exists(voucher_no):
                # 同号不同内容属于凭证号冲突，单列异常，拒绝重复登记。
                self._add_exception(
                    "凭证号冲突", "费用", voucher_no,
                    f"凭证号 {voucher_no} 已存在但本次内容与原凭证不一致，已拒绝重复登记",
                    record,
                )
                failed += 1
                continue
            record["id"] = _next_id(T_VOUCHERS)
            store.rows(T_VOUCHERS).append(record)
            for alloc in record["分摊项"]:
                alloc["id"] = _next_id(T_ALLOCATIONS)
                alloc["凭证id"] = record["id"]
                store.rows(T_ALLOCATIONS).append(alloc)
            accepted += 1

        batch = {
            "id": _next_id(T_BATCHES),
            "批次号": batch_no,
            "类型": "费用导入",
            "指纹": fingerprints,
            "导入时间": _now(),
            "接收行数": accepted,
            "重复跳过行数": duplicated,
            "异常行数": failed,
        }
        store.rows(T_BATCHES).append(batch)
        return {
            "批次号": batch_no, "接收": accepted, "重复跳过": duplicated,
            "异常": failed, "提示": "重复导入只记一次，异常行已单列原因",
        }

    def _build_voucher(self, voucher_no: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        """把同一凭证号的导入行构造成凭证头 + 分摊项，并完成金额/比例校验。"""
        first = lines[0]
        occur_date = str(first.get("发生日期") or "").strip()
        currency = str(first.get("币种") or "CNY").strip().upper()
        header_minor = to_minor(first.get("金额"), currency)
        base = {
            "凭证号": voucher_no,
            "发生日期": occur_date,
            "币种": currency,
            "导入时间": _now(),
            "分摊项": [],
            "状态": VOUCHER_VALID,
        }
        if not voucher_no:
            base["状态"] = VOUCHER_INVALID
            base["异常原因"] = "凭证号缺失"
            self._add_exception("凭证号缺失", "费用", voucher_no or "(空)", "导入行缺少凭证号", first)
            base["指纹"] = _fingerprint(["no-no", occur_date, currency, header_minor])
            return base
        if header_minor is None:
            base["状态"] = VOUCHER_INVALID
            base["异常原因"] = "金额无效"
            self._add_exception("金额无效", "费用", voucher_no, f"凭证金额 {first.get('金额')!r} 无法解析为非负数字", first)
            base["指纹"] = _fingerprint([voucher_no, occur_date, currency, str(first.get("金额"))])
            return base
        base["金额_最小单位"] = header_minor
        base["金额"] = float(to_decimal(header_minor, currency))

        # 单行且未给比例：视为 100% 承担；多行必须给分摊比例。
        ratio_inputs: list[Decimal | None] = []
        for line in lines:
            ratio_inputs.append(parse_ratio(line.get("分摊比例")) if len(lines) > 1 else Decimal(1))
        if len(lines) == 1 and lines[0].get("分摊比例") not in (None, ""):
            ratio_inputs = [parse_ratio(lines[0].get("分摊比例"))]

        if any(ratio is None for ratio in ratio_inputs):
            base["状态"] = VOUCHER_INVALID
            base["异常原因"] = "分摊比例无效"
            self._add_exception("分摊比例无效", "费用", voucher_no, "存在无法解析的分摊比例", lines)
            base["指纹"] = _fingerprint([voucher_no, occur_date, currency, header_minor, "bad-ratio"])
            return base
        ratios = [ratio or Decimal(0) for ratio in ratio_inputs]
        ratio_total = sum(ratios, Decimal(0)).quantize(RATIO_QUANT)
        if len(lines) > 1 and ratio_total != Decimal(1):
            base["状态"] = VOUCHER_INVALID
            base["异常原因"] = "比例合计不符"
            self._add_exception(
                "比例合计不符", "费用", voucher_no,
                f"分摊比例合计为 {ratio_total}，不等于 1，凭证未参与核算", lines,
            )
            base["指纹"] = _fingerprint([voucher_no, occur_date, currency, header_minor, str(ratio_total)])
            return base

        minor_parts = allocate_by_ratio(header_minor, ratios)
        raw_floors = [int(Decimal(header_minor) * r / (ratio_total or Decimal(1))) for r in ratios]
        allocations: list[dict[str, Any]] = []
        for line, ratio, part_minor, floor_minor in zip(lines, ratios, minor_parts, raw_floors):
            account = str(line.get("科目编号") or "").strip()
            department = str(line.get("部门") or "").strip()
            if not self._find_account(account):
                self._add_exception(
                    "科目未关联", "费用", voucher_no,
                    f"分摊科目 {account or '(空)'} 不在财务科目表，该分摊项保留但不进入执行数据集", line,
                )
            allocations.append({
                "凭证号": voucher_no,
                "科目编号": account,
                "科目名称": self._account_name(account),
                "部门": department,
                "币种": currency,
                "分摊比例": str(ratio),
                "分摊金额_最小单位": part_minor,
                "分摊金额": float(to_decimal(part_minor, currency)),
                "取整尾差_最小单位": part_minor - floor_minor,
                "发生日期": occur_date,
            })
        base["分摊项"] = allocations
        # 闭合校验：分摊合计必须与原费用分毫不差。
        if sum(item["分摊金额_最小单位"] for item in allocations) != header_minor:  # pragma: no cover - 防御性校验
            raise RuntimeError(f"凭证 {voucher_no} 分摊未闭合，禁止入账")
        base["指纹"] = _fingerprint([
            voucher_no, occur_date, currency, header_minor,
            sorted((a["科目编号"], a["部门"], a["分摊比例"]) for a in allocations),
        ])
        return base

    # ------------------------------------------------------------------ 冲销导入
    def import_reversals(self, payload: dict[str, Any]) -> dict[str, Any]:
        """导入冲销记录。冲销行必须指向已存在的有效原凭证，未完成冲销不计入实际。"""
        batch_no = str(payload.get("批次号") or "").strip() or f"IMP-R-{_now().replace(' ', '')}"
        if self._batch_seen(batch_no):
            return self._batch_skip_result(batch_no, "冲销")
        lines = payload.get("冲销记录") or []
        accepted, duplicated, failed = 0, 0, 0
        fingerprints: list[str] = []
        for line in lines:
            record, reason = self._build_reversal(line)
            fp = record["指纹"]
            fingerprints.append(fp)
            if reason:
                failed += 1
                continue
            if self._fingerprint_exists(T_REVERSALS, fp):
                duplicated += 1
                continue
            record["id"] = _next_id(T_REVERSALS)
            store.rows(T_REVERSALS).append(record)
            accepted += 1
        batch = {
            "id": _next_id(T_BATCHES),
            "批次号": batch_no,
            "类型": "冲销导入",
            "指纹": fingerprints,
            "导入时间": _now(),
            "接收行数": accepted,
            "重复跳过行数": duplicated,
            "异常行数": failed,
        }
        store.rows(T_BATCHES).append(batch)
        return {
            "批次号": batch_no, "接收": accepted, "重复跳过": duplicated,
            "异常": failed, "提示": "未完成冲销已登记但不计入实际金额",
        }

    def _build_reversal(self, line: dict[str, Any]) -> tuple[dict[str, Any], str]:
        reversal_no = str(line.get("冲销号") or "").strip()
        original_no = str(line.get("原凭证号") or "").strip()
        occur_date = str(line.get("发生日期") or "").strip()
        currency = str(line.get("币种") or "CNY").strip().upper()
        status = str(line.get("冲销状态") or REVERSAL_DONE).strip()
        minor = to_minor(line.get("金额"), currency)
        fp_payload = [reversal_no, original_no, occur_date, currency, minor, status,
                      str(line.get("冲销项") or [])]
        record = {
            "冲销号": reversal_no or f"CX-{original_no}-{minor}-{occur_date}",
            "原凭证号": original_no,
            "发生日期": occur_date,
            "币种": currency,
            "冲销状态": status if status in (REVERSAL_DONE, REVERSAL_PENDING) else REVERSAL_PENDING,
            "导入时间": _now(),
            "指纹": _fingerprint(fp_payload),
        }
        if minor is None:
            self._add_exception("金额无效", "冲销", reversal_no or original_no,
                                f"冲销金额 {line.get('金额')!r} 无法解析为非负数字", line)
            return record, "金额无效"
        record["金额_最小单位"] = minor
        record["金额"] = float(to_decimal(minor, currency))

        voucher = self._find_voucher(original_no)
        if voucher is None:
            self._add_exception("缺少原凭证", "冲销", record["冲销号"],
                                f"冲销指向的原凭证 {original_no or '(空)'} 不存在或无效", line)
            return record, "缺少原凭证"
        if voucher.get("币种") != currency:
            self._add_exception("币种不一致", "冲销", record["冲销号"],
                                f"冲销币种 {currency} 与原凭证 {voucher.get('币种')} 不一致", line)
            return record, "币种不一致"
        # 冲销总额（含未完成的预约占用）不得超过原费用。
        occupied = sum(r.get("金额_最小单位", 0) for r in store.rows(T_REVERSALS)
                       if r.get("原凭证号") == original_no)
        if occupied + minor > voucher["金额_最小单位"]:
            self._add_exception(
                "冲销超额", "冲销", record["冲销号"],
                f"累计冲销 {format_amount(occupied + minor, currency)} 超过原凭证 "
                f"{format_amount(voucher['金额_最小单位'], currency)}", line,
            )
            return record, "冲销超额"
        # 自定义冲销分摊项需要比例合计为 1 且与原凭证分摊项一一对应；不给则按原凭证实际占比拆分。
        custom = line.get("冲销项") or []
        if custom:
            alloc_count = len([a for a in store.rows(T_ALLOCATIONS) if a.get("凭证id") == voucher["id"]])
            ratios = [parse_ratio(item.get("分摊比例")) for item in custom]
            if (len(custom) != alloc_count or any(r is None for r in ratios)
                    or sum(r or Decimal(0) for r in ratios).quantize(RATIO_QUANT) != Decimal(1)):
                self._add_exception(
                    "比例合计不符", "冲销", record["冲销号"],
                    f"冲销分摊项需与原凭证 {alloc_count} 个分摊项一一对应且比例合计为 1", line,
                )
                return record, "比例合计不符"
            record["冲销项"] = custom
        else:
            record["冲销项"] = []
        return record, ""

    # ------------------------------------------------------------------ 对账查询
    def list_vouchers(self, *, keyword: str | None = None, status: str | None = None,
                      page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(T_VOUCHERS)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("凭证号", ""))]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_reversals(self, *, keyword: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_REVERSALS)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("原凭证号", "")) or keyword in str(row.get("冲销号", ""))]
        if status:
            rows = [row for row in rows if row.get("冲销状态") == status]
        return rows

    def list_exceptions(self) -> list[dict[str, Any]]:
        """导入环节持久化的异常行；核算环节派生的异常随核算依据快照返回。"""
        return store.rows(T_EXCEPTIONS)

    def list_batches(self) -> list[dict[str, Any]]:
        return store.rows(T_BATCHES)

    def voucher_reconciliation(self, voucher_id: int) -> dict[str, Any] | None:
        """逐笔费用对账：分摊、冲销、剩余逐项可核，附件金额与原始费用闭合。"""
        voucher = next((row for row in store.rows(T_VOUCHERS) if int(row.get("id", 0)) == voucher_id), None)
        if voucher is None:
            return None
        currency = voucher["币种"]
        allocations = [a for a in store.rows(T_ALLOCATIONS) if a.get("凭证id") == voucher_id]
        reversals = [r for r in store.rows(T_REVERSALS) if r.get("原凭证号") == voucher["凭证号"]]

        reversal_views: list[dict[str, Any]] = []
        original_period = resolve_period(voucher["发生日期"], PERIOD_GRANULARITY)
        done_in_period = 0
        done_out_period = 0
        pending_total = 0
        for reversal in reversals:
            pieces = self._split_reversal_amount(
                voucher, allocations, reversal["金额_最小单位"], reversal.get("冲销项"),
            )
            reversal_period = resolve_period(reversal["发生日期"], PERIOD_GRANULARITY)
            # 冲销抵减归属原凭证周期：同周期冲销计入实际，跨期冲销单列但不抵减原周期剩余。
            same_period = reversal_period == original_period
            in_any_period = self._in_any_budget_period(reversal["发生日期"])
            if reversal["冲销状态"] == REVERSAL_PENDING:
                pending_total += reversal["金额_最小单位"]
            elif same_period:
                done_in_period += reversal["金额_最小单位"]
            else:
                done_out_period += reversal["金额_最小单位"]
            reversal_views.append({
                "冲销号": reversal["冲销号"],
                "发生日期": reversal["发生日期"],
                "冲销发生周期": reversal_period,
                "原凭证周期": original_period,
                "冲销状态": reversal["冲销状态"],
                "金额": format_amount(reversal["金额_最小单位"], currency),
                "金额_最小单位": reversal["金额_最小单位"],
                "是否在预算周期内": in_any_period,
                "是否跨期": reversal_period != original_period,
                "是否计入实际": reversal["冲销状态"] == REVERSAL_DONE and same_period,
                "冲销分摊": pieces,
            })

        original = voucher["金额_最小单位"]
        allocated_total = sum(a["分摊金额_最小单位"] for a in allocations)
        # 对账视图：只有同周期已完成冲销才抵减原周期剩余；跨期与未完成冲销单列。
        completed_total = done_in_period
        remaining = original - completed_total
        return {
            "凭证号": voucher["凭证号"],
            "状态": voucher["状态"],
            "发生日期": voucher["发生日期"],
            "所属周期": resolve_period(voucher["发生日期"], PERIOD_GRANULARITY),
            "币种": currency,
            "原费用": format_amount(original, currency),
            "原费用_最小单位": original,
            "分摊明细": [
                {
                    "科目编号": a["科目编号"], "部门": a["部门"], "分摊比例": a["分摊比例"],
                    "分摊金额": format_amount(a["分摊金额_最小单位"], currency),
                    "分摊金额_最小单位": a["分摊金额_最小单位"],
                    "取整尾差": a["取整尾差_最小单位"],
                } for a in allocations
            ],
            "分摊合计": format_amount(allocated_total, currency),
            "冲销记录": reversal_views,
            "周期内已完成冲销": format_amount(done_in_period, currency),
            "周期外已完成冲销": format_amount(done_out_period, currency),
            "未完成冲销": format_amount(pending_total, currency),
            "剩余金额": format_amount(remaining, currency),
            "剩余金额_最小单位": remaining,
            "闭合校验": {
                "分摊等于原费用": allocated_total == original,
                "剩余加同周期冲销等于原费用": remaining + done_in_period == original,
                "是否闭合": (allocated_total == original
                          and remaining + done_in_period == original
                          and done_in_period + done_out_period + pending_total <= original),
            },
        }

    def _split_reversal_amount(
        self,
        voucher: dict[str, Any],
        allocations: list[dict[str, Any]],
        reversal_minor: int,
        custom_items: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """拆分冲销金额，最大余数法保证冲销拆分闭合。

        默认按原凭证各分摊项的实际占比拆分；若冲销行显式给出冲销项比例且与分摊项
        一一对应，则采用自定义比例（科目/部门仍沿用原分摊项，保证可追溯）。
        """
        total = voucher["金额_最小单位"]
        if not allocations:
            return []
        if custom_items and len(custom_items) == len(allocations):
            custom_ratios = [parse_ratio(item.get("分摊比例")) for item in custom_items]
            if all(ratio is not None for ratio in custom_ratios) and sum(
                ratio or Decimal(0) for ratio in custom_ratios
            ).quantize(RATIO_QUANT) == Decimal(1):
                ratios = [ratio or Decimal(0) for ratio in custom_ratios]
            else:  # 导入时已校验，此处兜底回退到实际占比
                ratios = [Decimal(a["分摊金额_最小单位"]) / Decimal(total) for a in allocations]
        else:
            ratios = [Decimal(a["分摊金额_最小单位"]) / Decimal(total) for a in allocations]
        pieces = allocate_by_ratio(reversal_minor, ratios)
        return [
            {
                "科目编号": a["科目编号"], "部门": a["部门"], "币种": voucher["币种"],
                "冲销金额_最小单位": piece,
                "冲销金额": format_amount(piece, voucher["币种"]),
            }
            for a, piece in zip(allocations, pieces)
        ]

    # ------------------------------------------------------------------ 核算引擎
    def run_accounting(self, *, trigger: str = "核算", remark: str = "") -> dict[str, Any]:
        """执行一次预算核算，生成不可变的核算依据快照与预算执行差异数据集。"""
        ledger = self._compute_ledger()
        data_fingerprint = _fingerprint({
            "预算": sorted((i["预算周期"], i["科目编号"], i["部门"], i["币种"], i["预算金额_最小单位"])
                           for i in store.rows(T_BUDGET_ITEMS)),
            "凭证": sorted(v["指纹"] for v in store.rows(T_VOUCHERS) if v["状态"] == VOUCHER_VALID),
            "冲销": sorted(r["指纹"] for r in store.rows(T_REVERSALS)),
        })
        version = max((int(b.get("版本", 0)) for b in store.rows(T_BASES)), default=0) + 1
        basis_id = _next_id(T_BASES)
        basis_no = f"BAS-{version:03d}"
        basis = {
            "id": basis_id,
            "依据编号": basis_no,
            "版本": version,
            "触发类型": trigger,
            "备注": remark,
            "状态": "已完成",
            "生成时间": _now(),
            "数据指纹": data_fingerprint,
            "快照": ledger["snapshot"],
            "汇总": ledger["summary"],
        }
        store.rows(T_BASES).append(basis)
        for row in ledger["execution"]:
            row["id"] = _next_id(T_EXECUTION)
            row["核算依据编号"] = basis_no
            row["依据id"] = basis_id
            store.rows(T_EXECUTION).append(row)
        run = {
            "id": _next_id(T_RUNS),
            "依据编号": basis_no,
            "触发类型": trigger,
            "执行时间": _now(),
            "状态": "已完成",
            "数据指纹": data_fingerprint,
            "说明": ledger["summary"],
        }
        store.rows(T_RUNS).append(run)
        return basis

    def recover_accounting(self) -> dict[str, Any]:
        """核算恢复：数据未变化时复用最新依据，变化时才生成新版本，杜绝重复分摊。"""
        latest = self.latest_basis()
        current_fp = self._current_data_fingerprint()
        if latest is not None and latest.get("数据指纹") == current_fp:
            return {"复用依据": latest["依据编号"], "提示": "数据未变化，恢复核算未重复分摊", "依据id": latest["id"]}
        return self.run_accounting(trigger="恢复", remark="核算恢复：数据发生变化后重新核算")

    def correct_accounting(self, remark: str = "凭证或预算更正后重新核算") -> dict[str, Any]:
        """更正生成新核算依据；旧依据快照保留，旧报告继续使用原预算与凭证集合。"""
        return self.run_accounting(trigger="更正", remark=remark)

    def _current_data_fingerprint(self) -> str:
        return _fingerprint({
            "预算": sorted((i["预算周期"], i["科目编号"], i["部门"], i["币种"], i["预算金额_最小单位"])
                           for i in store.rows(T_BUDGET_ITEMS)),
            "凭证": sorted(v["指纹"] for v in store.rows(T_VOUCHERS) if v["状态"] == VOUCHER_VALID),
            "冲销": sorted(r["指纹"] for r in store.rows(T_REVERSALS)),
        })

    def _compute_ledger(self) -> dict[str, Any]:
        """核心核算：分摊入账 -> 冲销抵减 -> 关联预算 -> 生成差异数据集。"""
        exceptions: list[dict[str, Any]] = [dict(row) for row in store.rows(T_EXCEPTIONS)]
        movements: list[dict[str, Any]] = []
        snapshot_vouchers: list[dict[str, Any]] = []

        valid_vouchers = [v for v in store.rows(T_VOUCHERS) if v["状态"] == VOUCHER_VALID]
        for voucher in valid_vouchers:
            allocations = [a for a in store.rows(T_ALLOCATIONS) if a.get("凭证id") == voucher["id"]]
            period = resolve_period(voucher["发生日期"], PERIOD_GRANULARITY)
            in_period = self._in_any_budget_period(voucher["发生日期"])
            snapshot_vouchers.append({
                "凭证号": voucher["凭证号"], "发生日期": voucher["发生日期"],
                "币种": voucher["币种"], "金额_最小单位": voucher["金额_最小单位"],
                "所属周期": period, "分摊项": [dict(a) for a in allocations],
            })
            if not in_period:
                exceptions.append(self._derived_exception(
                    "超出预算周期", "费用", voucher["凭证号"],
                    f"发生日期 {voucher['发生日期']} 不在任何预算周期内，未计入执行数据集",
                ))
                continue
            for alloc in allocations:
                if not self._find_account(alloc["科目编号"]):
                    continue  # 科目未关联的异常已在导入时登记
                movements.append({
                    "预算周期": period, "科目编号": alloc["科目编号"],
                    "部门": alloc["部门"], "币种": voucher["币种"],
                    "金额_最小单位": alloc["分摊金额_最小单位"],
                    "来源类型": "费用分摊", "凭证号": voucher["凭证号"],
                    "发生日期": voucher["发生日期"],
                })

        snapshot_reversals: list[dict[str, Any]] = []
        for reversal in store.rows(T_REVERSALS):
            voucher = self._find_voucher(reversal["原凭证号"])
            snapshot_reversals.append(dict(reversal))
            if voucher is None or reversal["冲销状态"] != REVERSAL_DONE:
                # 未完成冲销：保留登记但绝不计入实际金额。
                if reversal["冲销状态"] == REVERSAL_PENDING:
                    exceptions.append(self._derived_exception(
                        "未完成冲销", "冲销", reversal["冲销号"],
                        f"冲销 {reversal['冲销号']} 状态为未完成，未计入实际金额",
                    ))
                continue
            original_period = resolve_period(voucher["发生日期"], PERIOD_GRANULARITY)
            reversal_period = resolve_period(reversal["发生日期"], PERIOD_GRANULARITY)
            allocations = [a for a in store.rows(T_ALLOCATIONS) if a.get("凭证id") == voucher["id"]]
            pieces = self._split_reversal_amount(
                voucher, allocations, reversal["金额_最小单位"], reversal.get("冲销项"),
            )
            if reversal_period != original_period:
                # 跨期冲销：冲销的是原凭证费用，归属原凭证所在周期抵减，并留痕提示；
                # 即使冲销发生周期没有预算条目，也不丢弃该冲销。
                exceptions.append(self._derived_exception(
                    "跨期冲销", "冲销", reversal["冲销号"],
                    f"冲销发生在 {reversal_period}，已归属原凭证周期 {original_period} 抵减",
                ))
            elif not self._in_any_budget_period(reversal["发生日期"]):
                exceptions.append(self._derived_exception(
                    "超出预算周期", "冲销", reversal["冲销号"],
                    f"冲销发生日期 {reversal['发生日期']} 超出预算周期，未抵减实际金额",
                ))
                continue
            for piece in pieces:
                if not self._find_account(piece["科目编号"]):
                    continue
                movements.append({
                    "预算周期": original_period, "科目编号": piece["科目编号"],
                    "部门": piece["部门"], "币种": reversal["币种"],
                    "金额_最小单位": -piece["冲销金额_最小单位"],
                    "来源类型": "冲销抵减", "凭证号": reversal["冲销号"],
                    "发生日期": reversal["发生日期"],
                    "冲销发生周期": reversal_period,
                    "是否跨期": reversal_period != original_period,
                })

        execution = self._build_execution(movements)
        snapshot = {
            "预算条目": [dict(i) for i in store.rows(T_BUDGET_ITEMS)],
            "凭证": snapshot_vouchers,
            "冲销": snapshot_reversals,
            "异常": exceptions,
        }
        summary = {
            "有效凭证数": len(valid_vouchers),
            "分摊行数": sum(len(v["分摊项"]) for v in snapshot_vouchers),
            "冲销行数": len(snapshot_reversals),
            "异常行数": len(exceptions),
            "执行数据集行数": len(execution),
            "涉及币种": sorted({m["币种"] for m in movements}),
        }
        return {"execution": execution, "snapshot": snapshot, "summary": summary}

    def _build_execution(self, movements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按（周期、科目、部门、币种）汇总实际并关联预算，币种之间不相加。"""
        groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for move in movements:
            key = (move["预算周期"], move["科目编号"], move["部门"], move["币种"])
            group = groups.setdefault(key, {
                "预算周期": move["预算周期"], "科目编号": move["科目编号"],
                "科目名称": self._account_name(move["科目编号"]),
                "部门": move["部门"], "币种": move["币种"],
                "实际金额_最小单位": 0, "分摊来源": [],
            })
            group["实际金额_最小单位"] += move["金额_最小单位"]
            group["分摊来源"].append({
                "凭证号": move["凭证号"], "来源类型": move["来源类型"],
                "发生日期": move["发生日期"],
                "金额_最小单位": move["金额_最小单位"],
                "金额": format_amount(move["金额_最小单位"], move["币种"]),
            })

        # 预算条目也要出场（没有实际发生时实际为 0），并做科目+部门 -> 科目口径回退。
        for item in store.rows(T_BUDGET_ITEMS):
            key = (item["预算周期"], item["科目编号"], item["部门"], item["币种"])
            group = groups.setdefault(key, {
                "预算周期": item["预算周期"], "科目编号": item["科目编号"],
                "科目名称": item["科目名称"], "部门": item["部门"],
                "币种": item["币种"], "实际金额_最小单位": 0, "分摊来源": [],
            })
            group.setdefault("预算条目编号", item["预算条目编号"])

        execution: list[dict[str, Any]] = []
        for group in groups.values():
            currency = group["币种"]
            # 预算只按「周期 + 科目 + 部门 + 币种」精确口径关联；未单列预算的部门
            # 不套用其他部门的预算额，按无预算（0）处理并明确标注。
            budget_item = self._match_budget_item(group["预算周期"], group["科目编号"], group["部门"], currency)
            if budget_item is not None:
                budget_minor = budget_item["预算金额_最小单位"]
                caliber = "科目+部门"
            else:
                budget_minor, caliber = 0, "无预算"
            actual_minor = group["实际金额_最小单位"]
            variance_minor = budget_minor - actual_minor
            execution.append({
                "预算周期": group["预算周期"],
                "科目编号": group["科目编号"],
                "科目名称": group["科目名称"] or self._account_name(group["科目编号"]),
                "部门": group["部门"],
                "币种": currency,
                "预算口径": caliber,
                "预算金额_最小单位": budget_minor,
                "预算金额": float(to_decimal(budget_minor, currency)),
                "实际金额_最小单位": actual_minor,
                "实际金额": float(to_decimal(actual_minor, currency)),
                "差额_最小单位": variance_minor,
                "差额": float(to_decimal(variance_minor, currency)),
                "执行状态": "超预算" if actual_minor > budget_minor else ("持平" if actual_minor == budget_minor else "预算内"),
                "分摊来源": group["分摊来源"],
            })
        execution.sort(key=lambda row: (row["预算周期"], row["科目编号"], row["部门"], row["币种"]))
        return execution

    def _match_budget_item(
        self, period: str, account: str, department: str | None, currency: str,
    ) -> dict[str, Any] | None:
        for item in store.rows(T_BUDGET_ITEMS):
            if item["预算周期"] != period or item["科目编号"] != account or item["币种"] != currency:
                continue
            if department is None or item["部门"] == department:
                return item
        return None

    # ------------------------------------------------------------------ 依据与附件
    def list_bases(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in basis.items() if k != "快照"} for basis in store.rows(T_BASES)]

    def get_basis(self, basis_id: int) -> dict[str, Any] | None:
        return next((b for b in store.rows(T_BASES) if int(b.get("id", 0)) == basis_id), None)

    def latest_basis(self) -> dict[str, Any] | None:
        bases = store.rows(T_BASES)
        return bases[-1] if bases else None

    def list_execution(self, *, basis_id: int | None = None, account: str | None = None,
                       department: str | None = None) -> list[dict[str, Any]]:
        basis = self.get_basis(basis_id) if basis_id else self.latest_basis()
        if basis is None:
            return []
        rows = [r for r in store.rows(T_EXECUTION) if r.get("依据id") == basis["id"]]
        if account:
            rows = [r for r in rows if account in str(r.get("科目编号", ""))]
        if department:
            rows = [r for r in rows if r.get("部门") == department]
        return rows

    def build_attachment(self, basis_id: int | None = None) -> dict[str, Any]:
        """科目报告端取数：基于指定（默认最新）核算依据快照生成科目对账附件。"""
        basis = self.get_basis(basis_id) if basis_id else self.latest_basis()
        if basis is None:
            raise ValueError("尚无核算依据，请先执行预算核算")
        snapshot = basis["快照"]
        body: list[dict[str, Any]] = []
        for row in self.list_execution(basis_id=basis["id"]):
            currency = row["币种"]
            body.append({
                "预算周期": row["预算周期"], "科目编号": row["科目编号"],
                "科目名称": row["科目名称"], "部门": row["部门"], "币种": currency,
                "预算金额": format_amount(row["预算金额_最小单位"], currency),
                "实际金额": format_amount(row["实际金额_最小单位"], currency),
                "差额": format_amount(row["差额_最小单位"], currency),
                "预算口径": row["预算口径"], "执行状态": row["执行状态"],
                "分摊来源": row["分摊来源"],
            })
        closure: list[dict[str, Any]] = []
        for voucher in snapshot["凭证"]:
            currency = voucher["币种"]
            allocated = sum(a["分摊金额_最小单位"] for a in voucher["分摊项"])
            tail = sum(a["取整尾差_最小单位"] for a in voucher["分摊项"])
            done = sum(r["金额_最小单位"] for r in snapshot["冲销"]
                       if r.get("原凭证号") == voucher["凭证号"] and r["冲销状态"] == REVERSAL_DONE)
            pending = sum(r["金额_最小单位"] for r in snapshot["冲销"]
                          if r.get("原凭证号") == voucher["凭证号"] and r["冲销状态"] == REVERSAL_PENDING)
            closure.append({
                "凭证号": voucher["凭证号"], "发生日期": voucher["发生日期"],
                "原费用": format_amount(voucher["金额_最小单位"], currency),
                "分摊合计": format_amount(allocated, currency),
                "尾差调整_最小单位": tail,
                "已完成冲销": format_amount(done, currency),
                "未完成冲销": format_amount(pending, currency),
                "剩余金额": format_amount(voucher["金额_最小单位"] - done, currency),
                "是否闭合": allocated == voucher["金额_最小单位"]
                and voucher["金额_最小单位"] - done + done == voucher["金额_最小单位"],
            })
        attachment = {
            "id": _next_id(T_ATTACHMENTS),
            "附件编号": f"ATT-{basis['依据编号']}-{len(store.rows(T_ATTACHMENTS)) + 1:02d}",
            "核算依据编号": basis["依据编号"],
            "依据id": basis["id"],
            "标题": f"科目预算执行对账附件（{basis['依据编号']}）",
            "生成时间": _now(),
            "主体": body,
            "凭证闭合校验": closure,
            "异常清单": snapshot["异常"],
        }
        store.rows(T_ATTACHMENTS).append(attachment)
        return attachment

    # ------------------------------------------------------------------ 辅助方法
    def _find_voucher(self, voucher_no: str) -> dict[str, Any] | None:
        return next((v for v in store.rows(T_VOUCHERS)
                     if v.get("凭证号") == voucher_no and v["状态"] == VOUCHER_VALID), None)

    def _voucher_no_exists(self, voucher_no: str) -> bool:
        return any(v.get("凭证号") == voucher_no for v in store.rows(T_VOUCHERS))

    def _fingerprint_exists(self, table: str, fingerprint: str) -> bool:
        return any(row.get("指纹") == fingerprint for row in store.rows(table))

    def _batch_seen(self, batch_no: str) -> bool:
        return any(b.get("批次号") == batch_no for b in store.rows(T_BATCHES))

    def _batch_skip_result(self, batch_no: str, kind: str) -> dict[str, Any]:
        batch = next(b for b in store.rows(T_BATCHES) if b.get("批次号") == batch_no)
        return {
            "批次号": batch_no, "接收": 0, "重复跳过": 0, "异常": 0,
            "提示": f"{kind}批次 {batch_no} 已导入过，整批幂等跳过，未重复登记",
            "原批次": batch,
        }

    def _add_exception(self, kind: str, source: str, ref: str, reason: str, detail: Any) -> None:
        store.rows(T_EXCEPTIONS).append({
            "id": _next_id(T_EXCEPTIONS),
            "异常类型": kind, "来源环节": source, "单据号": ref,
            "原因": reason, "明细": detail if isinstance(detail, (str, int, float)) else json.loads(
                json.dumps(detail, ensure_ascii=False, default=str),
            ),
            "发现时间": _now(),
        })

    def _derived_exception(self, kind: str, source: str, ref: str, reason: str) -> dict[str, Any]:
        return {
            "id": _next_id(T_EXCEPTIONS),
            "异常类型": kind, "来源环节": source, "单据号": ref,
            "原因": reason, "明细": None, "发现时间": _now(),
            "核算派生": True,
        }

    def _in_any_budget_period(self, occur_date: str) -> bool:
        for item in store.rows(T_BUDGET_ITEMS):
            bounds = period_bounds(item["预算周期"])
            if bounds and bounds[0] <= occur_date <= bounds[1]:
                return True
        return False
