"""Topic administration and offset lookups for Redpanda (Kafka API). See ADR 0010."""

from __future__ import annotations

from typing import Any

from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient
from confluent_kafka.admin._config import AlterConfigOpType, ConfigEntry, ConfigResource
from confluent_kafka.cimpl import NewTopic

from vlsim.config import TransportConfig

HOUR_MS = 3_600_000
REPLICATION_FACTOR = 1  # single-broker Redpanda in dev and on the home server (ADR 0010)


def topic_configs(transport: TransportConfig, name: str) -> dict[str, str]:
    spec = transport.topics[name]
    return {
        "cleanup.policy": "delete",
        "retention.ms": str(spec.retention_hours * HOUR_MS),
        "retention.bytes": str(spec.retention_bytes_per_partition),
    }


def ensure_topics(bootstrap: str, transport: TransportConfig, names: list[str]) -> None:
    """Create missing topics; enforce retention on existing ones; refuse partition mismatch."""
    admin = AdminClient({"bootstrap.servers": bootstrap})
    existing = admin.list_topics(timeout=10).topics
    new = [
        NewTopic(
            n,
            transport.topics[n].partitions,
            REPLICATION_FACTOR,
            config=topic_configs(transport, n),
        )
        for n in names
        if n not in existing
    ]
    if new:
        for fut in admin.create_topics(new).values():
            fut.result()
    for n in names:
        if n in existing and len(existing[n].partitions) != transport.topics[n].partitions:
            raise RuntimeError(
                f"{n} has {len(existing[n].partitions)} partitions, config says "
                f"{transport.topics[n].partitions}; recreate the topic deliberately"
            )
    resources = [
        ConfigResource(
            ConfigResource.Type.TOPIC,
            n,
            incremental_configs=[
                ConfigEntry(k, v, incremental_operation=AlterConfigOpType.SET)
                for k, v in sorted(topic_configs(transport, n).items())
            ],
        )
        for n in names
    ]
    for fut in admin.incremental_alter_configs(resources).values():
        fut.result()


def end_offsets(bootstrap: str, names: list[str]) -> dict[str, dict[str, int]]:
    """High watermark per partition (JSON-friendly: partition numbers as str keys)."""
    consumer = Consumer(
        {"bootstrap.servers": bootstrap, "group.id": "vlsim-offsets", "enable.auto.commit": False}
    )
    try:
        meta = consumer.list_topics(timeout=10)
        out: dict[str, dict[str, int]] = {}
        for n in names:
            parts = sorted(meta.topics[n].partitions)
            out[n] = {
                str(p): consumer.get_watermark_offsets(TopicPartition(n, p), timeout=10)[1]
                for p in parts
            }
        return out
    finally:
        consumer.close()


def make_producer(bootstrap: str, transport: TransportConfig) -> Any:
    return Producer({"bootstrap.servers": bootstrap, **transport.producer})
