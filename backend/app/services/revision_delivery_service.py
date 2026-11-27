"""兼容模块名 revision_delivery_service。"""
from app.services.revision_delivery import *  # noqa: F401,F403
from app.services.revision_delivery import RevisionDeliveryService, workflow

revision_delivery_service = workflow
