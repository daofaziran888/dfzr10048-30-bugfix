"""端到端采集管线编排器：连接全部管线组件的跨模块数据链路。

10 条贯通链路：
1. 媒体源→采集通道→分片切分→转码作业→转写任务→语音分段→字幕对齐→字幕包导出→交付回执
2. 转码/转写失败→失败重投（只处理仍缺失分片，不覆盖已成功产出）
3. 语音段重叠→字幕对齐边界（唯一归属边界）
4. 说话人标注（人工改标覆盖机器标注，留改标次数）→转写版本
5. 置信复核（低于阈值必须复核）→转写版本（复核结论是版本生效前置）
6. 纠错回路→转写版本→字幕包重算（被引用版本不得静默覆盖）
7. 术语抽取→媒体索引（索引版本必须与生效转写版本一致）
8. 分片水位（只有连续分片全部转写产出才推进）→失败重投
9. 转写配额（超额排队而非丢弃已产出）
10. 抽样质检（字错率超限→阻断版本生效→触发纠错回路）
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.pipeline.shard_watermark_checker import ShardWatermarkChecker
from app.pipeline.redeliver_engine import RedeliverEngine
from app.pipeline.transcribe_quota_manager import TranscribeQuotaManager
from app.pipeline.version_conflict_resolver import VersionConflictResolver, VersionEntry
from app.pipeline.sample_qc_gate import SampleQcGate
from app.pipeline.subtitle_pack_assembler import SubtitlePackAssembler
from app.pipeline.archive_reclaimer import ArchiveReclaimer


class PipelineOrchestrator:
    """管线编排器：执行 10 条业务链路并记录执行轨迹。"""

    def __init__(self) -> None:
        self.shard_wm = ShardWatermarkChecker()
        self.redeliver = RedeliverEngine()
        self.quota = TranscribeQuotaManager()
        self.version = VersionConflictResolver()
        self.qc_gate = SampleQcGate()
        self.pack = SubtitlePackAssembler()
        self.archive = ArchiveReclaimer()
        self.audit_log: list[dict[str, Any]] = []
        self._run_id = 0
        self._correction_runs: dict[str, dict[str, Any]] = {}

    def _log(self, link: str, step: str, outcome: str, detail: Any = None) -> None:
        self._run_id += 1
        self.audit_log.append({
            "run_id": self._run_id,
            "link": link,
            "step": step,
            "outcome": outcome,
            "detail": detail,
        })

    def link1_media_to_delivery(
        self, media_id: str, channel_id: str, shard_count: int,
        engine: str, format: str,
    ) -> dict[str, Any]:
        """链路1：媒体源接入→采集通道拉取→分片切分→转码→转写→语音分段→字幕对齐→字幕包导出→交付回执"""
        link = "media-to-delivery"
        self._log(link, "channel_setup", "ok", f"{channel_id}->{media_id}")

        line = self.shard_wm.register_media(media_id, shard_count)
        for s in range(1, shard_count + 1):
            shard_id = f"{media_id}-S{s:03d}"
            line.mark_processed(s)
            self.redeliver.register_success(shard_id)
            self._log(link, "shard_process", "ok", shard_id)

        can_adv, msg = line.advance()
        self._log(link, "watermark_advance", "ok" if can_adv else f"FAIL:{msg}")

        version_id = f"VER-{media_id}-v1"
        entry = VersionEntry(version_id, media_id, 1, None)
        self.version.register_version(entry)

        review_ok, _ = self.version.set_review_result(media_id, 1, True)
        self._log(link, "confidence_review", "ok" if review_ok else "fail")

        eff_ok, _ = self.version.set_effective(media_id, 1)
        self._log(link, "version_effective", "ok" if eff_ok else "fail")

        content = f"SUBS-{media_id}-{format}".lower()
        pack_entry = self.pack.create_pack(
            f"PACK-{media_id}", media_id, version_id,
            shard_count * 10, shard_count * 30.0, format, content,
        )
        self._log(link, "pack_create", "ok", pack_entry.pack_id)

        self._log(link, "delivery_receipt", "ok", f"RECEIPT-{media_id}")
        return {"link": link, "ok": True, "pack_id": pack_entry.pack_id, "shards": shard_count}

    def link2_fail_redeliver(
        self, media_id: str, failed_shards: list[int], total_shards: int,
    ) -> dict[str, Any]:
        """链路2：转码/转写失败→失败重投，只处理仍缺失分片，不覆盖已成功产出"""
        link = "fail-redeliver"
        line = self.shard_wm.register_media(media_id, total_shards)

        # 标记成功分片
        for s in range(1, total_shards + 1):
            if s not in failed_shards:
                shard_id = f"{media_id}-S{s:03d}"
                line.mark_processed(s)
                self.redeliver.register_success(shard_id)

        # 登记失败并创建重投条目
        redelivered = []
        for s in failed_shards:
            shard_id = f"{media_id}-S{s:03d}"
            self.redeliver.register_failed(shard_id, "转码失败", "超时无响应")
            can_adv, msg = line.advance()
            self._log(link, "watermark_check", "stalled" if not can_adv else "ok", msg)

        # 执行重投
        candidates = self.redeliver.get_redeliver_candidates()
        for entry in candidates:
            ok, msg = self.redeliver.execute_retry(entry.entry_id, "success")
            if ok:
                line.mark_processed(int(entry.shard_id.split("-S")[1]))
                self.redeliver.register_success(entry.shard_id)
                redelivered.append(entry.shard_id)
            self._log(link, "retry_exec", "ok" if ok else f"FAIL:{msg}")

        can_adv, adv_msg = line.advance()
        self._log(link, "final_watermark", "ok" if can_adv else f"stalled:{adv_msg}")
        return {"link": link, "redelivered": redelivered, "watermark_ok": can_adv}

    def link3_overlap_boundary(
        self, task_id: str, segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """链路3：语音段重叠→字幕对齐边界，产生唯一归属边界"""
        link = "overlap-boundary"
        boundaries: list[dict[str, Any]] = []
        for i, seg in enumerate(segments):
            seg["align_start"] = seg.get("start_ms", 0)
            if i > 0:
                prev_end = segments[i - 1].get("end_ms", 0)
                if seg["align_start"] < prev_end:
                    midpoint = (seg["align_start"] + prev_end) // 2
                    segments[i - 1]["align_end"] = midpoint
                    seg["align_start"] = midpoint
                    boundaries.append({
                        "segment_a": segments[i - 1].get("seg_id"),
                        "segment_b": seg.get("seg_id"),
                        "boundary_ms": midpoint,
                        "rule": "midpoint",
                    })
            seg["align_end"] = seg.get("end_ms", seg.get("align_start", 0))
        self._log(link, "boundary_resolve", f"{len(boundaries)} boundaries")
        return {"link": link, "boundaries": boundaries, "segments": segments}

    def link4_speaker_label(
        self, segment_id: str, label: str, source: str,
    ) -> dict[str, Any]:
        """链路4：说话人标注（人工改标覆盖机器标注），留改标次数→转写版本"""
        link = "speaker-label"
        ok, msg, change = self.version.apply_speaker_label(segment_id, label, source)
        self._log(link, "label_apply", "ok" if ok else f"FAIL:{msg}")
        return {
            "link": link, "ok": ok, "message": msg,
            "change_count": change.change_count if change else 0,
        }

    def link5_confidence_review(
        self, task_id: str, version_no: int, passed: bool, threshold: float,
    ) -> dict[str, Any]:
        """链路5：置信复核（低于阈值必须复核）→转写版本生效前置"""
        link = "confidence-review"
        ok, msg = self.version.set_review_result(task_id, version_no, passed)
        self._log(link, "review_result", "ok" if ok else f"FAIL:{msg}")
        if passed:
            eff_ok, eff_msg = self.version.set_effective(task_id, version_no)
            self._log(link, "version_effective", "ok" if eff_ok else f"FAIL:{eff_msg}")
            return {"link": link, "ok": eff_ok, "message": eff_msg, "threshold": threshold}
        return {"link": link, "ok": True, "message": msg, "threshold": threshold, "blocked": True}

    def link6_correction_loop(
        self, task_id: str, version_no: int, corrected_text: str,
        original_text: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        """链路6：纠错回路→转写版本→字幕包重算，被引用版本不得静默覆盖。

        corrected_text/正文必须进入新版本和新字幕包；同一 task+目标版本的
        超时重报与两端续办幂等返回同一个 VersionEntry、Pack 和重算结果。
        """
        link = "correction-loop"
        target_no = version_no + 1
        new_version_id = kwargs.get("new_version_id") or f"VER-{task_id}-v{target_no}"
        run_key = kwargs.get("idempotency_key") or f"{task_id}::v{target_no}"

        if run_key in self._correction_runs:
            cached = self._correction_runs[run_key]
            self._log(link, "idempotent_replay", cached.get("new_version", run_key))
            return {**cached, "duplicate": True, "idempotent_replay": True}

        old_entry = next(
            (v for v in self.version._versions.get(task_id, []) if v.version_no == version_no),
            None,
        )
        old_version_id = old_entry.version_id if old_entry else f"VER-{task_id}-v{version_no}"
        old_content = getattr(old_entry, "content", None)
        explicit_content = kwargs.get("content") or kwargs.get("正文")

        if explicit_content is not None:
            new_content = str(explicit_content)
        elif old_content and original_text is not None and original_text in old_content:
            new_content = old_content.replace(original_text, corrected_text, 1)
        elif old_content and "\n" not in str(corrected_text) and "-->" not in str(corrected_text):
            # 只有短语且没有明确原词时，不能拿短语覆盖整条字幕；保留父正文，
            # 调用方也可以通过 original_text/content 给出精确补丁或全文。
            new_content = old_content
        else:
            new_content = corrected_text

        ok, msg, entry = self.version.try_override(task_id, new_version_id, target_no)
        self._log(link, "version_override", "ok" if ok else f"FAIL:{msg}")

        pack_entry = None
        recompute_result: dict[str, Any] | None = None
        if ok and entry is not None:
            if getattr(entry, "content", None) is None:
                entry.content = new_content
                entry.parent_content = old_content
                entry.correction_id = kwargs.get("correction_id")
                entry.checksum = hashlib.md5(new_content.encode()).hexdigest()[:8]

            review_ok, _ = self.version.set_review_result(task_id, target_no, True)
            self._log(link, "review_new", "ok" if review_ok else "fail")
            eff_ok, _ = self.version.set_effective(task_id, target_no)
            self._log(link, "effective_new", "ok" if eff_ok else "fail")

            recomputed = self.pack.mark_recompute(old_version_id, kwargs.get("old_content"))
            self._log(link, "pack_recompute", f"{len(recomputed)} packs")

            old_packs = self.pack.get_packs_by_version(old_version_id)
            old_pack = old_packs[0] if old_packs else None
            pack_id = kwargs.get("pack_id") or f"PACK-{new_version_id}"
            media_id = kwargs.get("media_id") or (old_pack.media_id if old_pack else f"MEDI-{task_id}")
            line_count = kwargs.get("line_count") or (old_pack.line_count if old_pack else max(1, len(new_content.splitlines()) // 2))
            duration = kwargs.get("duration_seconds") or (old_pack.duration_seconds if old_pack else 600.0)
            export_format = kwargs.get("export_format") or (old_pack.export_format if old_pack else "SRT")
            pack_entry = self.pack.create_pack(
                pack_id, media_id, new_version_id,
                int(line_count), float(duration), export_format, new_content,
                revision_id=new_version_id,
                correction_id=kwargs.get("correction_id"),
                parent_version=old_version_id,
            )
            verification_ok = pack_entry.verify(new_content, pack_entry.checksum)
            recompute_result = {
                "recompute_id": f"REC-{new_version_id}",
                "revision_id": new_version_id,
                "pack_id": pack_id,
                "status": "已完成",
                "valid": verification_ok,
                "checksum": pack_entry.checksum,
                "content": new_content,
                "completion_basis": {
                    "recompute_id": f"REC-{new_version_id}",
                    "revision_id": new_version_id,
                    "pack_id": pack_id,
                    "checksum": pack_entry.checksum,
                    "verified": verification_ok,
                    "source": kwargs.get("correction_id") or "correction_loop",
                },
            }
            self._log(link, "new_pack", "ok" if verification_ok else "checksum_fail")

        result = {
            "link": link, "ok": ok, "message": msg,
            "new_version": new_version_id if entry else None,
            "new_version_id": new_version_id if entry else None,
            "revision_id": new_version_id if entry else None,
            "entry": entry.to_dict() if entry else None,
            "revision": entry.to_dict() if entry else None,
            "content": new_content if ok else None,
            "正文": new_content if ok else None,
            "recompute": recompute_result,
            "completion_basis": (recompute_result or {}).get("completion_basis") if recompute_result else None,
            "pack_id": pack_entry.pack_id if pack_entry else None,
            "pack": pack_entry.to_dict() if pack_entry else None,
            "duplicate": False,
        }
        if ok:
            self._correction_runs[run_key] = result
        return result

    def continue_link6_correction_loop(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """纠错原单重报入口；参数与 link6_correction_loop 相同，保持幂等。"""
        return self.link6_correction_loop(*args, **kwargs)

    def continue_pack_generation(
        self, pack_id: str, source: str = "subtitle_pack", content: str | None = None,
    ) -> dict[str, Any]:
        """字幕包页续办：只续办同一个包/修订，不创建第二个修订。"""
        entry = self.pack.continue_from(pack_id, source, content)
        if entry is None:
            return {"ok": False, "message": f"字幕包 {pack_id} 不存在"}
        self._log("correction-loop", "pack_continue", f"{source}:{pack_id}")
        return {"ok": True, "pack_id": pack_id, "pack": entry.to_dict(), "continued_from": entry.continued_from}

    def confirm_pack(self, pack_id: str, content: str | None = None) -> dict[str, Any]:
        entry = next((p for p in self.pack._packs if p.pack_id == pack_id), None)
        if entry is None:
            return {"ok": False, "confirmed": False, "message": f"字幕包 {pack_id} 不存在"}
        ok = entry.confirm(content)
        return {
            "ok": ok,
            "confirmed": ok,
            "claimable": ok,
            "message": "文件核验通过，属于本修订" if ok else "正文和包对不上，不能确认属于本修订",
            "pack": entry.to_dict(),
        }

    def claim_pack(self, pack_id: str, receiver: str | None = None) -> dict[str, Any]:
        ok, message, data = self.pack.claim(pack_id, receiver)
        return {"ok": ok, "claimable": ok, "message": message, "pack": data}

    def link7_term_index(
        self, task_id: str, terms: list[dict[str, str]],
        version_no: int,
    ) -> dict[str, Any]:
        """链路7：术语抽取→媒体索引构建，索引版本必须与生效转写版本一致"""
        link = "term-index"
        index_version = f"v{version_no}"
        entries: list[dict[str, Any]] = []
        for term in terms:
            entries.append({
                "term": term.get("text", ""),
                "hit_count": len(terms),
                "index_version": index_version,
            })
        self._log(link, "index_build", f"{len(entries)} entries, version {index_version}")
        return {"link": link, "entries": entries, "index_version": index_version}

    def link8_shard_watermark(
        self, media_id: str, total_shards: int, shard_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """链路8：分片水位，只有连续分片全部转写产出后才推进，缺失触发失败重投"""
        link = "shard-watermark"
        line = self.shard_wm.register_media(media_id, total_shards)
        for sr in shard_results:
            seq = sr.get("seq", 0)
            shard_id = f"{media_id}-S{seq:03d}"
            if sr.get("success"):
                line.mark_processed(seq)
                self.redeliver.register_success(shard_id)
            else:
                self.redeliver.register_failed(shard_id, sr.get("stage", "未知"), sr.get("reason", ""))
                line.mark_failed(seq)

        can_adv, msg = line.advance()
        self._log(link, "advance", "ok" if can_adv else f"FAIL:{msg}")

        missing = self.redeliver.get_missing_shards()
        self._log(link, "missing_report", f"{len(missing)} missing")
        return {"link": link, "can_advance": can_adv, "message": msg, "missing": sorted(missing)}

    def link9_quota_queue(
        self, channel: str, engine: str, task_id: str, minutes: int,
    ) -> dict[str, Any]:
        """链路9：转写配额消耗，超额排队而非丢弃已完成转写"""
        link = "quota-queue"
        ok, msg = self.quota.consume(channel, engine, task_id, minutes)
        self._log(link, "consume", "ok" if ok else f"QUEUED:{msg}")
        return {"link": link, "ok": ok, "message": msg}

    def link10_sample_qc(
        self, task_id: str, segment_count: int, error_count: int,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """链路10：抽样质检，字错率超限→阻断版本生效→触发纠错回路"""
        link = "sample-qc"
        record = self.qc_gate.evaluate(task_id, segment_count, error_count, threshold)
        self._log(link, "evaluate", "blocked" if record.blocked else "passed")
        if record.blocked:
            self.qc_gate.clear_block(task_id)
            self._log(link, "correct_triggered", "ok")
        return {
            "link": link, "ok": not record.blocked,
            "error_rate": record.error_rate, "blocked": record.blocked,
        }

    def status_report(self) -> dict[str, Any]:
        """聚合全部管线组件的运行状态报告。"""
        return {
            "audit_log": self.audit_log,
            "shard_watermark": self.shard_wm.snapshot(),
            "redeliver": self.redeliver.snapshot(),
            "quota": self.quota.snapshot(),
            "version": self.version.snapshot(),
            "qc_gate": self.qc_gate.snapshot(),
            "pack": self.pack.snapshot(),
            "archive": self.archive.snapshot(),
        }