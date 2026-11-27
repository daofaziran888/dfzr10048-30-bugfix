"""字幕包装配器：按生效版本组装并绑定版本引用。

核心行为：
- 按生效转写版本导出字幕包
- 字幕包绑定版本号，保证可追溯
- 被引用版本不得静默覆盖
- 纠错合入触发字幕包重算
"""
from __future__ import annotations

from typing import Any
import hashlib


class SubtitlePackEntry:
    """字幕包条目：记录版本绑定与内容校验。"""

    def __init__(
        self, pack_id: str, media_id: str, bound_version: str,
        line_count: int, duration_seconds: float, export_format: str,
        content: str = "",
        revision_id: str | None = None,
        correction_id: str | None = None,
        parent_version: str | None = None,
    ):
        self.pack_id = pack_id
        self.media_id = media_id
        self.bound_version = bound_version
        self.line_count = line_count
        self.duration_seconds = duration_seconds
        self.export_format = export_format
        self.content = content
        self.revision_id = revision_id or bound_version
        self.correction_id = correction_id
        self.parent_version = parent_version
        self.checksum = ""
        self.available = False
        self.recompute_needed = False
        self.confirmation_status = "pending"
        self.claimed = False
        self.continued_from: list[str] = []
        self.completion_basis: dict[str, Any] | None = None

    def compute_checksum(self, content: str) -> str:
        """计算字幕内容校验和。"""
        self.content = content
        self.checksum = hashlib.md5(content.encode()).hexdigest()[:8]
        return self.checksum

    def verify(self, expected_content: str | None = None, expected_checksum: str | None = None) -> bool:
        """核验当前文件正文/校验和是否确属绑定修订。"""
        content = self.content if expected_content is None else expected_content
        actual = hashlib.md5(content.encode()).hexdigest()[:8]
        if expected_checksum is not None and actual != expected_checksum:
            return False
        if expected_content is not None and expected_content != self.content:
            # 允许核验调用直接传入磁盘读到的内容；通过后回写为当前正文。
            self.content = expected_content
        return actual == self.checksum if expected_checksum is None else actual == expected_checksum

    def confirm(self, expected_content: str | None = None) -> bool:
        if not self.verify(expected_content):
            self.confirmation_status = "mismatch"
            self.available = False
            return False
        self.confirmation_status = "confirmed"
        self.available = True
        self.completion_basis = {
            "pack_id": self.pack_id,
            "revision_id": self.revision_id,
            "bound_version": self.bound_version,
            "correction_id": self.correction_id,
            "checksum": self.checksum,
        }
        return True

    def claim(self, receiver: str | None = None) -> tuple[bool, str, dict[str, Any]]:
        if self.confirmation_status != "confirmed" or not self.available:
            return False, "字幕包仍待确认或核验不一致，暂不可领取", self.to_dict()
        self.claimed = True
        return True, f"字幕包 {self.pack_id} 可由 {receiver or '接收人'} 领取", self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "media_id": self.media_id,
            "bound_version": self.bound_version,
            "revision_id": self.revision_id,
            "correction_id": self.correction_id,
            "parent_version": self.parent_version,
            "line_count": self.line_count,
            "duration_seconds": self.duration_seconds,
            "export_format": self.export_format,
            "content": self.content,
            "checksum": self.checksum,
            "available": self.available,
            "recompute_needed": self.recompute_needed,
            "confirmation_status": self.confirmation_status,
            "claimed": self.claimed,
            "continued_from": self.continued_from,
            "completion_basis": self.completion_basis,
        }


class SubtitlePackAssembler:
    """字幕包装配器。"""

    def __init__(self) -> None:
        self._packs: list[SubtitlePackEntry] = []
        self._version_pack_map: dict[str, list[str]] = {}

    def create_pack(
        self, pack_id: str, media_id: str, version_id: str,
        line_count: int, duration_seconds: float, export_format: str,
        content: str,
        revision_id: str | None = None,
        correction_id: str | None = None,
        parent_version: str | None = None,
        auto_confirm: bool = False,
    ) -> SubtitlePackEntry:
        """创建字幕包并绑定版本与正文校验值。"""
        entry = SubtitlePackEntry(
            pack_id=pack_id, media_id=media_id,
            bound_version=version_id, line_count=line_count,
            duration_seconds=duration_seconds, export_format=export_format,
            content=content, revision_id=revision_id or version_id,
            correction_id=correction_id, parent_version=parent_version,
        )
        entry.compute_checksum(content)
        entry.available = True
        if auto_confirm:
            entry.confirm(content)
        self._packs.append(entry)
        self._version_pack_map.setdefault(version_id, []).append(pack_id)
        if revision_id and revision_id != version_id:
            self._version_pack_map.setdefault(revision_id, []).append(pack_id)
        return entry

    def has_reference(self, version_id: str) -> bool:
        """检查版本是否被字幕包引用。"""
        return version_id in self._version_pack_map and len(self._version_pack_map[version_id]) > 0

    def get_ref_count(self, version_id: str) -> int:
        """获取版本的引用数量。"""
        return len(self._version_pack_map.get(version_id, []))

    def mark_recompute(self, version_id: str, content: str | None = None) -> list[str]:
        """标记需要重算的字幕包（纠错合入后）。

        传入重算正文时同步刷新该版本下所有包的正文与 checksum；不传正文时
        保持旧行为，只把包标记为不可用，等待装包入口用修订正文续办。
        """
        pack_ids = self._version_pack_map.get(version_id, [])
        for entry in self._packs:
            if entry.pack_id in pack_ids:
                entry.recompute_needed = True
                entry.available = False
                entry.confirmation_status = "pending"
                if content is not None:
                    entry.compute_checksum(content)
                    entry.recompute_needed = False
        return pack_ids

    def continue_from(
        self, pack_id: str, source: str, content: str | None = None,
    ) -> SubtitlePackEntry | None:
        """记录纠错端或字幕包端的同一次续办，可选地补齐正文。"""
        entry = next((p for p in self._packs if p.pack_id == pack_id), None)
        if entry is None:
            return None
        if source not in entry.continued_from:
            entry.continued_from.append(source)
        if content is not None:
            entry.compute_checksum(content)
            entry.recompute_needed = False
        return entry

    def claim(self, pack_id: str, receiver: str | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        entry = next((p for p in self._packs if p.pack_id == pack_id), None)
        if entry is None:
            return False, f"字幕包 {pack_id} 不存在", None
        ok, message, data = entry.claim(receiver)
        return ok, message, data
    def get_packs_by_version(self, version_id: str) -> list[SubtitlePackEntry]:
        """获取绑定到指定版本的全部字幕包。"""
        pack_ids = self._version_pack_map.get(version_id, [])
        return [p for p in self._packs if p.pack_id in pack_ids]

    def snapshot(self) -> dict[str, Any]:
        return {
            "packs": [p.to_dict() for p in self._packs],
            "total_packs": len(self._packs),
            "available_count": sum(1 for p in self._packs if p.available),
        }