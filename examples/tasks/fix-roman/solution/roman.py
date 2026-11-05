VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def to_int(numeral):
    numeral = numeral.upper()
    if not numeral or any(ch not in VALUES for ch in numeral):
        raise ValueError(f"not a roman numeral: {numeral!r}")
    total = 0
    for i, ch in enumerate(numeral):
        value = VALUES[ch]
        if i + 1 < len(numeral) and VALUES[numeral[i + 1]] > value:
            total -= value
        else:
            total += value
    return total
