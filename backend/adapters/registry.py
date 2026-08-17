from backend.adapters.hh.adapter import HHAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        adapter = HHAdapter()
        self._items = {adapter.site_id: adapter}

    def get(self, site_id: str):
        if site_id not in self._items:
            raise KeyError(f"Неизвестный адаптер: {site_id}")
        return self._items[site_id]

    def manifests(self) -> list[dict]:
        return [adapter.manifest.model_dump() for adapter in self._items.values()]


adapter_registry = AdapterRegistry()
