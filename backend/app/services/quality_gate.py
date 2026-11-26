"""装配质量闸领域服务：阈值校验与阻断放行决策，校验命中率导入导出规则，产出质量闸条目数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QualityGateService(BaseModuleService):
    MODULE = "quality_gate"
    ENTITY = "质量闸条目"
    LIST_FIELDS = ["闸口编号", "所属事实表", "校验项", "阈值声明", "实测读数", "通过判定", "阻断范围", "最近执行"]
    REQUIRED_FIELDS = ["闸口编号", "所属事实表", "校验项"]
    STATUS_ORDER = ["待执行", "执行中", "已放行", "已阻断"]
    ACTION_RULES = {"执行校验": "执行中", "放行批次": "已放行", "阻断批次": "已阻断"}
    NEGATIVE_ACTIONS = []

    def evaluate_gate(
        self, gate_id: str, fact_table: str, check_item: str,
        threshold: float, measured: float, block_scope: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """执行质量闸校验：实测读数越界时按阻断范围拦下批次。"""
        passed = measured >= threshold
        status = "已放行" if passed else "已阻断"

        entry = {
            "闸口编号": gate_id, "所属事实表": fact_table,
            "校验项": check_item, "阈值声明": str(threshold),
            "实测读数": str(measured), "通过判定": "是" if passed else "否",
            "阻断范围": block_scope if not passed else "",
            "最近执行": "2026-11-21",
            "status": status, "pending": False,
            "abnormal": not passed,
        }
        rows = store.rows(self.MODULE)
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)

        if passed:
            return True, f"质量闸 {gate_id} 放行：{check_item}={measured} >= 阈值{threshold}", entry
        return False, (
            f"质量闸 {gate_id} 阻断：{check_item}={measured} < 阈值{threshold}，阻断范围 {block_scope}"
        ), entry

    def check_fact_readiness(self, fact_table: str) -> tuple[bool, str]:
        """检查事实表是否有阻断中的质量闸。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("所属事实表") == fact_table and r.get("status") == "已阻断":
                return False, f"事实表 {fact_table} 存在阻断的质量闸"
        return True, f"事实表 {fact_table} 质量闸全部放行"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """放行不能在这里直接改状态，必须走放行确认接口留下有效判定与回执。

        ``阻断批次`` 仍可手工留痕，但 ``放行批次`` 只接受放行交接状态机已经
        确认放行的条目，避免出现「状态已放行、装载却拿不到回执」的断链。
        """
        if action != "放行批次":
            return super().run_action(entry_id, action)

        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        from app.services.release_control import release_control

        gate_id = str(entry.get("闸口编号", ""))
        released = release_control.released_application_for_gate(gate_id)
        if released is None:
            return None, (
                f"质量闸 {gate_id} 还没有实际放行的确认结论；"
                "请先在放行交接看板确认放行，装载窗口才能凭回执推进"
            )
        entry["status"] = "已放行"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["判定编号"] = released["judgement"]["judgement_id"]
        entry["阈值版本"] = f"v{released['judgement']['threshold_version']}"
        entry["放行回执"] = released["receipt"]["receipt_id"]
        return entry, (
            f"质量闸 {gate_id} 沿用放行确认判定 {released['judgement']['judgement_id']}，"
            f"回执 {released['receipt']['receipt_id']}"
        )