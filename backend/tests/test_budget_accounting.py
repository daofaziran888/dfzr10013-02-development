"""预算核算端到端校验：尾差闭合、部分/跨期/未完成冲销、重复导入、零预算、多币种、
版本快照、报告附件取数。运行：PYTHONPATH=. .venv/bin/python tests/test_budget_accounting.py
"""
from __future__ import annotations

from app.services.budget import (
    REVERSAL_DONE,
    REVERSAL_PENDING,
    T_ALLOCATIONS,
    T_BASES,
    T_BATCHES,
    T_EXECUTION,
    T_VOUCHERS,
    VOUCHER_VALID,
    BudgetService,
)
from app.services.budget_money import allocate_by_ratio, format_amount, to_minor
from app.store import store

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reset() -> BudgetService:
    store.reset_budget_tables()
    return BudgetService()


def test_money_and_allocation() -> None:
    print("[1] 最小货币单位与最大余数法分摊")
    check("CNY 两位小数转最小单位", to_minor("100.01", "CNY") == 10001)
    check("JPY 零位小数取整", to_minor("1500.5", "JPY") == 1501)
    check("非法金额返回 None", to_minor("abc", "CNY") is None and to_minor(-1, "CNY") is None)
    parts = allocate_by_ratio(10001, [__import__("decimal").Decimal("0.34"),
                                      __import__("decimal").Decimal("0.33"),
                                      __import__("decimal").Decimal("0.33")])
    check("三份尾差拆分闭合", parts == [3401, 3300, 3300], str(parts))
    check("分摊合计等于原费用", sum(parts) == 10001)


def test_seed_and_dedup() -> None:
    print("[2] 种子数据与重复导入幂等")
    service = reset()
    vouchers = store.rows(T_VOUCHERS)
    check("无效凭证未入账", all(v["状态"] == VOUCHER_VALID for v in vouchers))
    check("同号重复凭证只记一次", len([v for v in vouchers if v["凭证号"] == "JV-202610-001"]) == 1)
    # 再次整批重复导入：批次幂等，接收为 0。
    from app.budget_seed import EXPENSE_LINES
    result = service.import_expenses({"批次号": "SEED-EXPENSE-001", "费用明细": EXPENSE_LINES})
    check("重复批次整批跳过", result["接收"] == 0 and "幂等" in result["提示"], str(result))
    # 同批次号下换新内容也不允许重放。
    result2 = service.import_expenses({"批次号": "SEED-EXPENSE-001", "费用明细": []})
    check("批次号防重放", result2["接收"] == 0)
    check("凭证表行数未增长", len(store.rows(T_VOUCHERS)) == 5)


def test_voucher_closure() -> None:
    print("[3] 逐笔费用对账：尾差、冲销与剩余金额闭合")
    service = reset()
    voucher = next(v for v in store.rows(T_VOUCHERS) if v["凭证号"] == "JV-202610-001")
    rec = service.voucher_reconciliation(voucher["id"])
    check("分摊明细三项", len(rec["分摊明细"]) == 3)
    check("分摊等于原费用 100.01", rec["闭合校验"]["分摊等于原费用"])
    check("冲销拆分闭合到 30.00",
          sum(item["冲销金额_最小单位"] for item in rec["冲销记录"][0]["冲销分摊"]) == 3000)
    check("剩余金额 = 原费用 - 已完成冲销", rec["剩余金额_最小单位"] == 10001 - 3000)
    check("整体闭合", rec["闭合校验"]["是否闭合"])

    v2 = next(v for v in store.rows(T_VOUCHERS) if v["凭证号"] == "JV-202610-002")
    rec2 = service.voucher_reconciliation(v2["id"])
    pending = [r for r in rec2["冲销记录"] if r["冲销状态"] == REVERSAL_PENDING]
    check("未完成冲销单列", len(pending) == 1 and pending[0]["是否计入实际"] is False)
    check("未完成冲销不抵减剩余", rec2["剩余金额_最小单位"] == 100000 - 20000)

    v3 = next(v for v in store.rows(T_VOUCHERS) if v["凭证号"] == "JV-202611-003")
    rec3 = service.voucher_reconciliation(v3["id"])
    cross = rec3["冲销记录"][0]
    check("跨期冲销标记为跨期", cross["是否跨期"] is True and cross["是否计入实际"] is False)
    check("跨期冲销不抵减原周期剩余", rec3["剩余金额_最小单位"] == 80000)


