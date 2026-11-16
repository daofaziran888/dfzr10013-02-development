"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 预算核算表先建空仓，种子在应用启动时经服务层流程加载（见 load_budget_seed），
        # 避免在模块初始化阶段与服务层形成循环导入。
        from app.services.budget_constants import ALL_TABLES

        for name in ALL_TABLES:
            self._tables.setdefault(name, [])
        self._budget_seed_loaded = False

    def load_budget_seed(self) -> None:
        """启动阶段构建预算核算种子数据；重复调用不会重复分摊或重复入账。"""
        if self._budget_seed_loaded:
            return
        from app.budget_seed import build_budget_seed
        from app.services.budget import BudgetService
        from app.services.budget_constants import ALL_TABLES

        seed_tables = build_budget_seed(BudgetService())
        for name in ALL_TABLES:
            self._tables[name] = seed_tables[name]
        self._budget_seed_loaded = True

    def reset_budget_tables(self) -> None:
        """清空预算核算表并重新构建种子，供核算恢复与测试回到确定的初始状态。"""
        from app.budget_seed import build_budget_seed
        from app.services.budget import BudgetService
        from app.services.budget_constants import ALL_TABLES

        for name in ALL_TABLES:
            self._tables[name] = []
        seed_tables = build_budget_seed(BudgetService())
        for name in ALL_TABLES:
            self._tables[name] = seed_tables[name]
        self._budget_seed_loaded = True

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
