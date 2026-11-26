"""质量闸放行确认与装载窗口交接的核心状态机。

这个模块把「装配质量闸放行」做成一条可核对的跨模块交接链：

1. 质量闸受理一笔放行申请（application），记录受理时的阈值版本与名额语义；
2. 只有确认接口真正判定为放行时才签发回执（receipt），并写入装载待办（todo）；
3. 装载员必须凭有效回执确认装载，被拒绝或尚待结论的申请显式挡住该动作；
4. 放行确认与装载确认共用同一份 ``judgement``，两处读到的判定编号必须一致；
5. 重复请求按请求号幂等，同一申请最多一份有效回执，重复回执不会进入装载。

换班语义由申请受理时的 ``handover_policy`` 决定：

- ``reserve``（受理占位）：受理时即按当时阈值/上限判定并占住当班名额。
  换班后仍按受理版本的阈值与已占名额交接，即使新上限调低、容量用完，
  这笔仍可交班。
- ``recompute``（放行重算）：受理只入队、不占名额。实际放行前必须按当时
  生效的阈值版本与当班名额重算；容量用完则不可交班。

窗口状态、换班策略、阈值和名额在同一份判定里消费，优先级固定为：

    window_closed > handover_policy > threshold > capacity

窗口关闭与上限调整在同一个生效时刻相遇时，关闭优先。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.seed import SEED_ROWS
from app.store import store

UTC = timezone.utc

HANDOVER_POLICIES = ("reserve", "recompute")
HANDOVER_POLICY_LABELS = {
    "reserve": "受理占位：受理时占住当班名额，换班后按受理阈值交接",
    "recompute": "放行重算：受理只入队，实际放行前按现行阈值与名额重算",
}
EVALUATION_PRIORITY = ["window_closed", "handover_policy", "threshold", "capacity"]
APPLICATION_STATES = ("待确认", "已放行", "已拒绝")
WINDOW_STATES = ("open", "closed")


def _parse_dt(value: Any, fallback: datetime | None = None) -> datetime:
    """把 ISO 字符串或时间对象统一成带时区的 UTC 时间。"""
    if value in (None, ""):
        return fallback or datetime.now(UTC)
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


@dataclass
class ThresholdVersion:
    """一版换班策略：阈值、当班上限与在途提交的处理语义。"""

    version: int
    threshold: float
    cap: int
    handover_policy: str
    shift_id: str
    effective_at: datetime
    updated_by: str
    reason: str
    created_at: datetime
    # 生效标记在查询时按 effective_at 动态计算，不写死。

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(UTC)
        return {
            "version": self.version,
            "version_label": f"v{self.version}",
            "threshold": self.threshold,
            "cap": self.cap,
            "handover_policy": self.handover_policy,
            "handover_policy_label": HANDOVER_POLICY_LABELS.get(
                self.handover_policy, self.handover_policy),
            "shift_id": self.shift_id,
            "effective_at": _iso(self.effective_at),
            "created_at": _iso(self.created_at),
            "updated_by": self.updated_by,
            "reason": self.reason,
            "active": self.effective_at <= current,
            "pending": self.effective_at > current,
        }


@dataclass
class ReleaseApplication:
    """一笔质量闸放行申请：受理时冻结版本语义，确认时产出有效判定。"""

    app_id: str
    gate_id: str
    fact_table: str
    check_item: str
    measured: float
    window_id: str
    window_start: str
    window_end: str
    expected_rows: int
    submitted_at: datetime
    client_request_id: str
    received_version: int
    received_threshold: float
    received_cap: int
    received_policy: str
    shift_id: str
    reviewer: str
    state: str = "待确认"
    seat_reserved: bool = False
    seat_version: int | None = None
    confirm_request_id: str = ""
    confirmed_at: datetime | None = None
    receipt_id: str | None = None
    judgement: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "application_id": self.app_id,
            "gate_id": self.gate_id,
            "fact_table": self.fact_table,
            "check_item": self.check_item,
            "measured": self.measured,
            "window_id": self.window_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "expected_rows": self.expected_rows,
            "submitted_at": _iso(self.submitted_at),
            "client_request_id": self.client_request_id,
            "received_version": self.received_version,
            "received_version_label": f"v{self.received_version}",
            "received_threshold": self.received_threshold,
            "received_cap": self.received_cap,
            "received_policy": self.received_policy,
            "shift_id": self.shift_id,
            "reviewer": self.reviewer,
            "state": self.state,
            "seat_reserved": self.seat_reserved,
            "seat_version": self.seat_version,
            "confirm_request_id": self.confirm_request_id,
            "confirmed_at": _iso(self.confirmed_at),
            "receipt_id": self.receipt_id,
            "judgement": self.judgement,
        }


@dataclass
class ReleaseReceipt:
    """实际放行后签发的回执；同一申请最多一份有效回执。"""

    receipt_id: str
    app_id: str
    judgement_id: str
    threshold_version: int
    threshold: float
    issued_at: datetime
    state: str = "valid"  # valid | consumed | revoked
    consumed_window_id: int | None = None
    consumed_at: datetime | None = None
    duplicate_suppressed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "app_id": self.app_id,
            "application_id": self.app_id,
            "judgement_id": self.judgement_id,
            "threshold_version": self.threshold_version,
            "threshold": self.threshold,
            "issued_at": _iso(self.issued_at),
            "state": self.state,
            "valid": self.state == "valid",
            "consumed_window_id": self.consumed_window_id,
            "consumed_at": _iso(self.consumed_at),
            "duplicate_suppressed": self.duplicate_suppressed,
        }


class ReleaseControlService:
    """跨质量闸与装载窗口的放行确认状态机（线程安全、幂等）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._versions: list[ThresholdVersion] = []
        self._applications: dict[str, ReleaseApplication] = {}
        self._application_order: list[str] = []
        self._receipts: dict[str, ReleaseReceipt] = {}
        self._judgements: dict[str, dict[str, Any]] = {}
        self._request_index: dict[str, str] = {}
        self._confirm_request_index: dict[str, str] = {}
        self._window_events: dict[str, list[dict[str, Any]]] = {}
        self._created_window_ids: list[int] = []
        self._seq = 0
        self.reset()

    # ------------------------------------------------------------------ 种子
    def reset(self) -> None:
        """重置为演示种子：上限 2、两笔待确认申请、窗口开启。"""
        with self._lock:
            # 装载待办写在 load_window 表里，重置时恢复种子数据，演示可重复。
            original = SEED_ROWS.get("load_window", [])
            rows = store.rows("load_window")
            rows.clear()
            rows.extend(dict(row) for row in original)
            self._created_window_ids.clear()
            # 质量闸条目的确认结论也回滚到种子，避免上一轮放行留痕串场。
            for module in ("quality_gate",):
                module_rows = store.rows(module)
                module_rows.clear()
                module_rows.extend(dict(row) for row in SEED_ROWS.get(module, []))

            self._versions = []
            self._applications = {}
            self._application_order = []
            self._receipts = {}
            self._judgements = {}
            self._request_index = {}
            self._confirm_request_index = {}
            self._window_events = {}
            self._seq = 0

            now = datetime.now(UTC)
            base = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)
            self._versions.append(ThresholdVersion(
                version=1, threshold=0.85, cap=2, handover_policy="recompute",
                shift_id="SHIFT-A", effective_at=base,
                updated_by="排班系统", reason="换班初始策略：命中率阈值 0.85，当班上限 2",
                created_at=base,
            ))
            self._window_events["销售事实表"] = [{
                "state": "open", "by": "排班系统", "reason": "装载窗口按班次开启",
                "at": base,
            }]
            for app_id, gate_id, measured, window_id, request_id in (
                ("APP-20261126-001", "QG-101", 0.88, "LW-QG-101", "seed-app-001"),
                ("APP-20261126-002", "QG-102", 0.95, "LW-QG-102", "seed-app-002"),
            ):
                app = ReleaseApplication(
                    app_id=app_id, gate_id=gate_id, fact_table="销售事实表",
                    check_item="外键命中率", measured=measured, window_id=window_id,
                    window_start="2026-10-05", window_end="2026-10-05",
                    expected_rows=1000, submitted_at=base,
                    client_request_id=request_id, received_version=1,
                    received_threshold=0.85, received_cap=2,
                    received_policy="recompute", shift_id="SHIFT-A",
                    reviewer="数据工程组",
                )
                self._applications[app_id] = app
                self._application_order.append(app_id)
                self._request_index[request_id] = app_id

    # ------------------------------------------------------------ 基础查询
    def _now(self, now: datetime | None = None) -> datetime:
        if now is not None:
            return _parse_dt(now)
        return datetime.now(UTC)

    def _current_version(self, now: datetime) -> ThresholdVersion:
        active = [v for v in self._versions if v.effective_at <= now]
        if active:
            return max(active, key=lambda v: v.version)
        return min(self._versions, key=lambda v: v.version)

    def _version(self, version: int) -> ThresholdVersion:
        for item in self._versions:
            if item.version == version:
                return item
        raise KeyError(f"阈值版本 v{version} 不存在")

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def window_snapshot(
        self, fact_table: str, at: datetime | None = None,
    ) -> dict[str, Any]:
        """窗口在某时刻的状态：只取该时刻之前已经生效的变更事件。"""
        events = self._window_events.get(fact_table, [])
        moment = self._now(at)
        visible = [e for e in events if _parse_dt(e["at"]) <= moment]
        if not visible:
            return {
                "fact_table": fact_table, "state": "open", "state_label": "开启",
                "reason": "默认开启", "by": "", "changed_at": None,
                "events": [], "reopenable": False, "as_of": _iso(moment),
            }
        last = visible[-1]
        return {
            "fact_table": fact_table,
            "state": last["state"],
            "state_label": "开启" if last["state"] == "open" else "关闭",
            "reason": last["reason"],
            "by": last["by"],
            "changed_at": _iso(_parse_dt(last["at"])),
            "events": [
                {
                    "state": event["state"],
                    "state_label": "开启" if event["state"] == "open" else "关闭",
                    "reason": event["reason"],
                    "by": event["by"],
                    "changed_at": _iso(_parse_dt(event["at"])),
                }
                for event in visible
            ],
            "scheduled_events": [
                {
                    "state": event["state"],
                    "state_label": "开启" if event["state"] == "open" else "关闭",
                    "reason": event["reason"],
                    "by": event["by"],
                    "changed_at": _iso(_parse_dt(event["at"])),
                }
                for event in events
                if _parse_dt(event["at"]) > moment
            ],
            "reopenable": last["state"] == "closed",
            "as_of": _iso(moment),
        }

    def _seats_used(self) -> int:
        """当班名额占用：已放行 + 受理时已占位的待确认申请。

        ``reserve`` 申请在受理那一刻就占住受理班次的名额，换班后仍按受理阈值
        交接，所以上限调低后它依然占着名额；``recompute`` 申请受理时不占名额，
        直到实际放行才占用，所以降上限后新申请只能按现行阈值和剩余名额受理。
        """
        used = 0
        for app_id in self._application_order:
            app = self._applications[app_id]
            if app.state == "已放行":
                used += 1
            elif app.state == "待确认" and app.seat_reserved:
                used += 1
        return used

    def _application(self, app_id: str) -> ReleaseApplication | None:
        return self._applications.get(app_id)

    # -------------------------------------------------------------- 版本管理
    def adjust_version(
        self,
        *,
        threshold: float | None = None,
        cap: int | None = None,
        handover_policy: str | None = None,
        shift_id: str | None = None,
        effective_at: str | datetime | None = None,
        updated_by: str = "值班员",
        reason: str = "",
        now: datetime | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """由同事调整阈值/上限，生成新的阈值版本并返回重新判定预览。"""
        with self._lock:
            moment = self._now(now)
            current = self._current_version(moment)
            policy = handover_policy or current.handover_policy
            if policy not in HANDOVER_POLICIES:
                return False, f"换班策略 {policy} 无效，只能选择 reserve 或 recompute", None
            new_threshold = float(threshold) if threshold is not None else current.threshold
            new_cap = int(cap) if cap is not None else current.cap
            if new_threshold < 0 or new_threshold > 1:
                return False, f"阈值 {new_threshold} 超出 [0, 1] 范围", None
            if new_cap < 0:
                return False, f"当班上限 {new_cap} 不能为负数", None
            effective = _parse_dt(effective_at, moment)
            version = ThresholdVersion(
                version=self._versions[-1].version + 1,
                threshold=new_threshold,
                cap=new_cap,
                handover_policy=policy,
                shift_id=shift_id or current.shift_id,
                effective_at=effective,
                updated_by=updated_by or "值班员",
                reason=reason or "调整换班阈值与当班上限",
                created_at=moment,
            )
            self._versions.append(version)
            previews = self.reevaluate_pending(now=moment)
            detail = {
                "version": version.to_dict(moment),
                "current_version": current.to_dict(moment),
                "effective_now": effective <= moment,
                "previews": previews,
                "priority": list(EVALUATION_PRIORITY),
            }
            if effective <= moment:
                message = (
                    f"阈值版本 v{version.version} 已生效：阈值 {version.threshold}，"
                    f"当班上限 {version.cap}，换班策略 {HANDOVER_POLICY_LABELS[policy]}"
                )
            else:
                message = (
                    f"阈值版本 v{version.version} 将于 {_iso(effective)} 生效："
                    f"阈值 {version.threshold}，当班上限 {version.cap}；"
                    f"生效前仍按 v{current.version} 判定"
                )
            return True, message, detail

    def list_versions(self, now: datetime | None = None) -> list[dict[str, Any]]:
        with self._lock:
            moment = self._now(now)
            return [v.to_dict(moment) for v in self._versions]

    def next_change_at(self, now: datetime | None = None) -> datetime | None:
        """下一个即将生效的阈值版本或窗口变更时刻。"""
        moment = self._now(now)
        candidates = [
            v.effective_at for v in self._versions if v.effective_at > moment
        ]
        for events in self._window_events.values():
            candidates.extend(
                _parse_dt(e["at"]) for e in events if _parse_dt(e["at"]) > moment
            )
        return min(candidates) if candidates else None

    def project_handover(self, now: datetime | None = None) -> dict[str, Any]:
        """值班员查询：下一次变更何时生效，变更前已受理未完成的提交能否交接。"""
        with self._lock:
            moment = self._now(now)
            change_at = self.next_change_at(moment)
            if change_at is None:
                return {
                    "next_change_at": None,
                    "current_version": self._current_version(moment).to_dict(moment),
                    "previews": self.reevaluate_pending(now=moment),
                    "handover_decisions": [],
                    "message": "当前没有待生效的阈值或窗口变更",
                }
            previews: list[dict[str, Any]] = []
            decisions: list[dict[str, Any]] = []
            for app_id in self._application_order:
                app = self._applications[app_id]
                if app.state != "待确认":
                    continue
                preview = self._evaluate(app, change_at)
                preview["projected_at"] = _iso(change_at)
                preview["projection"] = True
                previews.append(preview)
                decisions.append({
                    "application_id": app.app_id,
                    "gate_id": app.gate_id,
                    "window_id": app.window_id,
                    "accepted_version": f"v{app.received_version}",
                    "accepted_threshold": app.received_threshold,
                    "policy": app.received_policy,
                    "policy_label": HANDOVER_POLICY_LABELS[app.received_policy],
                    "seat_reserved": app.seat_reserved,
                    "seat_reserved_at_acceptance": app.received_policy == "reserve",
                    "outcome_at_change": preview["outcome_label"],
                    "reason_code": preview["reason_code"],
                    "can_handover": preview["handover"]["can_handover"],
                    "handover_reason": preview["handover"]["reason"],
                })
            return {
                "next_change_at": _iso(change_at),
                "changes_at": _iso(change_at),
                "current_version": self._current_version(moment).to_dict(moment),
                "version_at_change": self._current_version(change_at).to_dict(change_at),
                "window_at_change": {
                    table: self.window_snapshot(table, change_at)
                    for table in sorted(self._window_events)
                },
                "handover_decisions": decisions,
                "previews": previews,
                "priority": list(EVALUATION_PRIORITY),
                "message": (
                    f"下一次变更将于 {_iso(change_at)} 生效；"
                    "下表给出变更前已受理但尚未完成的提交在生效时刻的交接结论"
                ),
            }

    # -------------------------------------------------------------- 窗口管理
    def set_window(
        self,
        *,
        fact_table: str,
        state: str,
        by: str = "值班员",
        reason: str = "",
        effective_at: str | datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """开启或关闭装载窗口；关闭后拒绝新放行。

        支持预约生效：与上限调整落在同一生效时刻时，判定优先级里
        ``window_closed`` 排在 ``capacity`` 之前，窗口关闭胜出。
        """
        with self._lock:
            if state not in WINDOW_STATES:
                return False, f"窗口状态 {state} 无效，只能为 open 或 closed", self.window_snapshot(fact_table)
            moment = self._now(now)
            effective = _parse_dt(effective_at, moment)
            events = self._window_events.setdefault(fact_table, [])
            visible = [e for e in events if _parse_dt(e["at"]) <= moment]
            previous = visible[-1]["state"] if visible else "open"
            if effective <= moment and previous == state:
                label = "关闭" if state == "closed" else "开启"
                return False, f"装载窗口「{fact_table}」已经处于{label}状态，未重复登记", self.window_snapshot(fact_table, moment)
            event = {
                "state": state, "by": by or "值班员",
                "reason": reason or ("窗口关闭" if state == "closed" else "窗口重新开启"),
                "at": effective,
            }
            events.append(event)
            events.sort(key=lambda item: _parse_dt(item["at"]))
            snapshot = self.window_snapshot(fact_table, moment)
            previews = self.reevaluate_pending(now=moment)
            detail = {
                "window": snapshot,
                "previews": previews,
                "priority": list(EVALUATION_PRIORITY),
                "effective_now": effective <= moment,
                "scheduled_at": _iso(effective) if effective > moment else None,
            }
            label = "关闭" if state == "closed" else "开启"
            if effective > moment:
                message = (
                    f"装载窗口「{fact_table}」将于 {_iso(effective)} {label}；"
                    "生效前仍按当前窗口状态判定，生效后关闭优先于上限调整"
                )
            else:
                message = f"装载窗口「{fact_table}」已{label}；" + (
                    "关闭后拒绝新放行，已签发回执仍可完成装载" if state == "closed"
                    else "重新开启后可核对已受理申请的实际结论"
                )
            return True, message, detail

    # -------------------------------------------------------------- 申请受理
    def submit_application(
        self,
        *,
        gate_id: str,
        fact_table: str,
        check_item: str,
        measured: float,
        window_id: str = "",
        window_start: str = "",
        window_end: str = "",
        expected_rows: int = 1000,
        handover_policy: str | None = None,
        reviewer: str = "值班员",
        client_request_id: str = "",
        now: datetime | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """质量闸受理放行申请：冻结受理版本与名额语义。"""
        with self._lock:
            moment = self._now(now)
            request_id = (client_request_id or "").strip()
            if request_id and request_id in self._request_index:
                existing = self._applications[self._request_index[request_id]]
                payload = {
                    "application": existing.to_dict(),
                    "duplicate_submission": True,
                    "message": "重复提交命中同一受理申请，未重复排队",
                    "window": self.window_snapshot(fact_table, moment),
                }
                return existing.state != "已拒绝", (
                    f"重复提交命中已受理申请 {existing.app_id}，沿用原受理结论，未重复排队"
                ), payload

            current = self._current_version(moment)
            policy = handover_policy or current.handover_policy
            if policy not in HANDOVER_POLICIES:
                return False, f"换班策略 {policy} 无效", {"application": None}
            if not window_id:
                window_id = f"LW-{gate_id or 'QG'}"
            app = ReleaseApplication(
                app_id=f"APP-{moment.strftime('%Y%m%d')}-{len(self._application_order) + 1:03d}",
                gate_id=gate_id, fact_table=fact_table, check_item=check_item,
                measured=float(measured), window_id=window_id,
                window_start=window_start or window_end, window_end=window_end or window_start,
                expected_rows=int(expected_rows), submitted_at=moment,
                client_request_id=request_id,
                received_version=current.version,
                received_threshold=current.threshold,
                received_cap=current.cap,
                received_policy=policy,
                shift_id=current.shift_id,
                reviewer=reviewer or "值班员",
            )
            self._applications[app.app_id] = app
            self._application_order.append(app.app_id)
            if request_id:
                self._request_index[request_id] = app.app_id

            window = self.window_snapshot(fact_table, moment)
            if window["state"] == "closed":
                app.state = "已拒绝"
                app.judgement = self._judgement(
                    app, moment, outcome="rejected", reason_code="window_closed",
                    reason=(
                        f"装载窗口「{fact_table}」已于 {window['changed_at']} 关闭"
                        f"（操作人 {window['by']}，原因：{window['reason']}）；"
                        "已关闭的窗口拒绝新放行，申请未进入装载待办"
                    ),
                    effective_version=current, capacity_used=self._seats_used(),
                    capacity_limit=current.cap, seat_held=False, checks=[{
                        "priority": 1, "check": "window_closed", "status": "fail",
                        "detail": f"窗口已关闭：{window['reason']}",
                    }],
                )
                return False, app.judgement["reason"], {
                    "application": app.to_dict(),
                    "judgement": app.judgement,
                    "window": window,
                    "accepted": False,
                    "duplicate_submission": False,
                }

            if policy == "reserve":
                used = self._seats_used()
                if used >= current.cap:
                    app.state = "已拒绝"
                    app.judgement = self._judgement(
                        app, moment, outcome="rejected", reason_code="capacity_exhausted",
                        reason=(
                            f"受理按现行阈值版本 v{current.version}（阈值 {current.threshold}），"
                            f"但当班名额已用尽（{used}/{current.cap}），新申请不能占位受理"
                        ),
                        effective_version=current, capacity_used=used,
                        capacity_limit=current.cap, seat_held=False, checks=[{
                            "priority": 4, "check": "capacity", "status": "fail",
                            "detail": f"当班名额已用尽 {used}/{current.cap}",
                        }],
                    )
                    return False, app.judgement["reason"], {
                        "application": app.to_dict(),
                        "judgement": app.judgement,
                        "window": window,
                        "accepted": False,
                        "duplicate_submission": False,
                    }
                app.seat_reserved = True
                app.seat_version = current.version

            preview = self._evaluate(app, moment)
            app.judgement = preview
            return True, (
                f"放行申请 {app.app_id} 已受理：按阈值版本 v{current.version}"
                f"（阈值 {current.threshold}，上限 {current.cap}）受理，"
                f"{HANDOVER_POLICY_LABELS[policy]}；尚待确认"
            ), {
                "application": app.to_dict(),
                "judgement": preview,
                "window": window,
                "accepted": True,
                "duplicate_submission": False,
            }

    # -------------------------------------------------------------- 判定核心
    def _judgement(
        self,
        app: ReleaseApplication,
        moment: datetime,
        *,
        outcome: str,
        reason_code: str,
        reason: str,
        effective_version: ThresholdVersion,
        capacity_used: int,
        capacity_limit: int,
        seat_held: bool,
        checks: list[dict[str, Any]],
        preview: bool = False,
        capacity_after: int | None = None,
    ) -> dict[str, Any]:
        current = self._current_version(moment)
        after = capacity_after if capacity_after is not None else capacity_used + (
            0 if seat_held else 1)
        at_cap = outcome == "released" and after >= capacity_limit
        in_shift_change = (
            app.received_version != current.version
            or app.received_policy != current.handover_policy
        )
        if outcome == "released":
            if app.received_policy == "reserve" and app.seat_reserved:
                handover_reason = (
                    f"该笔受理时已按 v{app.received_version} 占住当班名额，"
                    f"换班后按受理阈值 {effective_version.threshold} 交接，仍可交班"
                )
            elif in_shift_change:
                handover_reason = (
                    f"受理于阈值版本 v{app.received_version}，受理时不占名额；"
                    f"已按现行阈值版本 v{current.version} 重算通过，可以交班"
                )
            else:
                handover_reason = "同一班次内确认，按受理阈值版本判定，可以交班"
            can_handover = True
        else:
            can_handover = False
            if reason_code == "window_closed":
                handover_reason = (
                    f"生效时刻装载窗口已关闭，窗口关闭优先于阈值/名额判定；"
                    f"该笔受理于 v{app.received_version}，不能交班"
                )
            elif reason_code == "threshold_not_met":
                if seat_held:
                    handover_reason = (
                        f"该笔已按受理版本 v{app.received_version} 占住名额，但实测 "
                        f"{app.measured:.3f} 低于受理阈值 {effective_version.threshold}，"
                        "不能交班"
                    )
                else:
                    handover_reason = (
                        f"受理于阈值版本 v{app.received_version}（阈值 "
                        f"{app.received_threshold}），受理时不占名额；按现行 v{current.version} "
                        f"阈值 {current.threshold} 重算后实测 {app.measured:.3f} 不达标，"
                        "不能交班"
                    )
            elif reason_code == "capacity_exhausted":
                if seat_held:
                    handover_reason = (
                        f"受理时占位版本 v{app.received_version} 的名额已按受理阈值判定，"
                        "但占位未能保留，不能交班"
                    )
                elif in_shift_change:
                    handover_reason = (
                        f"受理于阈值版本 v{app.received_version}，受理时不占名额；"
                        f"现行 v{current.version} 名额已用尽（{capacity_used}/{capacity_limit}），"
                        "该笔不能交班，只能等待下一班次按新阈值重新受理"
                    )
                else:
                    handover_reason = (
                        f"当班名额已用尽（{capacity_used}/{capacity_limit}），不能交班"
                    )
            else:
                handover_reason = f"判定结论为{reason_code}，不能交班"
        return {
            "judgement_id": "PREVIEW" if preview else self._next_id("JDG"),
            "preview": preview,
            "application_id": app.app_id,
            "gate_id": app.gate_id,
            "fact_table": app.fact_table,
            "check_item": app.check_item,
            "measured": app.measured,
            "window_id": app.window_id,
            "evaluated_at": _iso(moment),
            "outcome": outcome,
            "outcome_label": {"released": "已放行", "rejected": "已拒绝", "pending": "待确认"}[outcome],
            "reason_code": reason_code,
            "reason": reason,
            "threshold_version": effective_version.version,
            "threshold": effective_version.threshold,
            "cap": effective_version.cap,
            "handover_policy": app.received_policy,
            "handover_policy_label": HANDOVER_POLICY_LABELS[app.received_policy],
            "shift_id": current.shift_id,
            "window_state": self.window_snapshot(app.fact_table, moment)["state"],
            "window": self.window_snapshot(app.fact_table, moment),
            "capacity": {
                "used": capacity_used,
                "limit": capacity_limit,
                "after_confirm": after,
                "at_cap": at_cap,
                "remaining": max(capacity_limit - capacity_used, 0),
            },
            "handover": {
                "in_shift_change": in_shift_change,
                "accepted_version": app.received_version,
                "accepted_version_label": f"v{app.received_version}",
                "effective_version": effective_version.version,
                "effective_version_label": f"v{effective_version.version}",
                "current_version": current.version,
                "current_version_effective_at": _iso(current.effective_at),
                "policy": app.received_policy,
                "seat_reserved": seat_held,
                "seat_version": app.seat_version,
                "can_handover": can_handover,
                "reason": handover_reason,
            },
            "checks": checks,
            "priority": list(EVALUATION_PRIORITY),
            "consistent_judgement": True,
            "consumers": ["quality_gate"],
        }

    def _evaluate(self, app: ReleaseApplication, moment: datetime) -> dict[str, Any]:
        """按固定优先级计算一份有效判定（不落库）。"""
        current = self._current_version(moment)
        window = self.window_snapshot(app.fact_table, moment)
        checks: list[dict[str, Any]] = []
        seat_held = app.received_policy == "reserve" and app.seat_reserved
        effective = self._version(app.seat_version) if seat_held and app.seat_version else current
        capacity_limit = effective.cap if seat_held else current.cap
        capacity_used = self._seats_used()

        # 1. 窗口状态优先：关闭窗口拒绝新放行。
        if window["state"] == "closed":
            checks.append({
                "priority": 1, "check": "window_closed", "status": "fail",
                "detail": f"窗口已于 {window['changed_at']} 关闭：{window['reason']}",
            })
            return self._judgement(
                app, moment, outcome="rejected", reason_code="window_closed",
                reason=(
                    f"装载窗口「{app.fact_table}」已于 {window['changed_at']} 关闭"
                    f"（操作人 {window['by']}，原因：{window['reason']}）；"
                    "窗口关闭优先于上限调整，本次不放行"
                ),
                effective_version=current, capacity_used=capacity_used,
                capacity_limit=capacity_limit, seat_held=False, checks=checks,
                preview=True,
            )
        checks.append({
            "priority": 1, "check": "window_closed", "status": "pass",
            "detail": "窗口开启，允许继续判定",
        })

        # 2. 换班策略：占位申请沿用受理版本，重算申请使用现行版本。
        if seat_held:
            checks.append({
                "priority": 2, "check": "handover_policy", "status": "pass",
                "detail": (
                    f"受理占位：已按 v{effective.version} 占住名额，"
                    f"换班后按受理阈值 {effective.threshold} 交接"
                ),
            })
        else:
            in_shift_change = (
                app.received_version != current.version
                or app.received_policy != current.handover_policy
            )
            checks.append({
                "priority": 2, "check": "handover_policy",
                "status": "attention" if in_shift_change else "pass",
                "detail": (
                    f"受理不占名额，按现行阈值版本 v{current.version} 重算"
                    if in_shift_change else "同班次受理，按现行阈值版本判定"
                ),
            })

        # 3. 阈值判定。
        if app.measured < effective.threshold:
            checks.append({
                "priority": 3, "check": "threshold", "status": "fail",
                "detail": f"实测 {app.measured:.3f} < 生效阈值 {effective.threshold}",
            })
            return self._judgement(
                app, moment, outcome="rejected", reason_code="threshold_not_met",
                reason=(
                    f"实测{app.check_item} {app.measured:.3f} 低于生效阈值版本 "
                    f"v{effective.version} 的阈值 {effective.threshold}，拒绝放行"
                ),
                effective_version=effective, capacity_used=capacity_used,
                capacity_limit=capacity_limit, seat_held=seat_held, checks=checks,
                preview=True,
            )
        checks.append({
            "priority": 3, "check": "threshold", "status": "pass",
            "detail": f"实测 {app.measured:.3f} >= 生效阈值 {effective.threshold}",
        })

        # 4. 名额判定；占位申请已占名额，不再重复扣减。
        if not seat_held and capacity_used >= capacity_limit:
            checks.append({
                "priority": 4, "check": "capacity", "status": "fail",
                "detail": f"当班名额已用尽 {capacity_used}/{capacity_limit}",
            })
            return self._judgement(
                app, moment, outcome="rejected", reason_code="capacity_exhausted",
                reason=(
                    f"当班名额已用尽（{capacity_used}/{capacity_limit}），"
                    "该申请受理时不占名额，须按现行阈值重算，本次不能放行交班"
                ),
                effective_version=effective, capacity_used=capacity_used,
                capacity_limit=capacity_limit, seat_held=False, checks=checks,
                preview=True,
            )
        after = capacity_used if seat_held else capacity_used + 1
        if seat_held:
            checks.append({
                "priority": 4, "check": "capacity", "status": "pass",
                "detail": (
                    f"该申请受理时已占住当班名额，按受理版本 v{effective.version} 的"
                    f"上限 {capacity_limit} 交接，不再重新占用新名额"
                ),
            })
        else:
            checks.append({
                "priority": 4, "check": "capacity", "status": "pass",
                "detail": f"放行后名额 {after}/{capacity_limit}，未超过生效上限",
            })

        if seat_held:
            reason = (
                f"实测{app.check_item} {app.measured:.3f} >= 受理阈值 {effective.threshold}"
                f"（版本 v{effective.version}）；该申请受理时已占住当班名额，"
                f"换班后按受理阈值交接放行（名额 {capacity_used}/{capacity_limit}）"
            )
        elif after == capacity_limit:
            reason = (
                f"实测{app.check_item} {app.measured:.3f} >= 生效阈值 {effective.threshold}"
                f"（版本 v{effective.version}）；放行后当班名额 {after}/{capacity_limit}，"
                f"等于生效上限 {capacity_limit}，准予放行"
            )
        else:
            reason = (
                f"实测{app.check_item} {app.measured:.3f} >= 生效阈值 {effective.threshold}"
                f"（版本 v{effective.version}）；放行后当班名额 {after}/{capacity_limit}，"
                "未超上限，准予放行"
            )
        return self._judgement(
            app, moment, outcome="released", reason_code="released_at_cap" if after == capacity_limit else "released",
            reason=reason, effective_version=effective, capacity_used=capacity_used,
            capacity_limit=capacity_limit, seat_held=seat_held, checks=checks,
            preview=True, capacity_after=after,
        )

    def preview_application(self, app_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        with self._lock:
            app = self._application(app_id)
            if app is None:
                return None
            if app.state != "待确认":
                return app.judgement
            return self._evaluate(app, self._now(now))

    def reevaluate_pending(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """重新判定全部待确认申请，供确认框展示最新阈值下的预览结论。"""
        with self._lock:
            moment = self._now(now)
            previews: list[dict[str, Any]] = []
            for app_id in self._application_order:
                app = self._applications[app_id]
                if app.state != "待确认":
                    continue
                app.judgement = self._evaluate(app, moment)
                previews.append(app.judgement)
            return previews

    # -------------------------------------------------------------- 放行确认
    def confirm_release(
        self,
        app_id: str,
        *,
        client_request_id: str = "",
        now: datetime | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """质量闸确认接口：实际放行或拒绝，并返回换班语境下的原因。"""
        with self._lock:
            moment = self._now(now)
            app = self._application(app_id)
            if app is None:
                return False, f"放行申请 {app_id} 不存在或已归档", {
                    "application": None, "judgement": None, "receipt": None,
                }
            request_id = (client_request_id or "").strip()

            if app.state == "已放行":
                receipt = self._receipts.get(app.receipt_id or "")
                if receipt:
                    receipt.duplicate_suppressed += 1
                return True, (
                    f"重复确认命中同一申请：已沿用放行回执 {app.receipt_id}，"
                    "未重复签发回执，也未重复写入装载待办"
                    + ("（网络重试）" if request_id and request_id == app.confirm_request_id else "")
                ), {
                    "application": app.to_dict(),
                    "judgement": app.judgement,
                    "receipt": receipt.to_dict() if receipt else None,
                    "duplicate": True,
                    "todo": self._todo_for_app(app.app_id),
                }

            if app.state == "已拒绝":
                return False, app.judgement["reason"] if app.judgement else "申请已被拒绝", {
                    "application": app.to_dict(),
                    "judgement": app.judgement,
                    "receipt": None,
                    "duplicate": bool(request_id and request_id == app.confirm_request_id),
                }

            if request_id and request_id in self._confirm_request_index:
                existing_app = self._applications[self._confirm_request_index[request_id]]
                if existing_app is not app:
                    return False, (
                        f"请求号 {request_id} 已用于申请 {existing_app.app_id} 的确认，"
                        "不能重复用于另一笔申请"
                    ), {
                        "application": app.to_dict(), "judgement": app.judgement,
                        "receipt": None, "duplicate": True,
                    }

            if request_id:
                app.confirm_request_id = request_id
                self._confirm_request_index[request_id] = app.app_id

            judgement = self._evaluate(app, moment)
            judgement["judgement_id"] = self._next_id("JDG")
            judgement["preview"] = False
            judgement["confirmed_at"] = _iso(moment)
            app.judgement = judgement
            app.confirmed_at = moment
            self._judgements[judgement["judgement_id"]] = judgement

            if judgement["outcome"] != "released":
                app.state = "已拒绝"
                if app.seat_reserved:
                    # 占位申请被拒后释放名额，让后续申请可以按现行阈值受理。
                    app.seat_reserved = False
                judgement["handover"]["seat_reserved"] = False
                judgement["handover"]["can_handover"] = False
                self._sync_gate_row(app, judgement)
                return False, judgement["reason"], {
                    "application": app.to_dict(),
                    "judgement": judgement,
                    "receipt": None,
                    "duplicate": False,
                    "todo": None,
                }

            # 实际放行：锁定名额、签发唯一回执、写入装载待办。
            app.state = "已放行"
            if not app.seat_reserved:
                app.seat_version = judgement["threshold_version"]
            receipt = ReleaseReceipt(
                receipt_id=self._next_id("RCPT"),
                app_id=app.app_id,
                judgement_id=judgement["judgement_id"],
                threshold_version=judgement["threshold_version"],
                threshold=judgement["threshold"],
                issued_at=moment,
            )
            self._receipts[receipt.receipt_id] = receipt
            app.receipt_id = receipt.receipt_id
            todo = self._create_load_todo(app, receipt, judgement)
            judgement["receipt_id"] = receipt.receipt_id
            judgement["load_todo_id"] = todo.get("id") if todo else None
            self._sync_gate_row(app, judgement)
            return True, judgement["reason"], {
                "application": app.to_dict(),
                "judgement": judgement,
                "receipt": receipt.to_dict(),
                "duplicate": False,
                "todo": todo,
            }

    def _sync_gate_row(self, app: ReleaseApplication, judgement: dict[str, Any]) -> None:
        """把确认结论回写到质量闸条目，便于值班员在原有模块里核对。"""
        for row in store.rows("quality_gate"):
            if str(row.get("闸口编号", "")) != app.gate_id:
                continue
            released = judgement["outcome"] == "released"
            row["实测读数"] = f"{app.measured:g}"
            row["阈值声明"] = f"{judgement['threshold']:g}"
            row["通过判定"] = "是" if released else "否"
            row["status"] = "已放行" if released else "已阻断"
            row["pending"] = False
            row["abnormal"] = not released
            row["判定编号"] = judgement["judgement_id"]
            row["阈值版本"] = f"v{judgement['threshold_version']}"
            row["最近执行"] = str(judgement.get("confirmed_at", ""))[:10]
            if not released:
                row["阻断范围"] = row.get("阻断范围") or "批次级"
                row["阻断原因"] = judgement["reason"]
            else:
                row["阻断范围"] = ""
                row.pop("阻断原因", None)
            return

    # -------------------------------------------------------------- 装载交接
    def _create_load_todo(
        self, app: ReleaseApplication, receipt: ReleaseReceipt, judgement: dict[str, Any],
    ) -> dict[str, Any] | None:
        """只在实际放行后写入装载待办；同一申请至多一条。"""
        existing = self._todo_for_app(app.app_id)
        if existing is not None:
            return existing
        rows = store.rows("load_window")
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "窗口编号": app.window_id,
            "所属事实表": app.fact_table,
            "窗口起点": app.window_start,
            "窗口终点": app.window_end,
            "预期行数": app.expected_rows,
            "实际行数": "",
            "装载水位": "",
            "装载耗时": "",
            "放行回执": receipt.receipt_id,
            "放行申请": app.app_id,
            "判定编号": judgement["judgement_id"],
            "阈值版本": f"v{judgement['threshold_version']}",
            "放行判定": "已放行",
            "status": "待装载",
            "pending": True,
            "abnormal": False,
        }
        rows.append(entry)
        self._created_window_ids.append(int(entry["id"]))
        return entry

    def _todo_for_app(self, app_id: str) -> dict[str, Any] | None:
        for row in store.rows("load_window"):
            if row.get("放行申请") == app_id:
                return row
        return None

    def todo_for_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        for row in store.rows("load_window"):
            if row.get("放行回执") == receipt_id:
                return row
        return None

    def released_application_for_gate(self, gate_id: str) -> dict[str, Any] | None:
        """按闸口编号取最近一笔实际放行的申请及其有效回执。"""
        found: ReleaseApplication | None = None
        for app_id in self._application_order:
            app = self._applications[app_id]
            if app.gate_id == gate_id and app.state == "已放行" and app.receipt_id:
                found = app
        if found is None:
            return None
        receipt = self._receipts.get(found.receipt_id or "")
        if receipt is None:
            return None
        return {
            "application": found.to_dict(),
            "judgement": self._judgements.get(receipt.judgement_id),
            "receipt": receipt.to_dict(),
        }

    def find_window(self, window_key: str | int) -> dict[str, Any] | None:
        rows = store.rows("load_window")
        text = str(window_key)
        if text.isdigit():
            for row in rows:
                if int(row.get("id", 0)) == int(text):
                    return row
        for row in rows:
            if str(row.get("窗口编号", "")) == text:
                return row
        return None

    def check_load_ready(
        self,
        window_key: str | int,
        *,
        receipt_id: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None, dict[str, Any]]:
        """装载前置核对：回执必须有效、未核销且属于该窗口，但不改变任何状态。"""
        with self._lock:
            row = self.find_window(window_key)
            if row is None:
                return False, f"装载窗口 {window_key} 不存在或已归档", None, {"receipt": None}
            if row.get("status") in ("已装载", "装载中"):
                return False, (
                    f"装载窗口「{row.get('窗口编号')}」当前状态为 {row.get('status')}，"
                    "不能再次启动装载"
                ), row, {"receipt": None}
            rid = (receipt_id or row.get("放行回执") or "").strip()
            if not rid:
                return False, (
                    f"装载窗口「{row.get('窗口编号')}」没有关联有效放行回执，"
                    "被拒或尚待结论的申请不能执行装载推进"
                ), row, {"receipt": None}
            receipt = self._receipts.get(rid)
            if receipt is None or receipt.state != "valid":
                return False, (
                    f"放行回执 {rid} 不存在、已核销或已作废，装载员不得执行推进"
                ), row, {"receipt": receipt.to_dict() if receipt else None}
            if str(row.get("放行申请") or "") != receipt.app_id:
                return False, (
                    f"放行回执 {rid} 属于申请 {receipt.app_id}，"
                    f"与装载窗口「{row.get('窗口编号')}」不匹配，不得交叉核销"
                ), row, {"receipt": receipt.to_dict()}
            judgement = self._judgements.get(receipt.judgement_id)
            return True, (
                f"回执 {rid} 有效，可凭其确认装载；判定 {receipt.judgement_id} 与质量闸一致"
            ), row, {"receipt": receipt.to_dict(), "judgement": judgement}

    def confirm_load(
        self,
        window_key: str | int,
        *,
        receipt_id: str | None = None,
        actual_rows: int | None = None,
        client_request_id: str = "",
        now: datetime | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None, dict[str, Any]]:
        """装载员凭有效回执确认装载；无效/待定/重复回执显式挡住。"""
        with self._lock:
            moment = self._now(now)
            row = self.find_window(window_key)
            if row is None:
                return False, f"装载窗口 {window_key} 不存在或已归档", None, {
                    "judgement": None, "receipt": None,
                }
            if row.get("status") in ("已装载", "装载中断"):
                receipt = self._receipts.get(str(row.get("放行回执") or ""))
                return False, (
                    f"装载窗口「{row.get('窗口编号')}」当前状态为 {row.get('status')}，"
                    "同一回执不能重复推进装载"
                ), row, {
                    "judgement": self._judgements.get(receipt.judgement_id) if receipt else None,
                    "receipt": receipt.to_dict() if receipt else None,
                    "duplicate": True,
                }

            rid = (receipt_id or row.get("放行回执") or "").strip()
            if not rid:
                linked_app = self._applications.get(str(row.get("放行申请") or ""))
                if linked_app is not None and linked_app.state == "待确认":
                    detail_text = (
                        f"申请 {linked_app.app_id} 尚待质量闸结论，尚未签发回执"
                    )
                elif linked_app is not None and linked_app.state == "已拒绝":
                    detail_text = (
                        f"申请 {linked_app.app_id} 已被拒绝，没有有效回执"
                    )
                else:
                    detail_text = "没有关联有效放行回执"
                return False, (
                    f"装载窗口「{row.get('窗口编号')}」{detail_text}，"
                    "被拒或尚待结论的申请不能执行装载推进"
                ), row, {"judgement": None, "receipt": None}

            receipt = self._receipts.get(rid)
            if receipt is None:
                return False, (
                    f"放行回执 {rid} 不存在或已作废，装载员不得执行推进"
                ), row, {"judgement": None, "receipt": None}

            if receipt.state == "consumed":
                return False, (
                    f"放行回执 {rid} 已于 {_iso(receipt.consumed_at)} "
                    f"用于装载窗口 {receipt.consumed_window_id}，不得重复推进"
                ), row, {
                    "judgement": self._judgements.get(receipt.judgement_id),
                    "receipt": receipt.to_dict(),
                    "duplicate": True,
                }

            app = self._applications.get(receipt.app_id)
            if app is None or app.state != "已放行":
                state = app.state if app else "已归档"
                return False, (
                    f"放行申请 {receipt.app_id} 当前为「{state}」，"
                    "只有实际放行的申请才能执行装载推进"
                ), row, {
                    "judgement": app.judgement if app else None,
                    "receipt": receipt.to_dict(),
                }

            # 回执只能核销它对应的那一个装载待办窗口，防止拿 A 的回执推进 B。
            if str(row.get("放行申请") or "") != receipt.app_id:
                return False, (
                    f"放行回执 {rid} 属于申请 {receipt.app_id}，"
                    f"与装载窗口「{row.get('窗口编号')}」不匹配，不得交叉核销"
                ), row, {
                    "judgement": self._judgements.get(receipt.judgement_id),
                    "receipt": receipt.to_dict(),
                }

            judgement = self._judgements.get(receipt.judgement_id)
            if judgement is None or judgement.get("outcome") != "released":
                return False, (
                    "回执对应的有效判定不是放行结论，装载动作被挡住"
                ), row, {"judgement": judgement, "receipt": receipt.to_dict()}

            expected = int(row.get("预期行数") or 0)
            actual = int(actual_rows) if actual_rows is not None else expected
            if expected <= 0:
                return False, f"装载窗口「{row.get('窗口编号')}」预期行数 {expected} 无效", row, {
                    "judgement": judgement, "receipt": receipt.to_dict(),
                }
            deviation = abs(actual - expected) / expected
            if deviation > 0.20:
                return False, (
                    f"实际行数 {actual} 与预期 {expected} 偏差 {deviation:.1%} 超过 20%，"
                    "窗口不得推进"
                ), row, {"judgement": judgement, "receipt": receipt.to_dict()}

            # 消耗回执并推进窗口；判定仍为质量闸确认时那一份。
            receipt.state = "consumed"
            receipt.consumed_window_id = int(row.get("id", 0))
            receipt.consumed_at = moment
            row["status"] = "已装载"
            row["pending"] = False
            row["abnormal"] = False
            row["实际行数"] = actual
            row["装载水位"] = row.get("窗口终点") or row.get("窗口起点")
            row["装载耗时"] = "1.5s"
            row["装载回执状态"] = "已核销"
            row["装载确认时间"] = _iso(moment)
            if "load_window" not in judgement.get("consumers", []):
                judgement.setdefault("consumers", []).append("load_window")
            return True, (
                f"装载窗口「{row.get('窗口编号')}」凭回执 {rid} 确认装载，"
                f"水位推进至 {row['装载水位']}；与质量闸共用判定 {judgement['judgement_id']}"
            ), row, {
                "judgement": judgement,
                "judgement_id": judgement["judgement_id"],
                "receipt": receipt.to_dict(),
                "judgement_consistent": True,
                "window_state": self.window_snapshot(app.fact_table),
            }

    # ---------------------------------------------------------------- 对账
    def get_application(self, app_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        """核对实际结论：回包不明时重新打开质量闸即可读到已落库的判定。"""
        with self._lock:
            app = self._application(app_id)
            if app is None:
                return None
            judgement = app.judgement or self._evaluate(app, self._now(now))
            receipt = self._receipts.get(app.receipt_id or "")
            return {
                "application": app.to_dict(),
                "judgement": judgement,
                "receipt": receipt.to_dict() if receipt else None,
                "todo": self._todo_for_app(app.app_id),
                "actual_conclusion": {
                    "state": app.state,
                    "outcome": judgement.get("outcome"),
                    "reason_code": judgement.get("reason_code"),
                    "reason": judgement.get("reason"),
                    "receipt_id": app.receipt_id,
                    "receipt_state": receipt.state if receipt else None,
                },
            }

    def control_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """值班员视角：当前阈值版本、窗口状态、待确认申请与回执/待办。"""
        with self._lock:
            moment = self._now(now)
            current = self._current_version(moment)
            pending_previews = self.reevaluate_pending(now=moment)
            apps = [self._applications[app_id].to_dict() for app_id in self._application_order]
            return {
                "current_version": current.to_dict(moment),
                "versions": [v.to_dict(moment) for v in self._versions],
                "next_version": next(
                    (v.to_dict(moment) for v in self._versions if v.effective_at > moment),
                    None,
                ),
                "shift_change": {
                    "active_version": current.version,
                    "effective_at": _iso(current.effective_at),
                    "handover_policy": current.handover_policy,
                    "handover_policy_label": HANDOVER_POLICY_LABELS[current.handover_policy],
                    "handover_policies": [
                        {"value": key, "label": label}
                        for key, label in HANDOVER_POLICY_LABELS.items()
                    ],
                    "priority": list(EVALUATION_PRIORITY),
                },
                "windows": {
                    table: self.window_snapshot(table, moment)
                    for table in sorted({
                        app.fact_table for app in self._applications.values()
                    } | set(self._window_events))
                },
                "applications": apps,
                "pending_applications": [
                    preview["application_id"] for preview in pending_previews
                ],
                "previews": pending_previews,
                "pending_count": len(pending_previews),
                "capacity": {
                    "version": current.version,
                    "used": self._seats_used(),
                    "limit": current.cap,
                    "remaining": max(current.cap - self._seats_used(), 0),
                },
                "receipts": [r.to_dict() for r in self._receipts.values()],
                "todos": [
                    row for row in store.rows("load_window")
                    if row.get("放行申请")
                ],
                "evaluation_priority": list(EVALUATION_PRIORITY),
                "next_change_at": _iso(self.next_change_at(moment)),
                "window_events": {
                    table: [
                        {
                            "state": event["state"],
                            "state_label": "开启" if event["state"] == "open" else "关闭",
                            "reason": event["reason"],
                            "by": event["by"],
                            "changed_at": _iso(_parse_dt(event["at"])),
                        }
                        for event in events
                    ]
                    for table, events in self._window_events.items()
                },
                "version_history": [
                    v.to_dict(moment) for v in sorted(
                        self._versions, key=lambda v: v.effective_at)
                ],
            }


release_control = ReleaseControlService()
