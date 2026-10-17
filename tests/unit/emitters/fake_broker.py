"""A fake confluent_kafka.Producer: deliveries happen on poll()/flush(); `down` makes the
broker unreachable (messages wait, then fail once they exceed `timeout_polls`)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeError:
    def __init__(self, msg: str) -> None:
        self.msg = msg

    def str(self) -> str:
        return self.msg


class FakeMessage:
    def __init__(self, topic: str, key: bytes, value: bytes) -> None:
        self._t, self._k, self._v = topic, key, value

    def topic(self) -> str:
        return self._t

    def key(self) -> bytes:
        return self._k

    def value(self) -> bytes:
        return self._v


class FakeProducer:
    def __init__(self, timeout_polls: int = 3) -> None:
        self.down = False
        self.timeout_polls = timeout_polls
        self.queue: list[list[Any]] = []  # [msg, callback, age]
        self.log: list[tuple[str, bytes, bytes]] = []  # what the broker durably holds

    def produce(
        self, topic: str, key: bytes, value: bytes, on_delivery: Callable[..., None]
    ) -> None:
        self.queue.append([FakeMessage(topic, key, value), on_delivery, 0])

    def __len__(self) -> int:
        return len(self.queue)

    def poll(self, timeout: float = 0) -> int:
        served, keep = 0, []
        for item in self.queue:
            msg, cb, age = item
            if not self.down:
                self.log.append((msg.topic(), msg.key(), msg.value()))
                cb(None, msg)
                served += 1
            elif age + 1 >= self.timeout_polls:
                cb(FakeError("Local: Message timed out"), msg)
                served += 1
            else:
                item[2] = age + 1
                keep.append(item)
        self.queue = keep
        return served

    def flush(self, timeout: float = 0) -> int:
        self.poll(timeout)
        return len(self.queue)
