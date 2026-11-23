from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'
    ZONE_LEAD = 'zone_lead'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class Dispatch(StrEnum):
    """整改派单状态：开出 → 区线签收 → 复验签署撤下。"""
    OPEN = 'open'
    ACKNOWLEDGED = 'acknowledged'
    CLOSED = 'closed'


class Handover(StrEnum):
    """交接事件类型，写入交接事件表并作为断点续传的游标单位。"""
    OPENED = 'opened'
    ACKNOWLEDGED = 'acknowledged'
    CLOSED = 'closed'
