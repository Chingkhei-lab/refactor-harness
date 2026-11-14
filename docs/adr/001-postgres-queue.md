# ADR 001: Use Postgres as the job queue

## Status

Accepted

## Context

The engine needs a durable job queue with at-least-once delivery, retries with
backoff, crash recovery, and observable state. The system already runs
PostgreSQL 16 (see `docker-compose.yml`), and the goal is to keep operational
complexity low for the initial version.

## Decision

Use PostgreSQL itself as the queue backend: jobs are rows in a `jobs` table,
claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, and scheduled via a
`run_after` column. No separate broker or worker framework is introduced.

## Options considered

- **Kafka**: high throughput, durable log, replay, fan-out. Requires running and
  operating a Kafka cluster (ZooKeeper/KRaft), topic management, consumer groups,
  and offset tracking. Overkill for current scale; adds significant infra.
- **Redis (lists/streams)**: low latency, simple primitives. Persistence and
  crash recovery are opt-in and weaker; no transactional coupling with business
  data; visibility timeouts and dead-letter handling must be hand-rolled.
- **Celery + broker (Redis/RabbitMQ)**: mature task framework, retries, routing,
  monitoring. Adds a framework dependency, a broker, result backend, and
  serialization concerns; harder to reason about exactly-once-ish side effects.
- **Postgres (chosen)**: already operated; transactional consistency with
  application data; `SKIP LOCKED` provides efficient claiming; leases/heartbeats
  give crash recovery; scheduling via `run_after`.

## Consequences

### Gained

- Single datastore to back up, monitor, and secure.
- Job state transitions are transactional with business writes (enqueue in same tx).
- Crash recovery via leases without extra infrastructure.
- Simple mental model: SQL rows, indexes, and queries.

### Given up

- **Throughput**: Postgres is not a high-throughput message broker; very high
  enqueue/dequeue rates will hit row contention and bloat.
- **Fan-out / routing**: no native pub/sub or topic routing; broadcast patterns
  require extra tables or polling.
- **Horizontal scale**: scaling workers is fine, but the database becomes the
  bottleneck; sharding/partitioning adds complexity.
- **Ecosystem**: fewer off-the-shelf dashboards, retry policies, and monitoring
  integrations than Celery/Kafka.
- **Latency**: polling-based dispatch adds delay; no push notifications.

## When to revisit

Re-evaluate if sustained throughput exceeds Postgres comfort (roughly tens of
thousands of jobs/minute on a single instance), if fan-out/routing requirements
grow, or if multi-region deployment is needed.
