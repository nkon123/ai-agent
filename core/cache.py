"""TTL + 최대 개수(LRU) 캐시.

사용법:
    from core.cache import TTLCache, cached

    CACHE = TTLCache(ttl=300, maxsize=64)
    CACHE.set("k", value); CACHE.get("k")

    @cached(ttl=600, key=lambda root, name: root)   # name 은 키에서 제외
    def scan_files(root: str, name: str) -> list[str]: ...

캐시 키를 어떻게 잡을지가 이 모듈의 핵심이다.
'무엇이 비싼가'를 기준으로 정할 것. 예를 들어 파일 스캔이 무겁고
그 결과가 name 인자와 무관하다면, name 을 뺀 키로 캐시해야
name 이 바뀔 때마다 재스캔하지 않는다.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, Hashable, TypeVar

T = TypeVar("T")

_MISS = object()   # None 을 정상 값으로 캐시할 수 있어야 하므로 별도 센티널


class TTLCache:
    """만료 시간과 최대 개수를 가진 캐시. 스레드 안전.

    Flask 는 요청마다 다른 스레드에서 돈다. 락 없이 dict 를 쓰면
    순회 중 변경으로 터지거나 항목이 유실된다.
    """

    def __init__(self, ttl: float = 300.0, maxsize: int = 128) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            item = self._data.get(key, _MISS)
            if item is _MISS:
                self.misses += 1
                return default
            expires, value = item  # type: ignore[misc]
            if expires < time.monotonic():
                del self._data[key]     # 만료분은 조회 시점에 정리한다
                self.misses += 1
                return default
            self._data.move_to_end(key)  # LRU 갱신
            self.hits += 1
            return value

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)   # 가장 오래 안 쓴 것부터

    def pop(self, key: Hashable) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = self.misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "ttl": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


def cached(
    ttl: float = 300.0,
    maxsize: int = 128,
    key: Callable[..., Hashable] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """함수 결과 캐시 데코레이터.

    key: 인자에서 캐시 키를 만드는 함수. 지정하지 않으면 전체 인자를
         키로 쓴다. 비싼 부분과 무관한 인자는 key 로 반드시 걸러낼 것.
         (그러지 않으면 캐시가 있으나 마나 해진다.)

    반환된 함수에 .cache 로 TTLCache 인스턴스가 붙는다. 테스트나
    수동 무효화(clear)에 쓴다.
    """

    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        store = TTLCache(ttl=ttl, maxsize=maxsize)

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            k: Hashable = (
                key(*args, **kwargs)
                if key
                else (args, tuple(sorted(kwargs.items())))
            )
            hit = store.get(k, _MISS)
            if hit is not _MISS:
                return hit  # type: ignore[return-value]
            value = fn(*args, **kwargs)
            store.set(k, value)
            return value

        wrapper.cache = store  # type: ignore[attr-defined]
        return wrapper

    return deco