def test_execution_dataset() -> None:
    print("[4] 预算执行差异数据集")
    service = reset()
    rows = service.list_execution()
    # 7 个费用/预算口径组 + 1 个有预算无发生的咨询费组，共 8 行。
    check("数据集行数", len(rows) == 8, str(len(rows)))
    # 市场部 FINA-1004 Q4：JV-002 分摊 600 + JV-003 全额 800 - 同周期冲销 120
    # （RV-002 的 200 按 60% 拆分）- 跨期冲销 200 归属原周期抵减 = 1080；预算 12000。
    market = next(r for r in rows if r["科目编号"] == "FINA-1004" and r["部门"] == "市场部"
                  and r["预算周期"] == "2026-Q4" and r["币种"] == "CNY")
    check("冲销后实际 1080.00", market["实际金额_最小单位"] == 108000, str(market["实际金额"]))
    check("差额 = 预算 - 实际", market["差额_最小单位"] == 1200000 - 108000)
    check("执行状态预算内", market["执行状态"] == "预算内")
    # 销售部承担的 400 - 80 冲销，来源应同时含费用与冲销两条。
    sales = next(r for r in rows if r["科目编号"] == "FINA-1004" and r["部门"] == "销售部")
    kinds = {s["来源类型"] for s in sales["分摊来源"]}
    check("分摊来源含费用与冲销", kinds == {"费用分摊", "冲销抵减"}, str(kinds))
    check("销售部实际 320.00", sales["实际金额_最小单位"] == 32000)
    # 零预算科目：预算金额为 0 且有实际发生，判定超预算、口径为科目+部门。
    zero = next(r for r in rows if r["科目编号"] == "FINA-1003")
    check("零预算判定超预算", zero["预算金额_最小单位"] == 0 and zero["执行状态"] == "超预算"
          and zero["预算口径"] == "科目+部门")
    # 未单列预算的部门不套用其他部门预算，按无预算处理。
    no_budget = next(r for r in rows if r["科目编号"] == "FINA-1004" and r["部门"] == "销售部")
    check("未单列预算按无预算处理", no_budget["预算口径"] == "无预算"
          and no_budget["预算金额_最小单位"] == 0 and no_budget["执行状态"] == "超预算")
    # 有预算无发生的科目也要出场，实际为 0、状态预算内。
    quiet = next(r for r in rows if r["科目编号"] == "FINA-1005")
    check("有预算无发生照常展示", quiet["实际金额_最小单位"] == 0 and quiet["执行状态"] == "预算内")
    # 多币种隔离。
    usd = [r for r in rows if r["币种"] == "USD"]
    check("USD 独立成组不与 CNY 相加", len(usd) == 1 and usd[0]["实际金额_最小单位"] == 150050)
    check("USD 金额按 2 位展示", format_amount(usd[0]["实际金额_最小单位"], "USD") == "1500.50 USD")


def test_custom_reversal_split() -> None:
    print("[5] 自定义冲销项比例拆分与异常拦截")
    service = reset()
    # 对双分摊项凭证 JV-202610-002 用自定义 50%/50% 冲销 100 元，应拆成 50/50 且闭合。
    result = service.import_reversals({"批次号": "BATCH-CUSTOM-001", "冲销记录": [
        {"冲销号": "RV-CUSTOM-001", "原凭证号": "JV-202610-002", "发生日期": "2026-11-08",
         "币种": "CNY", "金额": 100, "冲销状态": "已完成",
         "冲销项": [{"分摊比例": "0.5"}, {"分摊比例": "0.5"}]},
    ]})
    check("自定义冲销导入成功", result["接收"] == 1 and result["异常"] == 0, str(result))
    v2 = next(v for v in store.rows(T_VOUCHERS) if v["凭证号"] == "JV-202610-002")
    rec = service.voucher_reconciliation(v2["id"])
    custom = next(r for r in rec["冲销记录"] if r["冲销号"] == "RV-CUSTOM-001")
    pieces = [p["冲销金额_最小单位"] for p in custom["冲销分摊"]]
    check("自定义比例 50/50 拆分", pieces == [5000, 5000], str(pieces))
    # 比例项数量与分摊项不符：拒绝并入异常。
    bad = service.import_reversals({"批次号": "BATCH-CUSTOM-002", "冲销记录": [
        {"冲销号": "RV-CUSTOM-002", "原凭证号": "JV-202610-002", "发生日期": "2026-11-09",
         "币种": "CNY", "金额": 10, "冲销状态": "已完成",
         "冲销项": [{"分摊比例": "1.0"}]},
    ]})
    check("冲销项数量不符被拦截", bad["异常"] == 1 and bad["接收"] == 0)
    check("异常类型含比例合计不符",
          any(e["异常类型"] == "比例合计不符" and e["单据号"] == "RV-CUSTOM-002"
              for e in service.list_exceptions()))


