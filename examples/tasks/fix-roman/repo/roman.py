VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def to_int(numeral):
    total = 0
    for ch in numeral:
        total += VALUES[ch]
    return total
