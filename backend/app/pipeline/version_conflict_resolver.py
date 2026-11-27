"""版本冲突解决器：转写版本冲突与引用保护。

核心行为：
- 已被字幕包引用的版本不得静默覆盖
- 人工改标 overrides 机器标注，留改标次数
- 复核结论是版本生效的前置条件
- 纠错合入后版本号递增并记录父版本
"""
from __future__ import annotations

import hashlib
from typing import Any


class VersionEntry:
    """版本条目：记录版本号、父版本和引用状态。"""

    def __init__(
        self, version_id: str, task_id: str, version_no: int,
        parent_version_no: int | None,
        content: str | None = None,
        correction_id: str | None = None,
        parent_content: str | None = None,
    ):
        self.version_id = version_id
        self.task_id = task_id
        self.version_no = version_no
        self.parent_version_no = parent_version_no
        self.content = content
        self.correction_id = correction_id
        self.parent_content = parent_content
        self.effective = False
        self.ref_count = 0
        self.confidence_review_passed = False
        self.overridden = False
        self.checksum = hashlib.md5((content or "").encode()).hexdigest()[:8] if content is not None else ""

    def increment_ref(self) -> None:
        self.ref_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "task_id": self.task_id,
            "version_no": self.version_no,
            "parent_version_no": self.parent_version_no,
            "content": self.content,
            "correction_id": self.correction_id,
            "parent_content": self.parent_content,
            "checksum": self.checksum,
            "effective": self.effective,
            "ref_count": self.ref_count,
            "confidence_review_passed": self.confidence_review_passed,
            "overridden": self.overridden,
        }


class SpeakerLabelChange:
    """说话人标注变更记录。"""

    def __init__(
        self, segment_id: str, original_label: str,
        new_label: str, source: str, change_count: int,
    ):
        self.segment_id = segment_id
        self.original_label = original_label
        self.new_label = new_label
        self.source = source
        self.change_count = change_count


class VersionConflictResolver:
    """版本冲突解决器。"""

    def __init__(self) -> None:
        self._versions: dict[str, list[VersionEntry]] = {}
        self._label_changes: dict[str, list[SpeakerLabelChange]] = {}
        self._review_results: dict[str, bool] = {}

    def register_version(self, entry: VersionEntry) -> None:
        self._versions.setdefault(entry.task_id, []).append(entry)

    def try_override(
        self, task_id: str, new_version_id: str, new_version_no: int,
    ) -> tuple[bool, str, VersionEntry | None]:
        """尝试覆盖已有版本：被引用的版本不可静默覆盖。"""
        versions = self._versions.get(task_id, [])
        for v in versions:
            if v.version_no == new_version_no:
                if v.ref_count > 0:
                    return False, (
                        f"版本 {v.version_id} 已被 {v.ref_count} 个字幕包引用，"
                        f"不可覆盖，请新建版本号"
                    ), None
                if v.version_id == new_version_id:
                    # 同一修订的超时重报/两端续办：幂等返回已有修订，不再追加版本。
                    return True, f"版本 {new_version_id} 已存在，返回同一修订", v
                v.overridden = True
                break

        entry = VersionEntry(new_version_id, task_id, new_version_no, new_version_no - 1 if new_version_no > 1 else None)
        self.register_version(entry)
        return True, f"版本 {new_version_id} 已创建", entry

    def set_effective(
        self, task_id: str, version_no: int,
    ) -> tuple[bool, str]:
        """设置版本为生效状态：复核结论必须先通过。"""
        versions = self._versions.get(task_id, [])
        for v in versions:
            if v.version_no == version_no:
                if not v.confidence_review_passed:
                    return False, (
                        f"版本 {v.version_id} 置信复核未通过，不可设置为生效"
                    )
                v.effective = True
                return True, f"版本 {v.version_id} 已生效"
        return False, f"任务 {task_id} 版本 {version_no} 不存在"

    def apply_speaker_label(
        self, segment_id: str, new_label: str, source: str,
    ) -> tuple[bool, str, SpeakerLabelChange | None]:
        """应用说话人标注：人工改标覆盖机器标注。"""
        existing = self._label_changes.get(segment_id, [])
        prev_count = len(existing)
        if source == "人工":
            change = SpeakerLabelChange(
                segment_id=segment_id,
                original_label=existing[-1].new_label if existing else "机器标注",
                new_label=new_label,
                source=source,
                change_count=prev_count + 1,
            )
            existing.append(change)
            self._label_changes[segment_id] = existing
            return True, f"人工改标已记录，改标次数 {change.change_count}", change
        else:
            change = SpeakerLabelChange(
                segment_id=segment_id,
                original_label="",
                new_label=new_label,
                source=source,
                change_count=prev_count + 1,
            )
            existing.append(change)
            self._label_changes[segment_id] = existing
            return True, f"机器标注已记录", change

    def set_review_result(self, task_id: str, version_no: int, passed: bool) -> tuple[bool, str]:
        """设置复核结论。"""
        key = f"{task_id}::v{version_no}"
        self._review_results[key] = passed
        versions = self._versions.get(task_id, [])
        for v in versions:
            if v.version_no == version_no:
                v.confidence_review_passed = passed
                if passed:
                    return True, f"版本 {v.version_id} 复核通过"
                return True, f"版本 {v.version_id} 复核未通过"
        return False, f"任务 {task_id} 版本 {version_no} 不存在"

    def snapshot(self) -> dict[str, Any]:
        return {
            "versions": {
                tid: [v.to_dict() for v in vers]
                for tid, vers in self._versions.items()
            },
            "label_changes_count": sum(
                len(changes) for changes in self._label_changes.values()
            ),
        }