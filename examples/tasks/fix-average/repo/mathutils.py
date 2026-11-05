def average(nums):
    return sum(nums) / len(nums)


def median(nums):
    ordered = sorted(nums)
    return ordered[len(ordered) // 2]
