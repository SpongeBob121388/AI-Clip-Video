"""Renderer Protocol 接口定义。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models.media import MediaProbe
from app.models.render_plan import RenderJob, RenderPlan


@runtime_checkable
class RendererProtocol(Protocol):
    async def probe(self, file_path: str) -> MediaProbe: ...

    async def render(self, plan: RenderPlan) -> RenderJob: ...

    async def extract_audio(
        self, input_path: str, track_index: int, output_path: str
    ) -> str: ...
