from __future__ import annotations

import math
import random
from dataclasses import dataclass

from backend.config import settings


@dataclass(frozen=True)
class Point:
    x: float
    y: float


class BezierPointer:
    def __init__(self, seed: int | None = None) -> None:
        self.random = random.Random(seed)
        self.current = Point(1, 1)

    def path(self, target: Point, viewport: tuple[int, int], steps: int | None = None) -> list[Point]:
        width, height = viewport
        start = self.current
        distance = math.dist((start.x, start.y), (target.x, target.y))
        count = steps or max(8, min(48, round(distance / 18)))
        bend = min(120, distance * 0.25)
        c1 = Point(start.x + (target.x - start.x) * 0.3, start.y + self.random.uniform(-bend, bend))
        c2 = Point(start.x + (target.x - start.x) * 0.7, target.y + self.random.uniform(-bend, bend))
        points = []
        for i in range(1, count + 1):
            t = i / count
            u = 1 - t
            x = u**3 * start.x + 3 * u**2 * t * c1.x + 3 * u * t**2 * c2.x + t**3 * target.x
            y = u**3 * start.y + 3 * u**2 * t * c1.y + 3 * u * t**2 * c2.y + t**3 * target.y
            points.append(Point(max(0, min(width - 1, x)), max(0, min(height - 1, y))))
        self.current = points[-1]
        return points

    async def move_to_locator(self, page, locator) -> None:
        await locator.scroll_into_view_if_needed()
        if not await locator.is_visible() or not await locator.is_enabled():
            raise RuntimeError("Элемент невидим или неактивен")
        box = await locator.bounding_box()
        if not box:
            raise RuntimeError("Не удалось определить положение элемента")
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        target = Point(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        path = self.path(target, (viewport["width"], viewport["height"]))
        if settings.pointer_overlay:
            await page.evaluate(
                """() => {
                    if (!document.getElementById('jao-pointer')) {
                        const dot = document.createElement('div');
                        dot.id = 'jao-pointer';
                        Object.assign(dot.style, {
                            position: 'fixed', width: '12px', height: '12px',
                            borderRadius: '50%', background: '#b9f36a',
                            border: '2px solid #17201d', zIndex: '2147483647',
                            pointerEvents: 'none', transform: 'translate(-50%, -50%)'
                        });
                        document.documentElement.appendChild(dot);
                    }
                }"""
            )
        for point in path:
            await page.mouse.move(point.x, point.y)
            if settings.pointer_overlay:
                await page.evaluate(
                    "([x,y]) => { const d=document.getElementById('jao-pointer');"
                    "if(d){d.style.left=x+'px';d.style.top=y+'px';} }",
                    [point.x, point.y],
                )
