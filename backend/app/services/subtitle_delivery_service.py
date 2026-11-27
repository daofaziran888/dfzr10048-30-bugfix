"""兼容命名：字幕包核验/领取服务。"""
from __future__ import annotations

from app.services.revision_delivery import RevisionDeliveryService, workflow


class SubtitleDeliveryService(RevisionDeliveryService):
    """装包、核验、确认、领取的兼容类名。"""


subtitle_delivery_service = workflow

__all__ = ["SubtitleDeliveryService", "RevisionDeliveryService", "subtitle_delivery_service", "workflow"]
