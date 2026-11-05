import functools
import re

_NUM = r"0|[1-9]\d*"
_PRE_ID = r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
_BUILD_ID = r"[0-9a-zA-Z-]+"
_SEMVER = re.compile(
    rf"^({_NUM})\.({_NUM})\.({_NUM})"
    rf"(?:-({_PRE_ID}(?:\.{_PRE_ID})*))?"
    rf"(?:\+({_BUILD_ID}(?:\.{_BUILD_ID})*))?$"
)


def parse(version):
    match = _SEMVER.match(version) if isinstance(version, str) else None
    if not match:
        raise ValueError(f"invalid semantic version: {version!r}")
    major, minor, patch, prerelease, build = match.groups()
    return int(major), int(minor), int(patch), prerelease, build


def _key(version):
    major, minor, patch, prerelease, _ = parse(version)
    if prerelease is None:
        pre = (1,)
    else:
        pre = (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                        for p in prerelease.split(".")))
    return (major, minor, patch, pre)


def compare(a, b):
    ka, kb = _key(a), _key(b)
    return (ka > kb) - (ka < kb)


def sort_versions(versions):
    return sorted(versions, key=functools.cmp_to_key(compare))
