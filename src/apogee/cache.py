import functools
from typing import Callable, Any, Dict
import pickle

from apogee import config

from diskcache import Cache

cache = Cache(config.CACHE_DIR)


def memoize(key=None, expire=None) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapped(*args: Any, **kwargs: Dict[str, Any]):
            nonlocal key
            if key is None:
                key = pickle.dumps((fn.__name__, args, kwargs))
            if key in cache:
                return cache[key]
            result = await fn(*args, **kwargs)
            cache.set(key, result, expire=expire)
            return result

        return wrapped

    return decorator
