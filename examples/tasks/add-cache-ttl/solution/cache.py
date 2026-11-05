import time


class Cache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._data = {}

    def set(self, key, value, ttl=None):
        expires = None if ttl is None else self._clock() + ttl
        self._data[key] = (value, expires)

    def _expired(self, expires):
        return expires is not None and self._clock() >= expires

    def get(self, key, default=None):
        if key not in self._data:
            return default
        value, expires = self._data[key]
        if self._expired(expires):
            del self._data[key]
            return default
        return value

    def __len__(self):
        return sum(1 for _, expires in self._data.values() if not self._expired(expires))
