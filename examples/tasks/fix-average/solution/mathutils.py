def average(nums):
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def median(nums):
    ordered = sorted(nums)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 0:
        return (ordered[mid - 1] + ordered[mid]) / 2
    return ordered[mid]
