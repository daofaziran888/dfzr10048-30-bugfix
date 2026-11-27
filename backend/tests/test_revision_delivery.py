"""纠错正文 → 修订重算 → 字幕包核验领取的回归测试。"""
from __future__ import annotations

import concurrent.futures

from app.services.revision_delivery import RevisionDeliveryService


def make_service(tmp_path) -> RevisionDeliveryService:
    return RevisionDeliveryService(artifact_dir=tmp_path / "subtitles")


def merge_whale(service: RevisionDeliveryService, cid: str = "CR-TEST-WHALE") -> dict:
    return service.merge_correction({
        "correction_id": cid,
        "version_ref": "V1",
        "original": "灰鲸",
        "corrected": "蓝鲸",
        "continued_from": "correction_loop",
    })


def test_whale_revision_is_traceable_and_preserves_parent(tmp_path):
    service = make_service(tmp_path)
    result = merge_whale(service)

    assert result["ok"]
    assert "蓝鲸" in result["content"]
    assert "灰鲸" not in result["content"]
    assert result["source"]["correction_id"] == "CR-TEST-WHALE"
    assert result["source"]["parent_version"] == "v1"
    assert result["recompute"]["status"] == "已完成"
    assert result["completion_basis"]["checksum"] == result["checksum"]

    old = service.download("V1")
    assert old["ok"]
    assert "灰鲸" in old["content"]
    assert old["verification"]["verified"]


def test_duplicate_reports_and_two_continuation_ends_create_one_revision(tmp_path):
    service = make_service(tmp_path)
    first = merge_whale(service, "CR-IDEMPOTENT")
    pack_id = first["pack_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: merge_whale(service, "CR-IDEMPOTENT"), range(4)))

    assert len({item["revision_id"] for item in results}) == 1
    assert len({item["recompute"]["recompute_id"] for item in results}) == 1

    from_correction = service.continue_work({
        "correction_id": "CR-IDEMPOTENT",
        "continued_from": "correction_loop",
    })
    from_pack = service.continue_work({
        "pack_id": pack_id,
        "continued_from": "subtitle_pack",
    })

    assert from_correction["revision_id"] == from_pack["revision_id"] == first["revision_id"]
    assert service.packs[pack_id]["continued_from"] == ["correction_loop", "subtitle_pack"]


def test_pending_pack_explains_source_then_confirm_allows_claim(tmp_path):
    service = make_service(tmp_path)
    result = merge_whale(service, "CR-CLAIM")
    pack_id = result["pack_id"]
    service.continue_work({"pack_id": pack_id, "continued_from": "subtitle_pack"})

    pending = service.claim_pack(pack_id, {"claimer": "receiver-1"})
    assert not pending["ok"]
    assert pending["status"] == "待确认"
    assert pending["last_continued_from"] == "subtitle_pack"
    assert "/confirm" in pending["how_to_claim"]
    assert "/claim" in pending["how_to_claim"]

    confirmed = service.confirm_pack(pack_id)
    claimed = service.claim_pack(pack_id, {"claimer": "receiver-1"})
    assert confirmed["ok"]
    assert claimed["ok"] and claimed["claimable"]
    assert "蓝鲸" in claimed["content"]
    assert "灰鲸" not in claimed["content"]
    assert claimed["revision"]["recompute"]["completion_basis"]


def test_stale_pack_residual_old_word_is_rejected(tmp_path):
    service = make_service(tmp_path)
    result = merge_whale(service, "CR-STALE")
    pack_id = result["pack_id"]
    service.generate_pack(pack_id, {"content": "1\n灰鲸残留旧包\n"})

    claimed = service.claim_pack(pack_id, {"confirm": True})
    assert not claimed["ok"]
    assert claimed["status"] == "正文和包对不上"
    assert "灰鲸" in claimed["pack"]["verification"]["reason"]

    service.continue_work({"pack_id": pack_id, "continued_from": "subtitle_pack"})
    confirmed = service.confirm_pack(pack_id)
    assert confirmed["ok"]


def test_second_edit_keeps_unrelated_lines(tmp_path):
    service = make_service(tmp_path)
    first = merge_whale(service, "CR-FIRST-LINE")
    second = service.merge_correction({
        "correction_id": "CR-SECOND-LINE",
        "version_ref": first["revision_id"],
        "original": "灯塔守住长夜",
        "corrected": "灯塔照亮长夜",
    })

    content = second["content"]
    assert "蓝鲸" in content
    assert "灯塔照亮长夜" in content
    assert "潮水翻动旧歌谣" in content
    assert "灰鲸" not in content
