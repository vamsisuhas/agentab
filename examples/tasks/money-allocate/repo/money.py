def allocate(total_cents, ratios):
    total_ratio = sum(ratios)
    return [round(total_cents * r / total_ratio) for r in ratios]
