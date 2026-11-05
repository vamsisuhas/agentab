from fractions import Fraction


def allocate(total_cents, ratios):
    if isinstance(total_cents, bool) or not isinstance(total_cents, int):
        raise TypeError("total_cents must be an int")
    if not ratios or any(r < 0 for r in ratios) or sum(ratios) == 0:
        raise ValueError("ratios must be non-empty, non-negative and not all zero")

    sign = -1 if total_cents < 0 else 1
    amount = abs(total_cents)
    fractions = [Fraction(r) for r in ratios]
    total_ratio = sum(fractions)
    exact = [amount * f / total_ratio for f in fractions]
    parts = [int(e) for e in exact]  # floor, since everything is non-negative
    leftover = amount - sum(parts)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in order[:leftover]:
        parts[i] += 1
    return [sign * p for p in parts]
