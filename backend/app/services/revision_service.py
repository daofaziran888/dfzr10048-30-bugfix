"""兼容命名：部分测试或脚本可能把修订服务称作 RevisionService。"""
from __future__ import annotations

from app.services.revision_delivery import RevisionDeliveryService, workflow


class RevisionService(RevisionDeliveryService):
    """修订、重算与交付服务的兼容类名。"""


revision_service = workflow

__all__ = ["RevisionService", "RevisionDeliveryService", "revision_service", "workflow"]
