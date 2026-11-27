"""修订传递、幂等续办、装包核验和领取的兼容 HTTP 接口。

通用资源路由仍保留；本路由把故事中会用到的语义化路径显式暴露出来，
并在路径命名上同时兼容下划线/连字符、纠错端/字幕包端两类入口。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.services.revision_delivery import workflow

router = APIRouter(prefix="/api", tags=["修订传递与字幕包领取"])


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    if isinstance(data, dict):
        return data
    return {"payload": data}


def _query(request: Request) -> dict[str, Any]:
    return {k: v for k, v in request.query_params.items()}


def _download_response(result: dict[str, Any]) -> Response:
    if not result.get("ok"):
        return JSONResponse(status_code=404, content=result)
    headers = {
        "X-Checksum": str(result.get("checksum", "")),
        "X-SHA256": str(result.get("sha256", "")),
        "X-Verified": "true" if result.get("verification", {}).get("verified", True) else "false",
    }
    filename = result.get("filename") or "subtitle.srt"
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(content=str(result.get("content", "")), media_type=result.get("media_type", "text/plain; charset=utf-8"), headers=headers)


# ----------------------------------------------------------------------
# 纠错回路：合入、重报、查询同一修订
# ----------------------------------------------------------------------
async def correction_merge(request: Request, ref: str | None = None) -> dict[str, Any]:
    data = await _body(request)
    if ref is not None:
        data.setdefault("correction_id", ref)
    data.setdefault("continued_from", "correction_loop")
    return workflow.merge_correction(data)


for path in [
    "/correction_loop/merge", "/correction_loop/merge_correction",
    "/correction-loop/merge", "/correction-loops/merge",
    "/correction_loops/merge", "/corrections/merge",
    "/revisions/merge", "/revision/merge",
]:
    router.add_api_route(path, correction_merge, methods=["POST"])

for path in [
    "/correction_loop/{ref}/merge", "/correction_loop/{ref}/merge_correction",
    "/correction-loop/{ref}/merge", "/corrections/{ref}/merge",
]:
    router.add_api_route(path, correction_merge, methods=["POST"])


async def correction_continue(request: Request, ref: str | None = None) -> dict[str, Any]:
    data = await _body(request)
    if ref is not None:
        data.setdefault("correction_id", ref)
    data.setdefault("continued_from", "correction_loop")
    return workflow.continue_work(data)


for path in [
    "/correction_loop/continue", "/correction_loop/resubmit",
    "/correction_loop/re_report", "/correction_loop/report",
    "/correction-loop/continue", "/correction-loops/continue",
    "/correction_loops/continue", "/corrections/continue",
]:
    router.add_api_route(path, correction_continue, methods=["POST"])

for path in [
    "/correction_loop/{ref}/continue", "/correction_loop/{ref}/resubmit",
    "/correction_loop/{ref}/re_report", "/correction_loop/{ref}/re-report",
    "/correction_loop/{ref}/report", "/correction-loop/{ref}/continue",
    "/corrections/{ref}/resubmit",
]:
    router.add_api_route(path, correction_continue, methods=["POST"])


def correction_revision(ref: str, request: Request) -> dict[str, Any]:
    return workflow.get_revision_view(ref, source="correction_loop")


for path in [
    "/correction_loop/{ref}/revision", "/correction_loop/{ref}/trace",
    "/correction_loop/{ref}/recompute", "/correction-loop/{ref}/revision",
    "/correction-loops/{ref}/revision", "/correction_loops/{ref}/revision",
    "/corrections/{ref}/revision",
]:
    router.add_api_route(path, correction_revision, methods=["GET"])


# ----------------------------------------------------------------------
# 转写版本：详情、正文、下载
# ----------------------------------------------------------------------
def revision_detail(ref: str, request: Request) -> dict[str, Any]:
    view = workflow.get_revision_view(ref)
    # 同时补入通用版本行，方便只认资源字段的调用方。
    row = workflow._find_store_row("transcript_version", ref)
    if row is not None:
        view["entry"] = row
        view["version"] = row
    return view


for path in [
    "/transcript_version/{ref}/revision", "/transcript_version/{ref}/detail",
    "/transcript_version/{ref}/trace", "/revisions/{ref}", "/revision/{ref}",
    "/versions/{ref}",
]:
    router.add_api_route(path, revision_detail, methods=["GET"])


def revision_content(ref: str, request: Request) -> dict[str, Any]:
    view = workflow.get_revision_view(ref)
    if not view.get("ok"):
        return JSONResponse(status_code=404, content=view)
    return {"ok": True, "ref": ref, "content": view.get("content"), "正文": view.get("content"), "checksum": view.get("checksum")}


for path in [
    "/transcript_version/{ref}/content", "/transcript_version/{ref}/body",
    "/revisions/{ref}/content", "/revisions/{ref}/body", "/versions/{ref}/content",
]:
    router.add_api_route(path, revision_content, methods=["GET"])


def revision_download(ref: str, request: Request) -> Response:
    return _download_response(workflow.download(ref, **_query(request)))


for path in [
    "/transcript_version/{ref}/download", "/revisions/{ref}/download",
    "/versions/{ref}/download",
]:
    router.add_api_route(path, revision_download, methods=["GET"])


# ----------------------------------------------------------------------
# 字幕包：继续生成、核验确认、领取、查询、下载
# ----------------------------------------------------------------------
async def pack_generate(request: Request, ref: str | None = None) -> dict[str, Any]:
    data = await _body(request)
    q = _query(request)
    data = {**q, **data}
    if ref is not None:
        data.setdefault("pack_id", ref)
    return workflow.generate_pack(data.get("pack_id"), data)


for path in ["/subtitle_pack/generate", "/subtitle-pack/generate", "/packs/generate"]:
    router.add_api_route(path, pack_generate, methods=["POST"])
router.add_api_route("/subtitle_pack/{ref}/generate", pack_generate, methods=["POST"])
router.add_api_route("/packs/{ref}/generate", pack_generate, methods=["POST"])


async def pack_continue(request: Request, ref: str | None = None) -> dict[str, Any]:
    data = await _body(request)
    data = {**_query(request), **data}
    if ref is not None:
        data.setdefault("pack_id", ref)
    data.setdefault("continued_from", "subtitle_pack")
    return workflow.continue_work(data)


for path in [
    "/subtitle_pack/continue", "/subtitle_pack/resume",
    "/subtitle-pack/continue", "/packs/continue",
]:
    router.add_api_route(path, pack_continue, methods=["POST"])

for path in [
    "/subtitle_pack/{ref}/continue", "/subtitle_pack/{ref}/resume",
    "/subtitle-pack/{ref}/continue", "/packs/{ref}/continue",
]:
    router.add_api_route(path, pack_continue, methods=["POST"])


async def pack_verify(request: Request, ref: str | None = None) -> dict[str, Any]:
    data = await _body(request)
    data = {**_query(request), **data}
    if ref is not None:
        data.setdefault("pack_id", ref)
    return workflow.verify_pack(data.get("pack_id"), **data)


for path in [
    "/subtitle_pack/{ref}/verify", "/subtitle_pack/{ref}/verification",
    "/subtitle-pack/{ref}/verify", "/packs/{ref}/verify",
]:
    router.add_api_route(path, pack_verify, methods=["GET", "POST"])


async def pack_confirm(request: Request, ref: str | None = None) -> dict[str, Any]:
    data = await _body(request)
    data = {**_query(request), **data}
    if ref is not None:
        data.setdefault("pack_id", ref)
    return workflow.confirm_pack(data.get("pack_id"), data)


for path in [
    "/subtitle_pack/confirm", "/subtitle_pack/confirmation",
    "/subtitle-pack/confirm", "/packs/confirm",
    "/subtitle_pack/{ref}/confirm", "/subtitle_pack/{ref}/confirmation",
    "/subtitle-pack/{ref}/confirm", "/packs/{ref}/confirm",
]:
    router.add_api_route(path, pack_confirm, methods=["POST"])


async def pack_claim(request: Request, ref: str | None = None) -> dict[str, Any]:
    data = await _body(request)
    data = {**_query(request), **data}
    if ref is not None:
        data.setdefault("pack_id", ref)
    return workflow.claim_pack(data.get("pack_id"), data)


for path in [
    "/subtitle_pack/claim", "/subtitle_pack/receive",
    "/subtitle-pack/claim", "/packs/claim",
    "/subtitle_pack/{ref}/claim", "/subtitle_pack/{ref}/receive",
    "/subtitle-pack/{ref}/claim", "/packs/{ref}/claim",
]:
    router.add_api_route(path, pack_claim, methods=["POST"])


def pack_revision(ref: str, request: Request) -> dict[str, Any]:
    result = workflow.get_revision_view(ref, source="subtitle_pack")
    pack = workflow.resolve_pack(ref)
    if pack is not None:
        result["pack"] = pack
        result.setdefault("recompute", pack.get("revision", {}).get("recompute") if isinstance(pack.get("revision"), dict) else None)
    return result


for path in [
    "/subtitle_pack/{ref}/revision", "/subtitle_pack/{ref}/trace",
    "/subtitle_pack/{ref}/recompute", "/subtitle-pack/{ref}/revision",
    "/packs/{ref}/revision", "/packs/{ref}/trace",
]:
    router.add_api_route(path, pack_revision, methods=["GET"])


def pack_content(ref: str, request: Request) -> dict[str, Any]:
    result = workflow.download(ref)
    if not result.get("ok"):
        return JSONResponse(status_code=404, content=result)
    return {"ok": True, "ref": ref, "content": result["content"], "正文": result["content"], "checksum": result["checksum"], "verification": result.get("verification")}


for path in [
    "/subtitle_pack/{ref}/content", "/subtitle_pack/{ref}/body",
    "/packs/{ref}/content",
]:
    router.add_api_route(path, pack_content, methods=["GET"])


def pack_download(ref: str, request: Request) -> Response:
    return _download_response(workflow.download(ref, **_query(request)))


for path in [
    "/subtitle_pack/{ref}/download", "/subtitle-pack/{ref}/download",
    "/packs/{ref}/download",
]:
    router.add_api_route(path, pack_download, methods=["GET"])
