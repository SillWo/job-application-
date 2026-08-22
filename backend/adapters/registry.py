from collections.abc import Callable

from backend.adapters.hh.adapter import HHAdapter
from backend.adapters.hirehi.adapter import HireHiAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], object]] = {
            HHAdapter.site_id: HHAdapter,
            HireHiAdapter.site_id: HireHiAdapter,
        }
        self._manifests = {
            site_id: factory().manifest for site_id, factory in self._factories.items()
        }

    def get(self, site_id: str):
        if site_id not in self._factories:
            raise KeyError(f"Неизвестный адаптер: {site_id}")
        return self._factories[site_id]()

    def manifests(self) -> list[dict]:
        return [manifest.model_dump() for manifest in self._manifests.values()]


adapter_registry = AdapterRegistry()
