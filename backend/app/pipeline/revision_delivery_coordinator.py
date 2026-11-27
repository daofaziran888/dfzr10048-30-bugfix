"""兼容命名：修订交付协调器。"""
from app.services.revision_delivery import RevisionDeliveryService, workflow


class RevisionDeliveryCoordinator(RevisionDeliveryService):
    """供按 Coordinator 命名编写的调用方使用。"""


coordinator = workflow

__all__ = ["RevisionDeliveryCoordinator", "RevisionDeliveryService", "coordinator", "workflow"]
