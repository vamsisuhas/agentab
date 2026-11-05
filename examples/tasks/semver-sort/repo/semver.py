def parse(version):
    core, _, build = version.partition("+")
    core, _, prerelease = core.partition("-")
    major, minor, patch = core.split(".")
    return int(major), int(minor), int(patch), prerelease or None, build or None


def compare(a, b):
    left = [int(x) for x in a.split(".")]
    right = [int(x) for x in b.split(".")]
    return (left > right) - (left < right)


def sort_versions(versions):
    import functools
    return sorted(versions, key=functools.cmp_to_key(compare))
