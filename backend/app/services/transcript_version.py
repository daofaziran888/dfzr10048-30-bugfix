"""转写版本落库领域服务：把转写结果冻结成带父版本号的版本记录，产出版本记录数据集供字幕包引用与纠错回路消费。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.revision_delivery import (
    md5_short,
    sha256_full,
    workflow,
)
from app.store import store


class TranscriptVersionService(BaseModuleService):
    MODULE = "transcript_version"
    ENTITY = "版本记录"
    OPERATION_TERM = "落库"
    LIST_FIELDS = ["版本编号", "关联任务", "版本号", "父版本号", "变更行数", "变更摘要", "生效时间", "被引用次数"]
    REQUIRED_FIELDS = ["版本编号", "关联任务", "版本号"]
    STATUS_ORDER = ["草稿", "过渡中", "已生效", "已废止"]
    ACTION_RULES = {"启动过渡": "过渡中", "生效版本": "已生效", "废止版本": "已废止"}
    NEGATIVE_ACTIONS = ["废止版本"]

    def create_version(
        self, version_id: str, task_id: str, version_no: str,
        parent: str, change_lines: int, summary: str,
        content: str | None = None, **kwargs: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        """创建新转写版本。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("版本编号") == version_id:
                return None, f"版本编号 {version_id} 已存在"
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "版本编号": version_id, "关联任务": task_id,
            "版本号": version_no, "父版本号": parent,
            "变更行数": str(change_lines), "变更摘要": summary,
            "生效时间": "-", "被引用次数": "0",
            "status": "草稿", "pending": True, "abnormal": False,
        }
        if content is not None:
            entry.update({
                "正文": content,
                "content": content,
                "更正正文": content,
                "checksum": md5_short(content),
                "sha256": sha256_full(content),
            })
        rows.append(entry)
        if content is not None:
            code = workflow.normalize_version_code(version_no) or version_no
            revision_id = str(kwargs.get("revision_id") or f"REV-{version_id}")
            revision = workflow._make_revision_record(
                revision_id=revision_id,
                version_pk=version_id,
                version_code=code,
                task_id=task_id,
                media_id=str(kwargs.get("media_id") or "MEDI-0001"),
                content=content,
                parent_revision_id=workflow.version_index.get(str(parent)),
                correction_ids=list(kwargs.get("correction_ids") or []),
                source=str(kwargs.get("source") or "version_create"),
                summary=summary,
            )
            workflow._index_revision(revision)
            entry["_revision_id"] = revision_id
        return entry, f"版本 {version_id} 已创建"

    def get_effective_version(self, task_id: str) -> dict[str, Any] | None:
        """获取指定任务的当前生效版本。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("关联任务") == task_id and r.get("status") == "已生效":
                return r
        return None

    def is_referenced(self, version_id: str) -> bool:
        """检查版本是否被字幕包引用。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("版本编号") == version_id:
                return int(r.get("被引用次数", "0")) > 0
        return False

    def increment_ref_count(self, version_id: str) -> tuple[bool, str]:
        """增加版本的被引用次数。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("版本编号") == version_id:
                current = int(r.get("被引用次数", "0"))
                r["被引用次数"] = str(current + 1)
                return True, f"版本 {version_id} 引用次数: {current + 1}"
        return False, f"版本 {version_id} 不存在"

    def get_versions_by_task(self, task_id: str) -> list[dict[str, Any]]:
        """获取指定任务的全部版本记录，按版本号排序。"""
        rows = store.rows(self.MODULE)
        vers = [r for r in rows if r.get("关联任务") == task_id]
        vers.sort(key=lambda r: r.get("版本号", "v0"))
        return vers

    def get_version_detail(self, version_ref: Any) -> dict[str, Any] | None:
        """返回带更正正文、重算结果和来源的版本详情。"""
        revision = workflow.resolve_revision(version_ref)
        row = workflow._find_store_row(self.MODULE, version_ref)
        if revision is None and row is None:
            return None
        detail = dict(row or {})
        if revision is not None:
            detail["revision"] = revision
            detail["recompute"] = revision.get("recompute")
            detail["completion_basis"] = revision.get("completion_basis")
            detail["来源"] = revision.get("来源")
            for key in ("正文", "content", "更正正文", "checksum", "sha256", "修订编号", "重算编号"):
                detail.setdefault(key, revision.get(key))
        return detail

    def get_revision(self, version_ref: Any) -> dict[str, Any] | None:
        return workflow.resolve_revision(version_ref)

    def get_content(self, version_ref: Any) -> str | None:
        revision = workflow.resolve_revision(version_ref)
        if revision is not None:
            return str(revision.get("content"))
        row = workflow._find_store_row(self.MODULE, version_ref)
        value = (row or {}).get("正文") or (row or {}).get("content")
        return None if value is None else str(value)

    def download_version(self, version_ref: Any) -> dict[str, Any]:
        return workflow.download(version_ref)

    def verify_version(self, version_ref: Any, content: str | None = None) -> dict[str, Any]:
        revision = workflow.resolve_revision(version_ref)
        if revision is None:
            return {"ok": False, "verified": False, "message": f"版本 {version_ref} 不存在"}
        body = revision["content"] if content is None else content
        verification = workflow.verify_content_against_revision(revision, body)
        return {"ok": verification["verified"], **verification, "revision": revision}
