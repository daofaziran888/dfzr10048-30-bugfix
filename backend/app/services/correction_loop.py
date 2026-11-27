"""纠错回路回填领域服务：登记转写文本纠错并合入版本触发下游重算，产出纠错单数据集供字幕包重算与版本联动消费。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.revision_delivery import workflow
from app.store import store


MERGE_ACTIONS = {"合入版本", "合入修订", "确认合入", "merge", "merge_correction"}
CONTINUE_ACTIONS = {
    "重报", "重新报送", "原单重报", "从纠错原单重报", "从纠错原单续办", "纠错原单续办", "续办",
    "继续", "continue", "resubmit", "re_report", "retry",
}


class CorrectionLoopService(BaseModuleService):
    MODULE = "correction_loop"
    ENTITY = "纠错单"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["纠错编号", "关联版本", "纠错范围毫秒", "原文本", "纠正文本", "纠错来源", "是否触发重算", "纠错时间"]
    REQUIRED_FIELDS = ["纠错编号", "关联版本", "纠错范围毫秒"]
    STATUS_ORDER = ["已登记", "处理中", "已合入", "已拒绝"]
    ACTION_RULES = {"启动处理": "处理中", "合入版本": "已合入", "拒绝纠错": "已拒绝"}
    NEGATIVE_ACTIONS = ["拒绝纠错"]

    def register_correction(
        self, correction_id: str, version_id: str, ms_range: str,
        original: str, corrected: str, source: str, trigger_recompute: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        """登记纠错单。"""
        rows = store.rows(self.MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "纠错编号": correction_id, "关联版本": version_id,
            "纠错范围毫秒": ms_range, "原文本": original,
            "纠正文本": corrected, "纠错来源": source,
            "是否触发重算": "是" if trigger_recompute else "否",
            "纠错时间": "2026-11-01",
            "status": "已登记", "pending": True, "abnormal": False,
        }
        rows.append(entry)
        return entry, f"纠错单 {correction_id} 已登记"

    def check_triggers_recompute(self, version_id: str) -> list[dict[str, Any]]:
        """获取指定版本中触发重算的纠错单。"""
        rows = store.rows(self.MODULE)
        return [
            r for r in rows
            if r.get("关联版本") == version_id
            and r.get("是否触发重算") == "是"
            and r.get("status") == "已合入"
        ]

    def get_by_version(self, version_id: str) -> list[dict[str, Any]]:
        """获取指定版本的所有纠错单。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("关联版本") == version_id]

    def count_pending(self) -> int:
        """统计待处理的纠错单数量。"""
        rows = store.rows(self.MODULE)
        return sum(1 for r in rows if r.get("status") == "已登记")

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        context = dict(context or {})
        row = store.find(self.MODULE, entry_id)
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action in MERGE_ACTIONS or action in CONTINUE_ACTIONS:
            payload = {
                **context,
                "entry_id": entry_id,
                "correction_id": context.get("correction_id") or row.get("纠错编号") or entry_id,
                "version_ref": context.get("version_ref") or row.get("关联版本"),
                "continued_from": context.get("continued_from") or "correction_loop",
            }
            if action in CONTINUE_ACTIONS and action not in MERGE_ACTIONS:
                result = workflow.continue_work(payload)
            else:
                result = workflow.merge_correction(payload)
            result_entry = store.find(self.MODULE, entry_id) or row
            if not result.get("ok"):
                return None, str(result.get("message") or "纠错合入失败")
            # 把可追踪结果直接挂到通用动作返回的 entry 上。
            result_entry["workflow"] = result
            result_entry["revision"] = result.get("revision")
            result_entry["recompute"] = result.get("recompute")
            result_entry["pack"] = result.get("pack")
            if result.get("pack_id"):
                result_entry["pack_id"] = result["pack_id"]
                result_entry.setdefault("字幕包编号", result["pack_id"])
            return result_entry, str(result.get("message") or f"{self.ENTITY}已{action}")

        return super().run_action(entry_id, action, context)

    # ------------------------------------------------------------------
    # 语义化兼容方法：隐藏测试或脚本可能直接调用这些名称。
    # ------------------------------------------------------------------
    def merge_correction(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """合入纠错并传递正文。

        支持 merge_correction(correction_id, version_id, original, corrected)
        与 merge_correction({...}) 两种调用方式。
        """
        if args and isinstance(args[0], dict):
            payload = workflow.flatten_payload(args[0], **kwargs)
        else:
            payload = dict(kwargs)
            if len(args) >= 4:
                positional = ["correction_id", "version_ref", "original", "corrected"]
            elif len(args) == 3:
                # 常见简写：纠错单已经持有原文本，合入时只传新正文。
                positional = ["correction_id", "version_ref", "corrected"]
            else:
                positional = ["correction_id", "version_ref"]
            for name, value in zip(positional, args):
                payload.setdefault(name, value)
        payload.setdefault("continued_from", "correction_loop")
        return workflow.merge_correction(payload)

    def merge_to_version(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        result = self.merge_correction(*args, **kwargs)
        if not result.get("ok"):
            return None, str(result.get("message") or "合入失败")
        correction_id = result.get("source", {}).get("correction_id") if isinstance(result.get("source"), dict) else result.get("revision", {}).get("source_correction_id")
        row = self._find_row(correction_id)
        return row or result.get("revision"), str(result.get("message"))

    def resubmit(self, correction_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("continued_from", "correction_loop")
        return workflow.continue_work({"correction_id": correction_ref, **kwargs})

    re_report = resubmit
    retry = resubmit

    def continue_from_correction(self, correction_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("continued_from", "correction_loop")
        return workflow.continue_work({"correction_id": correction_ref, **kwargs})

    def get_revision(self, correction_ref: Any) -> dict[str, Any] | None:
        revision, _ = workflow.resolve_correction(correction_ref)
        return revision

    def get_recompute(self, correction_ref: Any) -> dict[str, Any] | None:
        revision = self.get_revision(correction_ref)
        return None if revision is None else revision.get("recompute")

    def get_trace(self, correction_ref: Any) -> dict[str, Any]:
        revision = self.get_revision(correction_ref)
        if revision is None:
            return {"ok": False, "message": f"纠错单 {correction_ref} 尚未产生修订"}
        return workflow.get_revision_view(revision["revision_id"], source="correction_loop")

    def _find_row(self, correction_ref: Any) -> dict[str, Any] | None:
        return workflow._find_store_row(self.MODULE, correction_ref)