def test_exceptions() -> None:
    print("[6] 异常行单列原因")
    service = reset()
    kinds = {e["异常类型"] for e in service.list_exceptions()}
    for expected in ["金额无效", "比例合计不符", "缺少原凭证"]:
        check(f"异常类型 {expected}", expected in kinds, str(kinds))


def test_versioning_and_recovery() -> None:
    print("[7] 核算恢复、更正与版本快照")
    service = reset()
    before = len(store.rows(T_BASES))
    rec = service.recover_accounting()
    check("数据未变恢复时复用依据", "复用依据" in rec and len(store.rows(T_BASES)) == before)
    # 数据变化后恢复：生成新版本，且不重复分摊（执行数据按新快照重建）。
    service.import_expenses({"批次号": "BATCH-NEW-001", "费用明细": [
        {"凭证号": "JV-202612-008", "科目编号": "FINA-1002", "部门": "管理部",
         "发生日期": "2026-12-20", "币种": "CNY", "金额": 100},
    ]})
    rec2 = service.recover_accounting()
    check("数据变化后恢复生成新依据", "复用依据" not in rec2 and len(store.rows(T_BASES)) == before + 1)
    old_basis = service.list_bases()[0]
    old_rows = service.list_execution(basis_id=old_basis["id"])
    check("旧依据快照仍可取数", len(old_rows) == 8)
    new_rows = service.list_execution()
    check("新依据含新增费用", any(any(s["凭证号"] == "JV-202612-008" for s in r["分摊来源"]) for r in new_rows))
    # 更正再生成一版。
    basis3 = service.correct_accounting()
    check("更正产生新版本", basis3["版本"] == old_basis["版本"] + 2)
    check("旧依据数量保持不变", len(service.list_bases()) == before + 2)


def test_attachment_and_report() -> None:
    print("[8] 科目对账附件与跨模块报告绑定")
    service = reset()
    attachment = service.build_attachment()
    check("附件主体按科目部门展开", len(attachment["主体"]) == 8)
    check("附件含凭证闭合校验", len(attachment["凭证闭合校验"]) == 5)
    check("全部凭证闭合", all(item["是否闭合"] for item in attachment["凭证闭合校验"]))
    check("附件含异常清单", len(attachment["异常清单"]) >= 5)
    # 报告端跨模块绑定，旧报告固定旧依据。
    from app.services.report import ReportService
    report_service = ReportService()
    report, missing = report_service.create_entry({"报告编号": "RPT-BUDGET-01", "报告主题": "科目预算执行对账",
                                                   "分析周期": "2026-Q4"})
    check("报告登记成功", report is not None, str(missing))
    first_basis_id = service.latest_basis()["id"]
    att, msg = report_service.build_budget_attachment(report["id"], basis_id=first_basis_id)
    check("报告绑定对账附件", att is not None, msg)
    service.correct_accounting()
    bound = report_service.get_entry(report["id"])["预算对账附件"][0]
    check("更正后旧报告仍绑定旧依据", bound["依据id"] == first_basis_id)
    historical = service.list_execution(basis_id=first_basis_id)
    check("旧依据数据集行数不变", len(historical) == 8)


def main() -> None:
    test_money_and_allocation()
    test_seed_and_dedup()
    test_voucher_closure()
    test_execution_dataset()
    test_custom_reversal_split()
    test_exceptions()
    test_versioning_and_recovery()
    test_attachment_and_report()
    print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
