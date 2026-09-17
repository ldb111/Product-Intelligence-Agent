"""Event Core 的最小工作流接口。"""

from .dedup import deduplicate_event
from .detector import detect_event
from .schemas import DetectionInput, DetectionResult, EventCandidate

__all__ = ["detect_event", "deduplicate_event", "DetectionInput", "DetectionResult", "EventCandidate"]
