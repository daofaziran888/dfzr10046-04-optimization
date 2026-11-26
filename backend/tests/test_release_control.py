"""放行确认与装载窗口交接：跨模块一致性、换班语义与并发幂等测试。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.services.release_control import ReleaseControlService, release_control
from app.store import store


@pytest.fixture()
def control() -> ReleaseControlService:
    service = ReleaseControlService()
    service.reset()
    return service


def _seed_apps(control: ReleaseControlService) -> tuple[str, str]:
    snapshot = control.control_snapshot()
    ids = [item["app_id"] for item in snapshot["applications"]]
    assert len(ids) >= 2
    return ids[0], ids[1]


class TestAcceptanceFlow:
    def test_cap_is_two_with_two_pending(self, control):
        snapshot = control.control_snapshot()
        assert snapshot["current_version"]["cap"] == 2
        assert snapshot["pending_count"] == 2
        assert snapshot["capacity"]["used"] == 0

    def test_colleague_lowers_threshold_and_reevaluates(self, control):
        app_low, app_high = _seed_apps(control)
        ok, message, detail = control.adjust_version(
            threshold=0.90, updated_by="同事", reason="命中率收紧")
        assert ok
        previews = {item["application_id"]: item for item in detail["previews"]}
        # 0.88 的申请在新阈值下被重新判定为拒绝，0.95 的仍放行
        assert previews[app_low]["outcome"] == "rejected"
        assert previews[app_low]["reason_code"] == "threshold_not_met"
        assert previews[app_high]["outcome"] == "released"
        assert previews[app_high]["capacity"]["after_confirm"] <= 2

    def test_confirm_returns_actual_reason_with_version(self, control):
        app_low, _ = _seed_apps(control)
        control.adjust_version(threshold=0.90, updated_by="同事")
        ok, message, detail = control.confirm_release(app_low, client_request_id="r1")
        assert not ok
        judgement = detail["judgement"]
        assert judgement["outcome"] == "rejected"
        assert judgement["threshold_version"] == 2
        assert "阈值版本" in message and "0.9" in message

    def test_confirmed_release_writes_todo_once(self, control):
        app_high = _seed_apps(control)[1]
        ok, _, detail = control.confirm_release(app_high, client_request_id="r1")
        assert ok
        todos = [row for row in store.rows("load_window") if row.get("放行申请") == app_high]
        assert len(todos) == 1
        assert todos[0]["放行回执"] == detail["receipt"]["receipt_id"]
        assert todos[0]["判定编号"] == detail["judgement"]["judgement_id"]

    def test_rejected_application_writes_no_todo(self, control):
        app_low, _ = _seed_apps(control)
        control.adjust_version(threshold=0.99, updated_by="同事")
        ok, _, detail = control.confirm_release(app_low, client_request_id="r1")
        assert not ok
        assert detail["receipt"] is None
        todos = [row for row in store.rows("load_window") if row.get("放行申请") == app_low]
        assert todos == []


class TestWindowRules:
    def test_at_cap_releases(self, control):
        first, second = _seed_apps(control)
        ok1, _, d1 = control.confirm_release(first, client_request_id="r1")
        ok2, _, d2 = control.confirm_release(second, client_request_id="r2")
        assert ok1 and ok2
        assert d2["judgement"]["capacity"]["after_confirm"] == 2
        assert d2["judgement"]["reason_code"] == "released_at_cap"
        assert "等于生效上限" in d2["judgement"]["reason"]

    def test_over_cap_blocks_third(self, control):
        apps = _seed_apps(control)
        control.confirm_release(apps[0], client_request_id="r1")
        control.confirm_release(apps[1], client_request_id="r2")
        third = control.submit_application(
            gate_id="QG-300", fact_table="销售事实表", check_item="外键命中率",
            measured=0.99, window_id="LW-QG-300", window_start="2026-10-09",
            window_end="2026-10-09", client_request_id="r3",
        )
        _, _, detail = control.confirm_release(third[2]["application"]["app_id"],
                                              client_request_id="r3-confirm")
        assert detail["judgement"]["reason_code"] == "capacity_exhausted"
        assert detail["receipt"] is None

    def test_closed_window_rejects_new_release(self, control):
        ok, _, _ = control.set_window(
            fact_table="销售事实表", state="closed", by="值班员", reason="换班关闭")
        assert ok
        accepted, message, detail = control.submit_application(
            gate_id="QG-400", fact_table="销售事实表", check_item="外键命中率",
            measured=0.99, window_id="LW-QG-400", window_start="2026-10-10",
            window_end="2026-10-10", client_request_id="closed-1",
        )
        assert not accepted
        assert detail["judgement"]["reason_code"] == "window_closed"
        assert "已关闭" in message

    def test_confirmation_after_window_close_rejects_pending(self, control):
        app_high = _seed_apps(control)[1]
        control.set_window(fact_table="销售事实表", state="closed", reason="换班关闭")
        ok, message, detail = control.confirm_release(app_high, client_request_id="r1")
        assert not ok
        assert detail["judgement"]["reason_code"] == "window_closed"
        assert "优先于" in message

    def test_issued_receipt_survives_window_close(self, control):
        app_high = _seed_apps(control)[1]
        ok, _, detail = control.confirm_release(app_high, client_request_id="r1")
        assert ok
        control.set_window(fact_table="销售事实表", state="closed", reason="换班关闭")
        ok, message, row, extra = control.confirm_load(
            detail["todo"]["id"], receipt_id=detail["receipt"]["receipt_id"])
        assert ok
        assert row["status"] == "已装载"

    def test_window_close_beats_cap_change(self, control):
        now = datetime.now(timezone.utc)
        at = (now + timedelta(seconds=5)).isoformat()
        control.adjust_version(cap=1, effective_at=at, updated_by="同事C")
        control.set_window(
            fact_table="销售事实表", state="closed", effective_at=at, reason="换班关闭")
        projection = control.project_handover()
        for decision in projection["handover_decisions"]:
            assert decision["reason_code"] == "window_closed"
            assert decision["can_handover"] is False


class TestHandoverSemantics:
    def test_recompute_policy_uses_current_threshold(self, control):
        app_low, _ = _seed_apps(control)
        assert control.get_application(app_low)["application"]["received_policy"] == "recompute"
        control.adjust_version(threshold=0.90, updated_by="同事")
        judgement = control.preview_application(app_low)
        assert judgement["threshold_version"] == 2
        assert judgement["handover"]["in_shift_change"] is True

    def test_reserve_policy_keeps_acceptance_threshold(self, control):
        control.adjust_version(handover_policy="reserve", updated_by="同事")
        ok, _, detail = control.submit_application(
            gate_id="QG-500", fact_table="销售事实表", check_item="外键命中率",
            measured=0.86, window_id="LW-QG-500", window_start="2026-10-11",
            window_end="2026-10-11", client_request_id="reserve-1",
        )
        assert ok
        assert detail["application"]["seat_reserved"] is True
        # 换班后阈值抬高到 0.95，但占位申请仍按受理阈值 0.85 交接
        control.adjust_version(threshold=0.95, cap=1, updated_by="同事")
        ok, message, confirmed = control.confirm_release(
            detail["application"]["app_id"], client_request_id="reserve-1-c")
        assert ok
        assert confirmed["judgement"]["threshold_version"] == 2
        assert confirmed["judgement"]["handover"]["seat_reserved"] is True
        assert confirmed["judgement"]["handover"]["can_handover"] is True

    def test_lowered_cap_after_reserve_still_handover(self, control):
        control.adjust_version(handover_policy="reserve", updated_by="同事")
        ids = []
        for index in range(2):
            _, _, detail = control.submit_application(
                gate_id=f"QG-6{index:02d}", fact_table="销售事实表",
                check_item="外键命中率", measured=0.90,
                window_id=f"LW-QG-6{index}", window_start="2026-10-12",
                window_end="2026-10-12", client_request_id=f"reserve-{index}",
            )
            ids.append(detail["application"]["app_id"])
        control.adjust_version(cap=0, updated_by="同事", reason="名额压缩")
        ok, message, detail = control.confirm_release(ids[0], client_request_id="res-0-c")
        assert ok, message
        assert detail["judgement"]["handover"]["can_handover"] is True

    def test_recompute_cannot_handover_when_capacity_gone(self, control):
        first, _ = _seed_apps(control)
        control.confirm_release(first, client_request_id="r1")
        control.adjust_version(cap=1, updated_by="同事")
        second = _seed_apps(control)[1]
        ok, message, detail = control.confirm_release(second, client_request_id="r2")
        assert not ok
        assert detail["judgement"]["reason_code"] == "capacity_exhausted"
        assert detail["judgement"]["handover"]["can_handover"] is False
        assert "不能交班" in detail["judgement"]["handover"]["reason"]

    def test_projection_reports_effective_at(self, control):
        now = datetime.now(timezone.utc)
        at = (now + timedelta(minutes=3)).replace(microsecond=0).isoformat()
        control.adjust_version(threshold=0.92, effective_at=at, updated_by="同事")
        projection = control.project_handover()
        assert projection["changes_at"] == at
        assert projection["message"].startswith("下一次变更将于")
        assert len(projection["handover_decisions"]) == 2
        for decision in projection["handover_decisions"]:
            assert decision["accepted_version"] == "v1"
            assert "handover_reason" in decision


class TestSharedJudgement:
    def test_release_and_load_share_judgement_id(self, control):
        app_high = _seed_apps(control)[1]
        _, _, confirmed = control.confirm_release(app_high, client_request_id="r1")
        release_id = confirmed["judgement"]["judgement_id"]
        ok, message, row, extra = control.confirm_load(
            confirmed["todo"]["id"], receipt_id=confirmed["receipt"]["receipt_id"])
        assert ok
        assert extra["judgement_id"] == release_id
        assert extra["judgement_consistent"] is True
        assert row["判定编号"] == release_id

    def test_loader_blocked_without_receipt(self, control):
        row = next(r for r in store.rows("load_window") if r.get("status") == "待装载")
        ok, message, board, extra = control.confirm_load(row["id"])
        assert not ok
        assert "有效放行回执" in message

    def test_loader_blocked_for_pending_application(self, control):
        app_low = _seed_apps(control)[0]
        # 待确认申请没有回执，因此不存在可推进的装载待办
        assert control.get_application(app_low)["application"]["state"] == "待确认"
        todos = [r for r in store.rows("load_window") if r.get("放行申请") == app_low]
        assert todos == []
        pending_preview = control.preview_application(app_low)
        assert pending_preview["outcome"] in {"released", "rejected"}
        assert pending_preview["preview"] is True

    def test_loader_blocked_for_rejected_application(self, control):
        app_low, _ = _seed_apps(control)
        control.adjust_version(threshold=0.99, updated_by="同事")
        ok, _, detail = control.confirm_release(app_low, client_request_id="r1")
        assert not ok
        assert detail["todo"] is None

    def test_receipt_cannot_be_consumed_twice(self, control):
        app_high = _seed_apps(control)[1]
        _, _, confirmed = control.confirm_release(app_high, client_request_id="r1")
        control.confirm_load(confirmed["todo"]["id"],
                             receipt_id=confirmed["receipt"]["receipt_id"])
        ok, message, _, _ = control.confirm_load(
            confirmed["todo"]["id"], receipt_id=confirmed["receipt"]["receipt_id"])
        assert not ok
        assert "同一回执不能重复推进装载" in message or "当前状态" in message

    def test_unknown_receipt_blocked(self, control):
        app_high = _seed_apps(control)[1]
        _, _, confirmed = control.confirm_release(app_high, client_request_id="r1")
        ok, message, _, _ = control.confirm_load(
            confirmed["todo"]["id"], receipt_id="RCPT-FORGED")
        assert not ok
        assert "不存在" in message

    def test_receipt_cannot_drive_another_window(self, control):
        apps = _seed_apps(control)
        ids = []
        for index, app_id in enumerate(apps):
            ok, _, detail = control.confirm_release(app_id, client_request_id=f"m{index}")
            assert ok
            ids.append(detail)
        first, second = ids
        ok, message, _, _ = control.confirm_load(
            second["todo"]["id"], receipt_id=first["receipt"]["receipt_id"])
        assert not ok
        assert "不匹配" in message
        # 正确回执仍可推进
        ok, _, _, _ = control.confirm_load(
            second["todo"]["id"], receipt_id=second["receipt"]["receipt_id"])
        assert ok

    def test_gate_row_reflects_confirmation(self, control):
        app_low, app_high = _seed_apps(control)
        control.confirm_release(app_high, client_request_id="g1")
        row = next(r for r in store.rows("quality_gate") if r.get("闸口编号") == "QG-102")
        assert row["status"] == "已放行"
        assert row["判定编号"].startswith("JDG")

        control.adjust_version(threshold=0.99, updated_by="同事")
        control.confirm_release(app_low, client_request_id="g2")
        row = next(r for r in store.rows("quality_gate") if r.get("闸口编号") == "QG-101")
        assert row["status"] == "已阻断"
        assert "阻断原因" in row
        assert row["阈值版本"] == "v2"


class TestIdempotencyAndConcurrency:
    def test_duplicate_confirm_returns_same_receipt(self, control):
        app_high = _seed_apps(control)[1]
        _, _, first = control.confirm_release(app_high, client_request_id="r1")
        ok, message, second = control.confirm_release(app_high, client_request_id="r1-retry")
        assert ok
        assert second["receipt"]["receipt_id"] == first["receipt"]["receipt_id"]
        assert len([r for r in store.rows("load_window") if r.get("放行申请") == app_high]) == 1
        assert len(control.control_snapshot()["receipts"]) == 1

    def test_duplicate_submit_reuses_application(self, control):
        ok, _, first = control.submit_application(
            gate_id="QG-700", fact_table="销售事实表", check_item="外键命中率",
            measured=0.95, window_id="LW-QG-700", window_start="2026-10-13",
            window_end="2026-10-13", client_request_id="dup-1",
        )
        app_id = first["application"]["app_id"]
        ok, message, second = control.submit_application(
            gate_id="QG-700", fact_table="销售事实表", check_item="外键命中率",
            measured=0.95, window_id="LW-QG-700", window_start="2026-10-13",
            window_end="2026-10-13", client_request_id="dup-1",
        )
        assert second["duplicate_submission"] is True
        assert second["application"]["app_id"] == app_id

    def test_concurrent_confirms_yield_single_receipt(self, control):
        app_high = _seed_apps(control)[1]
        results: list[tuple[bool, str, dict]] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            outcome = control.confirm_release(app_high, client_request_id=f"race-{index}")
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        receipts = [entry["receipt"] for ok, _, entry in results if ok and entry["receipt"]]
        assert len({receipt["receipt_id"] for receipt in receipts}) == 1
        todo_rows = [r for r in store.rows("load_window") if r.get("放行申请") == app_high]
        assert len(todo_rows) == 1
        assert len(control.control_snapshot()["receipts"]) == 1

    def test_concurrent_confirms_respect_cap(self, control):
        app_a, app_b = _seed_apps(control)
        control.adjust_version(cap=1, updated_by="同事")
        outcomes: list[tuple[bool, str, dict]] = []
        lock = threading.Lock()

        def worker(app_id: str) -> None:
            outcome = control.confirm_release(app_id, client_request_id=f"cap-{app_id}")
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker, args=(app_id,))
                   for app_id in (app_a, app_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        released = [entry for ok, _, entry in outcomes
                    if ok and entry["judgement"]["outcome"] == "released"]
        assert len(released) == 1
        assert len(control.control_snapshot()["receipts"]) == 1

    def test_ambiguous_response_can_be_reconciled(self, control):
        app_high = _seed_apps(control)[1]
        control.confirm_release(app_high, client_request_id="lost-response")
        # 模拟回包丢失后重新打开质量闸核对实际结论
        reconciled = control.get_application(app_high)
        assert reconciled is not None
        assert reconciled["actual_conclusion"]["outcome"] == "released"
        receipt_id = reconciled["actual_conclusion"]["receipt_id"]
        assert receipt_id
        # 网络延迟导致的重复提交不会再多出一份可装载的回执
        ok, _, duplicate = control.confirm_release(app_high, client_request_id="retry")
        assert duplicate["receipt"]["receipt_id"] == receipt_id
        loadable = [r for r in store.rows("load_window")
                    if r.get("放行回执") == receipt_id and r.get("status") == "待装载"]
        assert len(loadable) == 1


class TestResourceActionIntegration:
    def test_quality_gate_release_requires_confirmation(self):
        from app.services.quality_gate import QualityGateService

        release_control.reset()
        service = QualityGateService()
        row = next(r for r in store.rows("quality_gate") if r.get("闸口编号") == "QG-101")
        entry, message = service.run_action(row["id"], "放行批次")
        assert entry is None
        assert "放行交接" in message

        control = release_control
        _, _, confirmed = control.confirm_release("APP-20261126-001", client_request_id="qg-1")
        assert confirmed["receipt"] is not None
        entry, message = service.run_action(row["id"], "放行批次")
        assert entry is not None
        assert entry["放行回执"] == confirmed["receipt"]["receipt_id"]
        assert confirmed["judgement"]["judgement_id"] in message

    def test_load_window_action_requires_receipt(self):
        from app.services.load_window import LoadWindowService

        release_control.reset()
        service = LoadWindowService()
        row = next(r for r in store.rows("load_window") if r.get("status") == "待装载")
        entry, message = service.run_action(row["id"], "确认装载")
        assert entry is None
        assert "有效放行回执" in message

    def test_load_window_action_consumes_receipt(self):
        from app.services.load_window import LoadWindowService

        release_control.reset()
        app_high = [item["app_id"] for item in release_control.control_snapshot()["applications"]][1]
        _, _, confirmed = release_control.confirm_release(
            app_high, client_request_id="resource-1")
        service = LoadWindowService()
        entry, message = service.run_action(confirmed["todo"]["id"], "启动装载")
        assert entry is not None
        assert entry["status"] == "装载中"
        # 启动后同一窗口再次启动被挡
        blocked, blocked_message = service.run_action(confirmed["todo"]["id"], "启动装载")
        assert blocked is None
        # 确认装载才核销回执并推进水位
        entry, message = service.run_action(confirmed["todo"]["id"], "确认装载")
        assert entry is not None
        assert entry["status"] == "已装载"
        assert "共用判定" in message
        receipt = next(r for r in release_control.control_snapshot()["receipts"]
                       if r["receipt_id"] == confirmed["receipt"]["receipt_id"])
        assert receipt["state"] == "consumed"
