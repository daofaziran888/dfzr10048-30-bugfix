"""字幕包导出领域服务：按生效转写版本导出字幕包并记录绑定版本，产出字幕包数据集供交付回执与归档架位消费。"""
from __future__ import annotations

from typing import Any
import hashlib

from app.services.base import BaseModuleService
from app.services.revision_delivery import workflow
from app.store import store


class SubtitlePackService(BaseModuleService):
    MODULE = "subtitle_pack"
    ENTITY = "字幕包"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["字幕包编号", "关联媒体源", "绑定版本", "字幕行数", "覆盖时长秒", "导出格式", "下载次数", "生成时间"]
    REQUIRED_FIELDS = ["字幕包编号", "关联媒体源", "绑定版本"]
    STATUS_ORDER = ["待生成", "生成中", "可下载", "已失效"]
    ACTION_RULES = {"启动生成": "生成中", "标记可下载": "可下载", "强制失效": "已失效"}
    NEGATIVE_ACTIONS = []

    def create_pack(
        self, pack_id: str, media_id: str, version_id: str,
        line_count: int, duration: int, export_format: str,
        content: str | None = None, **kwargs: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        """创建字幕包并绑定版本。"""
        rows = store.rows(self.MODULE)
        linked_revision = workflow.resolve_revision(version_id)
        if content is not None:
            body = content
        elif linked_revision is not None:
            body = linked_revision["content"]
        else:
            body = f"PACK-CONTENT-{pack_id}-{version_id}"
        checksum = hashlib.md5(body.encode()).hexdigest()[:8]
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "字幕包编号": pack_id, "关联媒体源": media_id,
            "绑定版本": version_id, "字幕行数": str(line_count),
            "覆盖时长秒": str(duration), "导出格式": export_format,
            "下载次数": "0", "生成时间": "2026-11-01",
            "status": "可下载", "pending": False, "abnormal": False,
            "_checksum": checksum,
            "checksum": checksum,
            "content": body,
            "正文": body,
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
        entry.update(kwargs.pop("entry_extra", {}) if isinstance(kwargs.get("entry_extra"), dict) else {})
        rows.append(entry)

        revision = linked_revision
        if revision is not None:
            pack = workflow._make_pack_record(
                pack_id=pack_id,
                revision=revision,
                content=body if content is not None else revision["content"],
                export_format=export_format,
                confirm=bool(kwargs.get("confirm", True)),
                store_id=entry["id"],
            )
            workflow._index_pack(pack)
            entry["_revision_id"] = revision["revision_id"]
            entry["_pack_record_id"] = pack["pack_id"]
            entry["file_path"] = pack["file_path"]
        elif content is not None:
            path = workflow._artifact_path(pack_id, export_format)
            artifact = workflow._write_artifact(path, body)
            entry["file_path"] = artifact["file_path"]
        return entry, f"字幕包 {pack_id} 已创建，绑定 {version_id}"

    def get_packs_by_version(self, version_id: str) -> list[dict[str, Any]]:
        """获取绑定到指定版本的所有字幕包。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("绑定版本") == version_id]

    def get_packs_by_media(self, media_id: str) -> list[dict[str, Any]]:
        """获取指定媒体源的所有字幕包。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("关联媒体源") == media_id]

    def mark_invalid(self, pack_id: str) -> tuple[bool, str]:
        """标记字幕包为失效（版本被覆盖后）。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("字幕包编号") == pack_id:
                r["status"] = "已失效"
                r["abnormal"] = True
                pack = workflow.resolve_pack(pack_id)
                if pack is not None:
                    pack["status"] = "已失效"
                    pack["claimable"] = False
                return True, f"字幕包 {pack_id} 已标记失效"
        return False, f"字幕包 {pack_id} 不存在"

    def increment_download(self, pack_id: str) -> tuple[bool, int]:
        """增加下载次数。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("字幕包编号") == pack_id:
                count = int(r.get("下载次数", "0")) + 1
                r["下载次数"] = str(count)
                return True, count
        return False, 0

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

        pack_ref = context.get("pack_id") or row.get("字幕包编号") or entry_id
        context.setdefault("pack_id", pack_ref)

        if action in {"继续生成", "continue", "continue_generation", "从字幕包页续办"}:
            context.setdefault("continued_from", "subtitle_pack")
            result = workflow.continue_work(context)
            entry = store.find(self.MODULE, entry_id) or row
            if result.get("pack"):
                entry["workflow"] = result
                entry["revision"] = result.get("revision")
                entry["recompute"] = result.get("recompute")
            return entry, str(result.get("message") or "字幕包已继续生成")

        if action in {"核验确认", "确认核验", "确认归属", "confirm", "verify", "核验文件"}:
            result = workflow.confirm_pack(pack_ref, context)
            entry = store.find(self.MODULE, entry_id) or row
            if not result.get("ok"):
                return None, str(result.get("message") or "字幕包核验未通过")
            entry["workflow"] = result
            return entry, str(result.get("message"))

        if action in {"领取", "claim"}:
            result = workflow.claim_pack(pack_ref, context)
            entry = store.find(self.MODULE, entry_id) or row
            entry["workflow"] = result
            if not result.get("ok"):
                return None, str(result.get("message") or "暂不可领取")
            return entry, str(result.get("message"))

        if action in {"确认并领取", "confirm_and_claim"}:
            context = {**context, "confirm": True}
            result = workflow.claim_pack(pack_ref, context)
            entry = store.find(self.MODULE, entry_id) or row
            entry["workflow"] = result
            if not result.get("ok"):
                return None, str(result.get("message") or "暂不可领取")
            return entry, str(result.get("message"))

        if action == "标记可下载":
            linked = workflow.resolve_pack(pack_ref)
            if linked is not None:
                result = workflow.confirm_pack(pack_ref, context)
                if not result.get("ok"):
                    entry = store.find(self.MODULE, entry_id) or row
                    return None, str(result.get("message") or "文件未确认属于本修订，不能标记可下载")
            entry, message = super().run_action(entry_id, action, context)
            if entry is not None and entry.get("status") == "可下载":
                entry["pending"] = False
            return entry, message

        entry, message = super().run_action(entry_id, action, context)
        if action == "强制失效" and entry is not None:
            pack = workflow.resolve_pack(pack_ref)
            if pack is not None:
                pack["status"] = "已失效"
                pack["claimable"] = False
        return entry, message

    # ------------------------------------------------------------------
    # 语义化兼容方法
    # ------------------------------------------------------------------
    def generate_pack(self, pack_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        return workflow.generate_pack(pack_ref, kwargs)

    def assemble_pack(self, pack_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.generate_pack(pack_ref, **kwargs)

    def continue_generation(self, pack_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("continued_from", "subtitle_pack")
        return workflow.continue_work({"pack_id": pack_ref, **kwargs})

    continue_from_pack = continue_generation

    def confirm_pack(self, pack_ref: Any = None, confirmer: Any = None, **kwargs: Any) -> dict[str, Any]:
        if confirmer is not None:
            kwargs.setdefault("confirmer", confirmer)
        return workflow.confirm_pack(pack_ref, kwargs)

    def verify_pack(self, pack_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        return workflow.verify_pack(pack_ref, **kwargs)

    def claim_pack(self, pack_ref: Any = None, claimer: Any = None, **kwargs: Any) -> dict[str, Any]:
        if claimer is not None:
            kwargs.setdefault("claimer", claimer)
        return workflow.claim_pack(pack_ref, kwargs)

    def download_pack(self, pack_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        return workflow.download(pack_ref, **kwargs)

    def get_revision(self, pack_ref: Any) -> dict[str, Any] | None:
        pack = workflow.resolve_pack(pack_ref)
        if pack is None:
            return None
        return workflow.revisions.get(pack.get("revision_id", ""))

    def get_recompute(self, pack_ref: Any) -> dict[str, Any] | None:
        revision = self.get_revision(pack_ref)
        return None if revision is None else revision.get("recompute")

    def get_trace(self, pack_ref: Any) -> dict[str, Any]:
        revision = self.get_revision(pack_ref)
        if revision is None:
            return {"ok": False, "message": f"字幕包 {pack_ref} 尚未关联修订"}
        return workflow.get_revision_view(revision["revision_id"], source="subtitle_pack")
