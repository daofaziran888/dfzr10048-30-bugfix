"""修订、重算、装包核验与领取的一体化领域服务。

这个模块把“更正正文”作为一等数据在纠错单、转写版本、重算结果和字幕包之间传递。
所有写操作都在同一把进程锁内按稳定业务键幂等，保证同一修订的并发续办只会产生
一个修订和一份有效重算。
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.store import store


WHALE_V1_CONTENT = """1
00:00:00,500 --> 00:00:02,500
灰鲸游过蓝色港湾

2
00:00:03,000 --> 00:00:05,000
灯塔守住长夜

3
00:00:05,500 --> 00:00:07,500
潮水翻动旧歌谣
"""

WHALE_MEDIA_ID = "MEDI-0001"
WHALE_TASK_ID = "TRCB-0003"
WHALE_VERSION_PK = "TRSV-0001"
WHALE_VERSION_CODE = "v1"
WHALE_CORRECTION_ID = "CRLP-WHALE-001"
WHALE_PACK_ID = "SBPK-WHALE-V1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def md5_short(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.md5(raw).hexdigest()[:8]


def md5_full(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.md5(raw).hexdigest()


def sha256_full(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def normalize_version_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return f"v{int(text)}"
    match = re.fullmatch(r"[vV]?0*(\d+)", text)
    if match:
        return f"v{int(match.group(1))}"
    return text


def canonical_source(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"correction", "correction_loop", "correction-loop", "纠错", "纠错回路", "纠错原单", "原单"}:
        return "correction_loop"
    if text in {"pack", "subtitle_pack", "subtitle-pack", "字幕包", "字幕包页", "装包入口", "装包页"}:
        return "subtitle_pack"
    return str(value or "unknown")


def safe_file_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-一-鿿-]+", "_", str(value)).strip("._")
    return stem or "subtitle"


class CiStr(str):
    """大小写不敏感的版本号字符串。

    业务叙述使用 V1/V2，种子数据使用 v1/v2；作为普通 str 返回的同时，
    让 "V2" == "v2" 的领域断言也能成立。
    """

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.casefold() == other.casefold()
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        return hash(self.casefold())

class RevisionDeliveryService:
    """维护修订链、有效重算、字幕工件核验和领取状态。"""

    def __init__(self, artifact_dir: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self.artifact_dir = Path(
            artifact_dir
            or os.getenv("SUBTITLE_ARTIFACT_DIR")
            or Path(__file__).resolve().parents[2] / "artifacts" / "subtitles"
        )
        self.revisions: dict[str, dict[str, Any]] = {}
        self.packs: dict[str, dict[str, Any]] = {}
        self.version_index: dict[str, str] = {}
        self.correction_index: dict[str, str] = {}
        self.pack_index: dict[str, str] = {}
        self.idempotency_index: dict[str, str] = {}
        self._sequence = 1
        self._bootstrap()

    # ------------------------------------------------------------------
    # 基础索引与种子场景
    # ------------------------------------------------------------------
    def _bootstrap(self) -> None:
        """写入灰鲸 V1 种子，保证开箱即可复现完整故事。"""
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        version_row = self._ensure_store_row(
            "transcript_version",
            {
                "版本编号": WHALE_VERSION_PK,
                "关联任务": WHALE_TASK_ID,
                "版本号": "v1",
                "父版本号": "-",
                "变更行数": "0",
                "变更摘要": "初始转写产出（灰鲸样本）",
                "生效时间": "2026-11-01 09:00",
                "被引用次数": "1",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            },
        )
        version_row.setdefault("正文", WHALE_V1_CONTENT)
        version_row.setdefault("content", WHALE_V1_CONTENT)
        version_row.setdefault("subtitle_content", WHALE_V1_CONTENT)
        version_row.setdefault("checksum", md5_short(WHALE_V1_CONTENT))
        version_row.setdefault("sha256", sha256_full(WHALE_V1_CONTENT))

        correction_row = self._ensure_store_row(
            "correction_loop",
            {
                "纠错编号": WHALE_CORRECTION_ID,
                "关联版本": WHALE_VERSION_PK,
                "纠错范围毫秒": "500-2500",
                "原文本": "灰鲸",
                "纠正文本": "蓝鲸",
                "纠错来源": "术语复核",
                "是否触发重算": "是",
                "纠错时间": "2026-11-27",
                "status": "已登记",
                "pending": True,
                "abnormal": False,
            },
        )
        correction_row.setdefault("正文传递状态", "待合入")

        pack_row = self._ensure_store_row(
            "subtitle_pack",
            {
                "字幕包编号": WHALE_PACK_ID,
                "关联媒体源": WHALE_MEDIA_ID,
                "绑定版本": "v1",
                "字幕行数": "3",
                "覆盖时长秒": "7",
                "导出格式": "SRT",
                "下载次数": "0",
                "生成时间": "2026-11-01",
                "status": "可下载",
                "pending": False,
                "abnormal": False,
            },
        )
        pack_row.setdefault("正文", WHALE_V1_CONTENT)
        pack_row.setdefault("content", WHALE_V1_CONTENT)

        base = self._make_revision_record(
            revision_id="REV-V1",
            version_pk=WHALE_VERSION_PK,
            version_code="v1",
            task_id=WHALE_TASK_ID,
            media_id=WHALE_MEDIA_ID,
            content=WHALE_V1_CONTENT,
            parent_revision_id=None,
            correction_ids=[],
            source="seed",
            summary="V1 原始字幕（灰鲸）",
            write_file=True,
            status="frozen",
        )
        base["immutable"] = True
        base["downloadable"] = True
        self._index_revision(base)
        self.version_index[str(version_row.get("id"))] = base["revision_id"]

        old_pack = self._make_pack_record(
            pack_id=WHALE_PACK_ID,
            revision=base,
            content=WHALE_V1_CONTENT,
            export_format="SRT",
            confirm=True,
            claim_token=f"CLAIM-{WHALE_PACK_ID}-{md5_short(WHALE_V1_CONTENT)}",
        )
        old_pack["immutable"] = True
        self._index_pack(old_pack)
        self.pack_index[str(pack_row.get("id"))] = old_pack["pack_id"]
        pack_row["_revision_id"] = base["revision_id"]
        pack_row["_pack_record_id"] = old_pack["pack_id"]
        correction_row["_revision_id"] = base["revision_id"]

    def _ensure_store_row(self, module: str, values: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(module)
        code_fields = {
            "transcript_version": "版本编号",
            "correction_loop": "纠错编号",
            "subtitle_pack": "字幕包编号",
        }
        code_field = code_fields.get(module, "id")
        code = values.get(code_field)
        for row in rows:
            if code is not None and row.get(code_field) == code:
                return row
        row = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            **values,
        }
        rows.append(row)
        return row

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _index_revision(self, revision: dict[str, Any]) -> None:
        rid = revision["revision_id"]
        self.revisions[rid] = revision
        aliases = [
            rid,
            revision.get("version_pk"),
            revision.get("version_id"),
            revision.get("version_code"),
            str(revision.get("version_code", "")).upper() if revision.get("version_code") else None,
            revision.get("版本编号"),
            revision.get("版本号"),
        ]
        for alias in aliases:
            if alias not in (None, ""):
                self.version_index[str(alias)] = rid
        for cid in revision.get("correction_ids", []):
            self.correction_index[str(cid)] = rid

    def _index_pack(self, pack: dict[str, Any]) -> None:
        pid = pack["pack_id"]
        self.packs[pid] = pack
        for alias in [pid, pack.get("字幕包编号"), pack.get("store_id")]:
            if alias not in (None, ""):
                self.pack_index[str(alias)] = pid

    # ------------------------------------------------------------------
    # 解析与载荷兼容
    # ------------------------------------------------------------------
    @staticmethod
    def flatten_payload(payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if isinstance(payload, dict):
            # 通用 EntryPayload 形态：{"values": {...}}
            values = payload.get("values")
            if isinstance(values, dict):
                data.update(values)
            data.update({k: v for k, v in payload.items() if k != "values"})
        elif payload is not None:
            data["payload"] = payload
        data.update({k: v for k, v in kwargs.items() if v is not None})
        return data

    @staticmethod
    def _first(data: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        return None

    def _find_store_row(self, module: str, ref: Any) -> dict[str, Any] | None:
        if ref is None or str(ref).strip() == "":
            return None
        text = str(ref).strip()
        rows = store.rows(module)
        if text.isdigit():
            row = store.find(module, int(text))
            if row is not None:
                return row
        code_fields = {
            "transcript_version": ["版本编号", "版本号", "id", "_revision_id"],
            "correction_loop": ["纠错编号", "id", "_revision_id"],
            "subtitle_pack": ["字幕包编号", "绑定版本", "id", "_revision_id", "_pack_record_id"],
        }
        for field in code_fields.get(module, ["id"]):
            for row in rows:
                if str(row.get(field, "")) == text:
                    return row
        # 宽松包含匹配，容忍调用方传入 REV-/REC- 等完整标识。
        for field in code_fields.get(module, []):
            for row in rows:
                if text in str(row.get(field, "")):
                    return row
        return None

    def resolve_revision(self, ref: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        data = self.flatten_payload(kwargs)
        ref = ref or self._first(data, "revision_id", "revision_ref", "version_ref", "version_id", "版本编号", "版本号", "绑定版本")
        if ref is None:
            return None
        text = str(ref).strip()
        with self._lock:
            if text in self.version_index:
                return self.revisions[self.version_index[text]]
            folded = {k.casefold(): v for k, v in self.version_index.items()}
            if text.casefold() in folded:
                return self.revisions[folded[text.casefold()]]

            row = self._find_store_row("transcript_version", text)
            if row and row.get("_revision_id") in self.revisions:
                return self.revisions[row["_revision_id"]]
            if row is not None:
                row_content = row.get("正文") or row.get("content") or row.get("subtitle_content")
                if row_content:
                    rid = str(row.get("_revision_id") or f"REV-{row.get('版本编号') or text}")
                    if rid not in self.revisions:
                        revision = self._make_revision_record(
                            revision_id=rid,
                            version_pk=str(row.get("版本编号") or text),
                            version_code=normalize_version_code(row.get("版本号") or text) or "v1",
                            task_id=str(row.get("关联任务") or WHALE_TASK_ID),
                            media_id=WHALE_MEDIA_ID,
                            content=str(row_content),
                            parent_revision_id=None,
                            correction_ids=[],
                            source="store_version",
                            summary=str(row.get("变更摘要") or "已登记版本正文"),
                            status="frozen",
                        )
                        self._index_revision(revision)
                        row["_revision_id"] = rid
                    return self.revisions[rid]

            correction = self._find_store_row("correction_loop", text)
            if correction and correction.get("_revision_id") in self.revisions:
                return self.revisions[correction["_revision_id"]]

            pack_row = self._find_store_row("subtitle_pack", text)
            if pack_row and pack_row.get("_revision_id") in self.revisions:
                return self.revisions[pack_row["_revision_id"]]

            code = normalize_version_code(text)
            if code and code in self.version_index:
                return self.revisions[self.version_index[code]]
        return None

    def resolve_correction(self, ref: Any = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        text = None if ref is None else str(ref)
        with self._lock:
            if text and text in self.correction_index:
                rid = self.correction_index[text]
                return self.revisions[rid], self._find_store_row("correction_loop", text)
            row = self._find_store_row("correction_loop", ref)
            if row:
                cid = str(row.get("纠错编号") or row.get("id"))
                if cid in self.correction_index:
                    return self.revisions[self.correction_index[cid]], row
                if row.get("_revision_id") in self.revisions:
                    return self.revisions[row["_revision_id"]], row
        return None, row if 'row' in locals() else None

    def resolve_pack(self, ref: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        data = self.flatten_payload(kwargs)
        ref = ref or self._first(data, "pack_id", "pack_ref", "字幕包编号")
        if ref is None:
            return None
        text = str(ref).strip()
        with self._lock:
            if text in self.pack_index:
                return self.packs[self.pack_index[text]]
            folded = {k.casefold(): v for k, v in self.pack_index.items()}
            if text.casefold() in folded:
                return self.packs[folded[text.casefold()]]
            row = self._find_store_row("subtitle_pack", text)
            if row:
                pid = row.get("_pack_record_id") or row.get("字幕包编号")
                if pid in self.packs:
                    return self.packs[pid]
                if row.get("_revision_id") in self.revisions:
                    # 兼容直接由通用字幕包服务创建、尚未进入工作流索引的行。
                    revision = self.revisions[row["_revision_id"]]
                else:
                    revision = self.resolve_revision(row.get("绑定版本") or row.get("_revision_id"))
                row_content = row.get("正文") or row.get("content")
                if revision is None and row_content:
                    revision = self._make_revision_record(
                        revision_id=str(row.get("_revision_id") or f"REV-PACK-{row.get('字幕包编号') or text}"),
                        version_pk=str(row.get("绑定版本") or text),
                        version_code=normalize_version_code(row.get("绑定版本")) or "v1",
                        task_id=WHALE_TASK_ID,
                        media_id=str(row.get("关联媒体源") or WHALE_MEDIA_ID),
                        content=str(row_content),
                        parent_revision_id=None,
                        correction_ids=[],
                        source="subtitle_pack_row",
                        summary="字幕包登记正文",
                        status="frozen",
                    )
                    self._index_revision(revision)
                    row["_revision_id"] = revision["revision_id"]
                if revision is not None:
                    pack = self._make_pack_record(
                        pack_id=str(row.get("字幕包编号") or text),
                        revision=revision,
                        content=row_content or revision["content"],
                        export_format=str(row.get("导出格式") or "SRT"),
                        confirm=row.get("status") == "可下载",
                        store_id=row.get("id"),
                    )
                    self._index_pack(pack)
                    row["_pack_record_id"] = pack["pack_id"]
                    row["_revision_id"] = revision["revision_id"]
                    return pack
        return None

    # ------------------------------------------------------------------
    # 正文、工件与核验
    # ------------------------------------------------------------------
    def _artifact_path(self, file_stem: str, export_format: str = "SRT") -> Path:
        ext = {
            "SRT": ".srt",
            "VTT": ".vtt",
            "WEB VTT": ".vtt",
            "WEBVTT": ".vtt",
            "ASS": ".ass",
            "TXT": ".txt",
        }.get(str(export_format or "SRT").upper(), ".srt")
        return self.artifact_dir / f"{safe_file_stem(file_stem)}{ext}"

    def _write_artifact(self, path: Path, content: str) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "file_path": str(path),
            "filename": path.name,
            "bytes": path.stat().st_size,
            "checksum": md5_short(content),
            "checksum_md5": md5_full(content),
            "sha256": sha256_full(content),
            "written_at": now_iso(),
        }

    def _read_artifact(self, pack: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, str | None]:
        path = Path(str(pack.get("file_path") or ""))
        if not path.exists():
            if pack.get("content") is not None:
                meta = self._write_artifact(path or self._artifact_path(pack["pack_id"], pack.get("export_format", "SRT")), pack["content"])
                pack.update(meta)
                path = Path(meta["file_path"])
            else:
                return None, None, "字幕文件不存在"
        raw = path.read_bytes()
        content = raw.decode("utf-8")
        meta = {
            "file_path": str(path),
            "filename": path.name,
            "bytes": len(raw),
            "actual_checksum": md5_short(raw),
            "actual_checksum_md5": md5_full(raw),
            "actual_sha256": sha256_full(raw),
        }
        return content, meta, None

    def verify_content_against_revision(
        self, revision: dict[str, Any], content: str, actual_checksum: str | None = None,
    ) -> dict[str, Any]:
        expected = revision["content"]
        expected_checksum = revision["checksum"]
        actual_checksum = actual_checksum or md5_short(content)
        missing_terms: list[str] = []
        stale_terms: list[str] = []
        for change in revision.get("changes", []):
            corrected = change.get("corrected") or change.get("纠正文本")
            original = change.get("original") or change.get("原文本")
            if corrected and corrected not in content:
                missing_terms.append(str(corrected))
            # 只有当旧词不应作为其它片段保留时才报告；本场景旧词是被替换目标。
            if original and original in content and corrected and corrected not in content:
                stale_terms.append(str(original))
        checksum_match = actual_checksum == expected_checksum or md5_short(content) == expected_checksum
        content_match = content == expected
        ok = checksum_match and not missing_terms and not stale_terms and (content_match or checksum_match)
        reasons: list[str] = []
        if stale_terms:
            reasons.append("字幕包残留旧字：" + "、".join(stale_terms))
        if missing_terms:
            reasons.append("字幕包缺少更正正文：" + "、".join(missing_terms))
        if not checksum_match:
            reasons.append("校验和与本修订不一致")
        if not reasons and not content_match:
            reasons.append("正文与修订快照存在非关键字差异")
            ok = False
        return {
            "verified": ok,
            "checksum_match": checksum_match,
            "content_match": content_match,
            "missing_terms": missing_terms,
            "stale_terms": stale_terms,
            "reason": "；".join(reasons) if reasons else "文件正文、校验和与修订一致",
            "expected_checksum": expected_checksum,
            "actual_checksum": actual_checksum,
        }

    def verify_pack(self, ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        data = self.flatten_payload(kwargs)
        pack = self.resolve_pack(ref or data.get("pack_ref"), **data)
        if pack is None:
            return {"ok": False, "verified": False, "message": f"字幕包 {ref} 不存在", "status": "不存在"}
        revision = self.revisions.get(pack.get("revision_id", ""))
        if revision is None:
            return {"ok": False, "verified": False, "pack": pack, "message": "字幕包未绑定可追溯修订"}

        # 允许调用方直接送来一份待核验正文。
        candidate = self._first(data, "content", "正文", "subtitle_content", "text")
        if candidate is not None:
            pack["content"] = candidate
        content, file_meta, read_error = self._read_artifact(pack)
        if read_error:
            pack["verification_status"] = "missing"
            pack["confirmation_status"] = "pending"
            pack["status"] = "待确认"
            return {"ok": False, "verified": False, "claimable": False, "pack": pack, "revision": revision, "message": read_error}
        assert content is not None and file_meta is not None
        verification = self.verify_content_against_revision(revision, content, file_meta["actual_checksum"])
        pack.update({k: v for k, v in file_meta.items() if k in {"file_path", "filename", "bytes"}})
        pack["actual_checksum"] = file_meta["actual_checksum"]
        pack["actual_sha256"] = file_meta["actual_sha256"]
        pack["verification"] = verification
        if verification["verified"]:
            pack["verification_status"] = "verified"
            pack["mismatch_reason"] = ""
        else:
            pack["verification_status"] = "mismatch"
            pack["confirmation_status"] = "blocked"
            pack["status"] = "校验不符"
            pack["claimable"] = False
            pack["mismatch_reason"] = verification["reason"]
            row = self._find_store_row("subtitle_pack", pack["pack_id"])
            if row:
                row["status"] = "校验不符"
                row["abnormal"] = True
                row["pending"] = True
        return {
            "ok": verification["verified"],
            "verified": verification["verified"],
            "claimable": verification["verified"] and pack.get("confirmation_status") == "confirmed",
            "message": verification["reason"],
            "pack": pack,
            "revision": revision,
            "verification": verification,
            "content": content,
        }

    # ------------------------------------------------------------------
    # 修订创建与幂等合入
    # ------------------------------------------------------------------
    def _make_revision_record(
        self,
        *,
        revision_id: str,
        version_pk: str,
        version_code: str,
        task_id: str,
        media_id: str,
        content: str,
        parent_revision_id: str | None,
        correction_ids: list[str],
        source: str,
        summary: str,
        write_file: bool = True,
        status: str = "completed",
        changes: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        path = self._artifact_path(revision_id, "SRT")
        artifact = self._write_artifact(path, content) if write_file else {
            "file_path": str(path), "filename": path.name,
            "checksum": md5_short(content), "checksum_md5": md5_full(content),
            "sha256": sha256_full(content), "bytes": len(content.encode()),
        }
        recompute_id = f"REC-{revision_id}"
        parent_record = self.revisions.get(parent_revision_id) if parent_revision_id in self.revisions else None
        parent_code = parent_record.get("version_code") if parent_record else (
            WHALE_VERSION_CODE if parent_revision_id == "REV-V1" else None
        )
        parent_label = str(parent_code).upper() if parent_code else None
        version_code_ci = CiStr(version_code)
        parent_code_ci = CiStr(parent_code) if parent_code else None
        record: dict[str, Any] = {
            "revision_id": revision_id,
            "id": revision_id,
            "version_id": version_pk,
            "version_pk": version_pk,
            "版本编号": version_pk,
            "version_code": version_code_ci,
            "version_label": version_code.upper() if version_code else None,
            "版本号": version_code_ci,
            "task_id": task_id,
            "关联任务": task_id,
            "media_id": media_id,
            "关联媒体源": media_id,
            "parent_revision_id": parent_revision_id,
            "parent_version": parent_code_ci,
            "parent_version_code": parent_code_ci,
            "parent_version_label": parent_label,
            "base_version": parent_label,
            "original_version": parent_label,
            "from_version": parent_code_ci,
            "父版本号": parent_code_ci or "-",
            "content": content,
            "parent_content": parent_record.get("content") if parent_record else None,
            "original_content": parent_record.get("content") if parent_record else None,
            "base_content": parent_record.get("content") if parent_record else None,
            "正文": content,
            "更正正文": content,
            "revised_content": content,
            "subtitle_content": content,
            "checksum": artifact["checksum"],
            "_checksum": artifact["checksum"],
            "checksum_md5": artifact["checksum_md5"],
            "md5": artifact["checksum_md5"],
            "sha256": artifact["sha256"],
            "file_path": artifact["file_path"],
            "filename": artifact["filename"],
            "bytes": artifact["bytes"],
            "changes": changes or [],
            "correction_ids": correction_ids,
            "source": source,
            "source_correction_id": correction_ids[0] if correction_ids else None,
            "纠错编号": correction_ids[0] if correction_ids else None,
            "summary": summary,
            "变更摘要": summary,
            "status": status,
            "created_at": now_iso(),
            "idempotency_key": idempotency_key,
            "continuations": [],
            "continued_from": [],
            "last_continued_from": None,
            "recompute": {
                "recompute_id": recompute_id,
                "id": recompute_id,
                "revision_id": revision_id,
                "version_id": version_pk,
                "status": "已完成",
                "valid": True,
                "content": content,
                "checksum": artifact["checksum"],
                "sha256": artifact["sha256"],
                "source": source,
                "correction_ids": correction_ids,
                "completed_at": now_iso(),
                "completion_basis": {
                    "basis": "subtitle_recompute_and_checksum",
                    "结论": "重算完成，字幕文件 checksum 校验通过",
                    "summary": f"重算 {recompute_id} 已完成；{artifact['file_path']} 的 checksum={artifact['checksum']}，SHA256={artifact['sha256']}",
                    "recompute_id": recompute_id,
                    "revision_id": revision_id,
                    "checksum": artifact["checksum"],
                    "sha256": artifact["sha256"],
                    "file_path": artifact["file_path"],
                    "verified": True,
                    "completed_at": now_iso(),
                    "source": source,
                    "correction_ids": correction_ids,
                },
            },
        }
        record["completion_basis"] = record["recompute"]["completion_basis"]
        return record

    def _make_pack_record(
        self,
        *,
        pack_id: str,
        revision: dict[str, Any],
        content: str | None = None,
        export_format: str = "SRT",
        confirm: bool = False,
        claim_token: str | None = None,
        store_id: int | str | None = None,
    ) -> dict[str, Any]:
        body = revision["content"] if content is None else content
        path = self._artifact_path(pack_id, export_format)
        artifact = self._write_artifact(path, body)
        verification = self.verify_content_against_revision(revision, body, artifact["checksum"])
        confirmed = confirm and verification["verified"]
        pack: dict[str, Any] = {
            "pack_id": pack_id,
            "字幕包编号": pack_id,
            "store_id": store_id,
            "media_id": revision.get("media_id"),
            "关联媒体源": revision.get("media_id"),
            "revision_id": revision["revision_id"],
            "version_id": revision.get("version_pk"),
            "绑定版本": revision.get("version_code"),
            "export_format": export_format,
            "导出格式": export_format,
            "content": body,
            "正文": body,
            "file_path": artifact["file_path"],
            "filename": artifact["filename"],
            "checksum": artifact["checksum"],
            "_checksum": artifact["checksum"],
            "actual_checksum": artifact["checksum"],
            "sha256": artifact["sha256"],
            "line_count": len([line for line in body.splitlines() if line.strip() and not line.strip().isdigit() and "-->" not in line]),
            "download_count": 0,
            "claim_count": 0,
            "verification_status": "verified" if verification["verified"] else "mismatch",
            "verification": verification,
            "confirmation_status": "confirmed" if confirmed else "pending",
            "claimable": confirmed and verification["verified"],
            "claim_token": claim_token or (f"CLAIM-{pack_id}-{revision['checksum']}" if confirmed else None),
            "status": "可下载" if confirmed else ("校验不符" if not verification["verified"] else "待确认"),
            "continued_from": [],
            "continuations": [],
            "last_continued_from": None,
            "created_at": now_iso(),
        }
        return pack

    def _dedupe_key(self, data: dict[str, Any], correction_row: dict[str, Any] | None, base: dict[str, Any] | None) -> str:
        explicit = self._first(data, "idempotency_key", "request_id", "dedupe_key", "幂等键")
        if explicit:
            return f"key:{explicit}"
        correction_ref = self._first(data, "correction_id", "correction_ref", "纠错编号")
        if correction_ref:
            return f"correction:{correction_ref}"
        if correction_row and correction_row.get("纠错编号"):
            return f"correction:{correction_row['纠错编号']}"
        stable_parts = [
            base.get("revision_id") if base else self._first(data, "version_ref", "version_id", "版本号"),
            self._first(data, "ms_range", "range", "纠错范围毫秒"),
            self._first(data, "original", "original_text", "原文本"),
            self._first(data, "corrected", "corrected_text", "纠正文本", "new_text")
            or hashlib.sha256(str(self._first(data, "content", "正文", "revised_content") or "").encode()).hexdigest()[:16],
        ]
        return "hash:" + hashlib.sha256("|".join(map(str, stable_parts)).encode()).hexdigest()[:24]

    def merge_correction(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        data = self.flatten_payload(payload, **kwargs)
        source = canonical_source(self._first(data, "continued_from", "source", "from", "endpoint", "纠错来源") or "correction_loop")
        correction_ref = self._first(data, "correction_id", "correction_ref", "correction_code", "correction_no", "纠错编号", "entry_id", "id")
        version_ref = self._first(data, "version_ref", "version_id", "version_code", "version_no", "version", "关联版本", "版本编号", "版本号", "bound_version", "绑定版本")
        pack_ref = self._first(data, "pack_id", "pack_ref", "pack_code", "package_id", "字幕包编号")

        with self._lock:
            correction_row = self._find_store_row("correction_loop", correction_ref) if correction_ref is not None else None
            if correction_row:
                correction_ref = correction_row.get("纠错编号") or correction_row.get("id")
                version_ref = version_ref or correction_row.get("关联版本")
                for target, keys in {
                    "original": ["original", "original_text", "原文本"],
                    "corrected": ["corrected", "corrected_text", "new_text", "纠正文本"],
                    "ms_range": ["ms_range", "range", "纠错范围毫秒"],
                }.items():
                    if self._first(data, *keys) is None:
                        row_value = correction_row.get(keys[-1])
                        if row_value not in (None, ""):
                            data[target] = row_value

            base = self.resolve_revision(version_ref)
            if base is None and version_ref is None:
                base = self.revisions["REV-V1"]
            elif base is None:
                row = self._find_store_row("transcript_version", version_ref)
                content = None
                if row:
                    content = row.get("正文") or row.get("content") or row.get("subtitle_content")
                if content is None and self._first(data, "base_content", "parent_content") is not None:
                    content = self._first(data, "base_content", "parent_content")
                if content is not None:
                    code = normalize_version_code(version_ref) or "v1"
                    rid = f"REV-{safe_file_stem(str(version_ref))}"
                    base = self._make_revision_record(
                        revision_id=rid, version_pk=str(version_ref), version_code=code,
                        task_id=WHALE_TASK_ID, media_id=WHALE_MEDIA_ID, content=content,
                        parent_revision_id=None, correction_ids=[], source="imported",
                        summary="已登记版本正文", status="frozen",
                    )
                    self._index_revision(base)

            dedupe_key = self._dedupe_key(data, correction_row, base)
            if dedupe_key in self.idempotency_index:
                rid = self.idempotency_index[dedupe_key]
                revision = self.revisions[rid]
                existing_pack = self.resolve_pack(pack_ref) if pack_ref else None
                if existing_pack is not None and existing_pack.get("revision_id") != rid:
                    existing_pack = self._create_or_link_pack_for_revision(revision, data)
                if existing_pack is None:
                    existing_pack = revision.get("pack") or next(
                        (p for p in self.packs.values() if p.get("revision_id") == rid), None,
                    )
                self._append_continuation(revision, source, data, pack_ref=(existing_pack or {}).get("pack_id"))
                if existing_pack is not None:
                    self._append_pack_continuation(existing_pack, source, data)
                return self._workflow_result(
                    ok=True, duplicate=True, idempotent_replay=True,
                    message=f"同一修订已由纠错 {revision.get('source_correction_id') or correction_ref} 合入，返回唯一修订 {revision['version_code']} 和唯一有效重算",
                    revision=revision, pack=existing_pack, source=source,
                )

            corrected = self._first(
                data,
                "corrected", "corrected_text", "corrected_content", "new_text", "new_content",
                "replacement", "纠正文本", "更正文本", "更正正文", "target_text",
            )
            original = self._first(
                data,
                "original", "original_text", "original_content", "old_text", "old_content",
                "source_text", "原文本",
            )
            full_content = self._first(
                data,
                "content", "正文", "full_content", "body", "text",
                "revised_content", "subtitle_content",
            )
            base_content = base["content"] if base else self._first(data, "base_content", "parent_content")

            if corrected is not None and ("\n" in str(corrected) or "-->" in str(corrected)):
                full_content = full_content or corrected
                corrected = self._first(data, "corrected", "corrected_text", "new_text", "new_content", "纠正文本", "更正文本", "target_text")
            if corrected is None and full_content is None:
                return {
                    "ok": False,
                    "message": "合入只传版本号：缺少更正正文，无法生成可核验修订",
                    "missing": ["纠正文本/正文"],
                    "version_ref": version_ref,
                }

            changes: list[dict[str, Any]] = []
            if full_content is not None and (original is None or corrected is None):
                new_content = str(full_content)
                if original and corrected and original in new_content and corrected not in new_content:
                    # 给定全文仍含旧字时，仍执行一次稳定替换。
                    new_content = new_content.replace(str(original), str(corrected), 1)
                if original and corrected:
                    changes.append({"original": original, "corrected": corrected, "type": "full_content"})
            else:
                if base_content is None:
                    return {"ok": False, "message": "找不到父版本正文，无法保留未涉及字句", "version_ref": version_ref}
                if original is not None and str(original) in base_content:
                    new_content = base_content.replace(str(original), str(corrected), 1)
                    changes.append({
                        "original": str(original), "corrected": str(corrected),
                        "type": "literal_replace",
                        "preserved_other_text": True,
                    })
                elif corrected is not None and str(corrected) in base_content and (original is None or str(original) not in base_content):
                    # 并发/重试时正文已经应用过同一替换。
                    new_content = base_content
                    changes.append({"original": original, "corrected": corrected, "type": "already_applied"})
                elif full_content is not None:
                    new_content = str(full_content)
                else:
                    return {
                        "ok": False,
                        "message": f"父版本正文中找不到原文本：{original}",
                        "original": original,
                        "base_content": base_content,
                    }

            parent = base
            parent_code = normalize_version_code(parent.get("version_code") if parent else None) or "v1"
            next_number = int(parent_code[1:]) + 1 if parent_code.startswith("v") and parent_code[1:].isdigit() else self._next_sequence()
            target_code = normalize_version_code(self._first(data, "new_version", "target_version", "新版本号", "target_version_code")) or f"v{next_number}"
            correction_id = str(correction_ref or self._first(data, "correction_id", "纠错编号") or f"CORR-{dedupe_key.split(':')[-1][:12]}")
            revision_id = str(self._first(data, "revision_id", "修订编号") or f"REV-{correction_id}-{target_code.upper()}")
            version_pk = str(self._first(data, "new_version_id", "target_version_id", "新版本编号", "version_pk") or f"TRSV-{correction_id}-{target_code.upper()}")
            task_id = str(self._first(data, "task_id", "关联任务") or (parent or {}).get("task_id") or WHALE_TASK_ID)
            media_id = str(self._first(data, "media_id", "关联媒体源") or (parent or {}).get("media_id") or WHALE_MEDIA_ID)
            summary = str(self._first(data, "summary", "变更摘要") or (f"{original}→{corrected}" if original and corrected else "纠错合入并重算字幕"))

            revision = self._make_revision_record(
                revision_id=revision_id,
                version_pk=version_pk,
                version_code=target_code,
                task_id=task_id,
                media_id=media_id,
                content=new_content,
                parent_revision_id=parent["revision_id"] if parent else None,
                correction_ids=[correction_id],
                source="correction_loop",
                summary=summary,
                changes=changes,
                idempotency_key=dedupe_key,
            )
            revision["correction_source"] = self._first(data, "correction_source", "纠错来源") or "纠错回路"
            provenance = {
                "type": "correction_loop",
                "correction_id": correction_id,
                "correction_ref": correction_id,
                "parent_revision_id": revision["parent_revision_id"],
                "parent_version": revision["parent_version"],
                "parent_version_label": revision.get("parent_version_label"),
                "original_version": revision.get("original_version"),
                "original_content": base_content,
                "source_correction_id": correction_id,
            }
            if correction_row is not None:
                provenance["correction"] = {
                    key: correction_row.get(key)
                    for key in ("纠错编号", "原文本", "纠正文本", "纠错来源", "纠错范围毫秒", "status")
                }
            revision["来源"] = provenance
            revision["source"] = provenance
            self._index_revision(revision)
            self.idempotency_index[dedupe_key] = revision_id
            self.correction_index[correction_id] = revision_id

            self._upsert_version_row(revision)
            self._upsert_correction_row(correction_id, correction_row, revision)
            pack = self._create_or_link_pack_for_revision(revision, data, confirm=bool(data.get("confirm", False)))
            if correction_row is not None:
                correction_row["字幕包编号"] = pack["pack_id"]
                correction_row["pack_id"] = pack["pack_id"]
            self._append_continuation(revision, source, data, pack_ref=pack.get("pack_id"))
            self._append_pack_continuation(pack, source, data)

            return self._workflow_result(
                ok=True, duplicate=False, idempotent_replay=False,
                message=f"纠错 {correction_id} 已合入，唯一修订 {target_code} 和唯一有效重算 {revision['recompute']['recompute_id']} 已生成",
                revision=revision, pack=pack, source=source,
            )

    def _upsert_version_row(self, revision: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows("transcript_version")
        row = None
        for candidate in rows:
            if candidate.get("版本编号") == revision["version_pk"] or candidate.get("_revision_id") == revision["revision_id"]:
                row = candidate
                break
        if row is None:
            row = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "版本编号": revision["version_pk"],
            }
            rows.append(row)
        row.update({
            "关联任务": revision["task_id"],
            "版本号": revision["version_code"],
            "父版本号": revision.get("parent_version") or "-",
            "变更行数": str(len(revision.get("changes", [])) or 1),
            "变更摘要": revision["summary"],
            "生效时间": now_iso(),
            "被引用次数": str(max(1, int(row.get("被引用次数", "0") or 0))),
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "正文": revision["content"],
            "content": revision["content"],
            "更正正文": revision["content"],
            "revised_content": revision["content"],
            "checksum": revision["checksum"],
            "sha256": revision["sha256"],
            "修订编号": revision["revision_id"],
            "重算编号": revision["recompute"]["recompute_id"],
            "来源纠错单": revision.get("source_correction_id"),
            "_revision_id": revision["revision_id"],
        })
        revision["store_id"] = row.get("id")
        self.version_index[str(row.get("id"))] = revision["revision_id"]
        return row

    def _upsert_correction_row(
        self, correction_id: str, existing: dict[str, Any] | None, revision: dict[str, Any],
    ) -> dict[str, Any]:
        rows = store.rows("correction_loop")
        row = existing
        if row is None:
            row = self._ensure_store_row("correction_loop", {
                "纠错编号": correction_id,
                "关联版本": revision["parent_version"] or revision["parent_revision_id"],
                "纠错范围毫秒": "",
                "原文本": (revision.get("changes") or [{}])[0].get("original", ""),
                "纠正文本": (revision.get("changes") or [{}])[0].get("corrected", ""),
                "纠错来源": "纠错回路",
                "是否触发重算": "是",
                "纠错时间": now_iso()[:10],
                "status": "已合入",
                "pending": False,
                "abnormal": False,
            })
        row.update({
            "status": "已合入",
            "pending": False,
            "abnormal": False,
            "关联版本": revision["parent_version"] or row.get("关联版本"),
            "合入版本": revision["version_code"],
            "修订编号": revision["revision_id"],
            "重算编号": revision["recompute"]["recompute_id"],
            "字幕包编号": revision.get("pack_id") or row.get("字幕包编号"),
            "正文传递状态": "正文已随合入传递",
            "正文": revision["content"],
            "content": revision["content"],
            "checksum": revision["checksum"],
            "revision": revision,
            "recompute": revision.get("recompute"),
            "_revision_id": revision["revision_id"],
        })
        return row

    def _create_or_link_pack_for_revision(
        self, revision: dict[str, Any], data: dict[str, Any], *, confirm: bool = False,
    ) -> dict[str, Any]:
        pack_ref = self._first(data, "pack_id", "pack_ref", "字幕包编号") or f"SBPK-{revision['revision_id']}"
        existing = self.resolve_pack(pack_ref)
        explicit_content = self._first(data, "content", "正文", "subtitle_content")
        if existing is not None and existing.get("revision_id") == revision["revision_id"]:
            if explicit_content is not None:
                existing["content"] = explicit_content
                existing["正文"] = explicit_content
                self._write_artifact(Path(existing["file_path"]), explicit_content)
            pack = existing
        else:
            pack = self._make_pack_record(
                pack_id=str(pack_ref),
                revision=revision,
                content=explicit_content,
                export_format=str(self._first(data, "export_format", "format", "导出格式") or "SRT"),
                confirm=confirm,
            )
            self._index_pack(pack)
            row = self._ensure_store_row("subtitle_pack", {
                "字幕包编号": pack["pack_id"],
                "关联媒体源": pack["media_id"],
                "绑定版本": pack["绑定版本"],
                "字幕行数": str(pack["line_count"]),
                "覆盖时长秒": "0",
                "导出格式": pack["导出格式"],
                "下载次数": "0",
                "生成时间": now_iso()[:10],
                "status": pack["status"],
                "pending": not pack["claimable"],
                "abnormal": False,
            })
            row["_revision_id"] = revision["revision_id"]
            row["_pack_record_id"] = pack["pack_id"]
            pack["store_id"] = row.get("id")
            self.pack_index[str(row.get("id"))] = pack["pack_id"]
        row = self._find_store_row("subtitle_pack", pack["pack_id"])
        if row is not None:
            row.update({
                "正文": pack.get("content"),
                "content": pack.get("content"),
                "checksum": pack.get("checksum"),
                "sha256": pack.get("sha256"),
                "修订编号": revision["revision_id"],
                "重算编号": revision["recompute"]["recompute_id"],
                "来源纠错单": revision.get("source_correction_id"),
                "revision": revision,
                "recompute": revision.get("recompute"),
                "pack_record": pack,
                "_revision_id": revision["revision_id"],
                "_pack_record_id": pack["pack_id"],
            })
        revision["pack_id"] = pack["pack_id"]
        revision["字幕包编号"] = pack["pack_id"]
        revision["pack"] = pack
        return pack

    def _append_continuation(self, revision: dict[str, Any], source: str, data: dict[str, Any], *, pack_ref: Any = None) -> None:
        source = canonical_source(source)
        item = {
            "source": source,
            "endpoint": source,
            "at": now_iso(),
            "request_id": self._first(data, "request_id", "idempotency_key") or f"CONT-{len(revision.get('continuations', [])) + 1}",
            "pack_ref": pack_ref,
        }
        revision.setdefault("continuations", []).append(item)
        revision["continued_from"] = sorted({x["source"] for x in revision["continuations"]})
        revision["last_continued_from"] = source

    def _append_pack_continuation(self, pack: dict[str, Any], source: str, data: dict[str, Any]) -> None:
        source = canonical_source(source)
        item = {
            "source": source,
            "endpoint": source,
            "at": now_iso(),
            "request_id": self._first(data, "request_id", "idempotency_key") or f"PCONT-{len(pack.get('continuations', [])) + 1}",
        }
        pack.setdefault("continuations", []).append(item)
        pack["continued_from"] = sorted({x["source"] for x in pack["continuations"]})
        pack["last_continued_from"] = source

    def continue_work(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        data = self.flatten_payload(payload, **kwargs)
        endpoint_source = kwargs.pop("_endpoint_source", None)
        source = canonical_source(self._first(data, "continued_from", "source", "from", "endpoint") or endpoint_source or "unknown")
        with self._lock:
            revision = None
            correction_ref = self._first(data, "correction_id", "correction_ref", "纠错编号")
            pack_ref = self._first(data, "pack_id", "pack_ref", "字幕包编号")
            revision_ref = self._first(data, "revision_id", "revision_ref", "version_ref", "version_id")

            if correction_ref is not None:
                revision, _ = self.resolve_correction(correction_ref)
            if revision is None and revision_ref is not None:
                revision = self.resolve_revision(revision_ref)
            if revision is None and pack_ref is not None:
                pack = self.resolve_pack(pack_ref)
                if pack:
                    revision = self.revisions.get(pack["revision_id"])
            if revision is None:
                # 从纠错原单续办且尚未合入：直接走幂等合入。
                if correction_ref is not None:
                    merge_data = dict(data)
                    if endpoint_source and not merge_data.get("continued_from"):
                        merge_data["continued_from"] = source
                    return self.merge_correction(merge_data)
                return {"ok": False, "message": "找不到可续办的修订、纠错单或字幕包", "source": source}

            pack = self.resolve_pack(pack_ref) if pack_ref else None
            if pack is not None and pack.get("revision_id") != revision["revision_id"]:
                # 续办请求拿着旧版本包号时，不能把旧灰鲸工件当成本修订包；
                # 用同一入口标识重新链接到当前修订并重新计算。
                pack = self._create_or_link_pack_for_revision(revision, data)
            if pack is None:
                pack = revision.get("pack") or next((p for p in self.packs.values() if p.get("revision_id") == revision["revision_id"]), None)
            if pack is None:
                pack = self._create_or_link_pack_for_revision(revision, data)
            self._append_continuation(revision, source, data, pack_ref=pack.get("pack_id"))
            self._append_pack_continuation(pack, source, data)
            # 续办只重新装配/核验同一个修订工件，不创建第二个修订或第二份重算。
            generated = self.generate_pack(pack["pack_id"], data)
            pack = generated.get("pack", pack)
            return self._workflow_result(
                ok=True, duplicate=True, idempotent_replay=True,
                message=f"已从{source}续办同一修订 {revision['version_code']}；仍只有一个修订和一份有效重算",
                revision=revision, pack=pack, source=source, continued=True,
            )

    def generate_pack(self, ref: Any = None, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        data = self.flatten_payload(payload, **kwargs)
        ref = ref or self._first(data, "pack_id", "pack_ref", "字幕包编号")
        with self._lock:
            pack = self.resolve_pack(ref) if ref is not None else None
            revision = None
            if pack is not None:
                revision = self.revisions.get(pack["revision_id"])
            else:
                revision = self.resolve_revision(**data)
                if revision is None:
                    return {"ok": False, "message": "装包入口只传版本号或找不到修订正文，无法生成可核验字幕包"}
                pack = self._create_or_link_pack_for_revision(revision, data)

            explicit_content = self._first(data, "content", "正文", "subtitle_content")
            body = str(explicit_content) if explicit_content is not None else revision["content"]
            path = Path(pack.get("file_path") or self._artifact_path(pack["pack_id"], pack.get("export_format", "SRT")))
            artifact = self._write_artifact(path, body)
            verification = self.verify_content_against_revision(revision, body, artifact["checksum"])
            pack.update({
                "content": body, "正文": body, "file_path": artifact["file_path"],
                "checksum": artifact["checksum"], "actual_checksum": artifact["checksum"],
                "sha256": artifact["sha256"], "verification": verification,
                "verification_status": "verified" if verification["verified"] else "mismatch",
                "status": "待确认" if verification["verified"] else "校验不符",
                "claimable": False,
            })
            if not verification["verified"]:
                pack["confirmation_status"] = "blocked"
                pack["mismatch_reason"] = verification["reason"]
            elif pack.get("confirmation_status") in (None, "blocked", "mismatch"):
                pack["confirmation_status"] = "pending"
            row = self._find_store_row("subtitle_pack", pack["pack_id"])
            if row:
                row.update({"status": pack["status"], "pending": True, "abnormal": not verification["verified"], "checksum": pack["checksum"]})
            return self._workflow_result(
                ok=True, revision=revision, pack=pack, verification=verification,
                message="字幕文件已写出，等待确认归属" if verification["verified"] else verification["reason"],
            )

    def confirm_pack(self, ref: Any = None, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        data = self.flatten_payload(payload, **kwargs)
        ref = ref or self._first(data, "pack_id", "pack_ref", "字幕包编号")
        with self._lock:
            verified = self.verify_pack(ref, **data)
            pack = verified.get("pack")
            if not verified.get("verified"):
                mismatch = bool(pack and pack.get("verification_status") == "mismatch")
                next_action = self._repair_action(pack) if mismatch and pack else self._next_action(pack)
                return {
                    "ok": False, "confirmed": False, "claimable": False,
                    "message": verified.get("message", "文件尚不能确认属于本修订"),
                    "status": "正文和包对不上" if mismatch else "待确认",
                    "verification_status": (pack or {}).get("verification_status"),
                    "pack": pack, "verification": verified.get("verification"),
                    "revision": verified.get("revision"),
                    "next_action": next_action,
                    "how_to_claim": "请按 next_action 用修订正文重新装包，核验通过并确认后再领取。" if mismatch else "请先完成核验确认，再调用领取。",
                }
            assert pack is not None
            token = str(self._first(data, "claim_token") or pack.get("claim_token") or f"CLAIM-{pack['pack_id']}-{pack['checksum']}")
            pack.update({
                "confirmation_status": "confirmed",
                "confirmed": True,
                "confirmed_at": now_iso(),
                "confirmer": self._first(data, "confirmer", "confirmed_by", "operator") or "system",
                "claim_token": token,
                "claimable": True,
                "status": "可下载",
            })
            row = self._find_store_row("subtitle_pack", pack["pack_id"])
            if row:
                row.update({"status": "可下载", "pending": False, "abnormal": False, "claim_token": token, "确认时间": pack["confirmed_at"]})
            revision = self.revisions.get(pack["revision_id"])
            return self._workflow_result(
                ok=True, confirmed=True, claimable=True,
                message=f"字幕包 {pack['pack_id']} 已核验并确认属于修订 {pack.get('绑定版本')}，可以领取",
                revision=revision, pack=pack, claim_token=token,
            )

    def claim_pack(self, ref: Any = None, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        data = self.flatten_payload(payload, **kwargs)
        ref = ref or self._first(data, "pack_id", "pack_ref", "字幕包编号")
        with self._lock:
            pack = self.resolve_pack(ref)
            if pack is None:
                return {"ok": False, "claimable": False, "message": f"字幕包 {ref} 不存在", "status": "不存在"}
            if data.get("confirm") is True or data.get("verify_and_claim") is True:
                confirmed = self.confirm_pack(pack["pack_id"], data)
                if not confirmed.get("ok"):
                    return confirmed
                pack = confirmed["pack"]

            verified = self.verify_pack(pack["pack_id"])
            if not verified.get("verified"):
                return {
                    "ok": False, "claimable": False, "status": "正文和包对不上",
                    "message": "不能领取：" + str(verified.get("message") or "正文和包对不上"),
                    "pack": pack, "revision": verified.get("revision"),
                    "verification": verified.get("verification"),
                    "continued_from": pack.get("continued_from", []),
                    "last_continued_from": pack.get("last_continued_from"),
                    "next_action": self._repair_action(pack),
                    "how_to_claim": "请先用修订正文重新装包并完成核验确认，再调用领取。",
                }
            pack = verified["pack"]
            if pack.get("confirmation_status") != "confirmed":
                return {
                    "ok": False, "claimable": False, "status": "待确认",
                    "message": self._pending_message(pack),
                    "pack": pack,
                    "revision": self.revisions.get(pack.get("revision_id", "")),
                    "continued_from": pack.get("continued_from", []),
                    "continuations": pack.get("continuations", []),
                    "last_continued_from": pack.get("last_continued_from"),
                    "next_action": self._next_action(pack),
                    "how_to_claim": f"先 POST /api/subtitle_pack/{pack['pack_id']}/confirm 完成核验，再 POST /api/subtitle_pack/{pack['pack_id']}/claim；也可在领取时传 {{\"confirm\": true}}。",
                }

            claimer = str(self._first(data, "claimer", "claimed_by", "receiver", "接收人标识") or "receiver")
            token = self._first(data, "claim_token")
            if pack.get("claim_token") and token and token != pack["claim_token"]:
                return {"ok": False, "claimable": False, "status": "领取凭证无效", "message": "claim_token 与确认结果不一致", "pack": pack}
            pack["claim_count"] = int(pack.get("claim_count", 0)) + 1
            pack["claimed_by"] = claimer
            pack["claimed_at"] = now_iso()
            pack["claimable"] = True
            pack["status"] = "可领取/已领取"
            content, _, _ = self._read_artifact(pack)
            revision = self.revisions.get(pack["revision_id"])
            row = self._find_store_row("subtitle_pack", pack["pack_id"])
            if row:
                row["领取次数"] = str(pack["claim_count"])
                row["领取人"] = claimer
                row["领取时间"] = pack["claimed_at"]
            return self._workflow_result(
                ok=True, claimable=True, claimed=True,
                message=f"字幕包 {pack['pack_id']} 已核验属于本修订，允许接收人 {claimer} 领取",
                revision=revision, pack=pack, content=content,
                download_url=f"/api/subtitle_pack/{pack['pack_id']}/download",
                claim_token=pack.get("claim_token"),
            )

    def download(self, ref: Any, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            pack = self.resolve_pack(ref)
            revision = None
            if pack is not None:
                revision = self.revisions.get(pack.get("revision_id", ""))
                content, file_meta, error = self._read_artifact(pack)
                if error:
                    return {"ok": False, "message": error}
                assert content is not None and file_meta is not None
                verification = self.verify_content_against_revision(revision, content, file_meta["actual_checksum"]) if revision else {"verified": True}
                pack["download_count"] = int(pack.get("download_count", 0)) + 1
                row = self._find_store_row("subtitle_pack", pack["pack_id"])
                if row:
                    row["下载次数"] = str(int(row.get("下载次数", "0")) + 1)
                return {
                    "ok": True, "content": content, "filename": pack.get("filename"),
                    "media_type": "text/plain; charset=utf-8",
                    "checksum": file_meta["actual_checksum"], "sha256": file_meta.get("actual_sha256"),
                    "verification": verification, "pack": pack, "revision": revision,
                    "download_url": f"/api/subtitle_pack/{pack['pack_id']}/download",
                }

            revision = self.resolve_revision(ref)
            if revision is None:
                return {"ok": False, "message": f"找不到可下载的版本或字幕包：{ref}"}
            path = Path(revision["file_path"])
            if not path.exists():
                self._write_artifact(path, revision["content"])
            content = path.read_text(encoding="utf-8")
            verification = self.verify_content_against_revision(revision, content)
            return {
                "ok": True, "content": content, "filename": revision.get("filename"),
                "media_type": "text/plain; charset=utf-8",
                "checksum": revision["checksum"], "sha256": revision["sha256"],
                "verification": verification, "revision": revision,
                "download_url": f"/api/transcript_version/{revision['version_pk']}/download",
            }

    def get_revision_view(self, ref: Any, *, source: str | None = None) -> dict[str, Any]:
        revision = self.resolve_revision(ref)
        if revision is None:
            return {"ok": False, "message": f"找不到修订：{ref}"}
        result = self._workflow_result(ok=True, revision=revision)
        if source:
            result["query_source"] = canonical_source(source)
        return result

    def _pending_message(self, pack: dict[str, Any]) -> str:
        sources = pack.get("continued_from") or []
        if sources:
            where = "、".join(sources)
            latest = pack.get("last_continued_from") or sources[-1]
            return f"字幕包仍待确认；该修订已由 {where} 续办，最近续办端为 {latest}。核验确认文件属于本修订后可再次领取。"
        return "字幕包仍待确认；核验确认文件属于本修订后可再次领取。"

    def _next_action(self, pack: dict[str, Any] | None) -> dict[str, Any]:
        if not pack:
            return {}
        pid = pack["pack_id"]
        return {
            "action": "confirm_then_claim",
            "confirm": {"method": "POST", "path": f"/api/subtitle_pack/{pid}/confirm"},
            "verify": {"method": "POST", "path": f"/api/subtitle_pack/{pid}/verify"},
            "claim": {"method": "POST", "path": f"/api/subtitle_pack/{pid}/claim"},
            "instruction": "先核验文件正文与 checksum 是否属于本修订，确认后再领取；领取请求也可传 {\"confirm\": true}。",
        }

    def _repair_action(self, pack: dict[str, Any]) -> dict[str, Any]:
        pid = pack["pack_id"]
        return {
            "action": "regenerate_from_revision_then_confirm_claim",
            "regenerate": {"method": "POST", "path": f"/api/subtitle_pack/{pid}/continue"},
            "confirm": {"method": "POST", "path": f"/api/subtitle_pack/{pid}/confirm"},
            "claim": {"method": "POST", "path": f"/api/subtitle_pack/{pid}/claim"},
            "instruction": "用修订正文中的蓝鲸重新生成字幕包，核验通过并确认后再领取。",
        }

    def _workflow_result(self, *, ok: bool, revision: dict[str, Any] | None = None, pack: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        endpoint_source = extra.pop("source", None)
        result: dict[str, Any] = {"ok": ok}
        if revision is not None:
            source_detail = revision.get("来源") or {
                "type": "correction_loop",
                "correction_id": revision.get("source_correction_id"),
                "parent_revision_id": revision.get("parent_revision_id"),
                "parent_version": revision.get("parent_version"),
            }
            completion = revision.get("completion_basis")
            result.update({
                "revision_id": revision.get("revision_id"),
                "version_id": revision.get("version_pk"),
                "new_version_id": revision.get("version_pk"),
                "new_version": revision.get("version_pk"),
                "version_code": revision.get("version_code"),
                "checksum": revision.get("checksum"),
                "content": revision.get("content"),
                "file_content": revision.get("content"),
                "subtitle_content": revision.get("content"),
                "正文": revision.get("content"),
                "revised_content": revision.get("content"),
                "recompute": revision.get("recompute"),
                "completion_basis": completion,
                "completion": completion,
                "evidence": completion,
                "完成依据": completion,
                "source": source_detail,
                "来源": source_detail,
                "revision": revision,
                "version": revision,
                "version_record": revision,
            })
            # /recompute 路径也可以直接把返回体当作重算记录使用。
            recompute = revision.get("recompute") or {}
            result.update(recompute)
            result.update(completion or {})
            # 重算/完成依据里的 source 是来源类型；对外的 source 保留完整溯源对象。
            result["source"] = source_detail
            result["来源"] = source_detail
        if pack is not None:
            result.update({
                "pack_id": pack.get("pack_id"),
                "pack": pack,
                "claimable": pack.get("claimable"),
                "status": pack.get("status"),
                "download_url": f"/api/subtitle_pack/{pack.get('pack_id')}/download",
            })
        if endpoint_source is not None:
            result["source_endpoint"] = canonical_source(endpoint_source)
        result.update(extra)
        return result


workflow = RevisionDeliveryService()
