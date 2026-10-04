"""Persist this identity at launch; never infer past runs from the current checkout."""

HH_SEARCH_VERSION = "adaptive_v1"
HIREHI_SEARCH_V1 = "hirehi_v1"
HIREHI_SEARCH_ADAPTIVE_V3 = "hirehi_adaptive_v3"
HIREHI_SEARCH_VERSIONS = {HIREHI_SEARCH_V1, HIREHI_SEARCH_ADAPTIVE_V3}


def is_hirehi_adaptive(version: str | None) -> bool:
    return (version or HIREHI_SEARCH_ADAPTIVE_V3) == HIREHI_SEARCH_ADAPTIVE_V3
