"""路由汇总：语义化修订链路先注册，随后是所有模块共用的资源路由。"""
from __future__ import annotations

from app.routers import resource, revision_delivery

ROUTERS = [revision_delivery, resource]
