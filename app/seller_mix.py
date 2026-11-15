"""One combined 15% allowance for realtors and unclassified authors."""

REALTOR_BASE_PERCENT = 15
REALTOR_MAX_PERCENT = 15


def realtor_allowance(ordinary_count: int, target: int, *, base_allowance: int | None = None) -> int:
    """ordinary_count means confirmed owners; never exceed 15% of actual stock."""
    del base_allowance
    owners, target = max(0, ordinary_count), max(0, target)
    return min(target * 15 // 100, owners * 15 // 85)
