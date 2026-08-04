import asyncio
import os
from decimal import Decimal
from typing import Any

import asyncpg


def _to_jsonable(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


class Database:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        dsn = os.environ["DATABASE_URL"]
        self._pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=2,
            max_size=int(os.environ.get("DB_POOL_MAX", "20")),
            command_timeout=10,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() not called")
        return self._pool

    async def list_customers(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT c.id, c.name, c.tier, c.risk,
                   a.balance, a.target
            FROM customers c
            JOIN accounts a ON a.customer_id = c.id
            ORDER BY c.id
            """
        )
        return [_to_jsonable(r) for r in rows]  # type: ignore[misc]

    async def get_customer(self, customer_id: int) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT c.id, c.name, c.tier, c.risk,
                   a.balance, a.target
            FROM customers c
            JOIN accounts a ON a.customer_id = c.id
            WHERE c.id = $1
            """,
            customer_id,
        )
        return _to_jsonable(row)

    async def get_balance(self, customer_id: int) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT customer_id, balance, target, updated_at FROM accounts WHERE customer_id = $1",
            customer_id,
        )
        return _to_jsonable(row)

    async def credit_account(
        self,
        customer_id: int,
        amount: float,
        tx_id: str,
        agent_id: str,
        execution_run_id: int,
        agent_slot: int | None = None,
    ) -> dict[str, Any]:
        # INSERT ... ON CONFLICT DO NOTHING is the per-run idempotency gate.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """
                    INSERT INTO transactions
                        (execution_run_id, tx_id, customer_id, amount, agent_id, agent_slot)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (execution_run_id, tx_id) DO NOTHING
                    RETURNING tx_id
                    """,
                    execution_run_id,
                    tx_id,
                    customer_id,
                    Decimal(str(amount)),
                    agent_id,
                    agent_slot,
                )
                if inserted is None:
                    balance = await conn.fetchval(
                        "SELECT balance FROM accounts WHERE customer_id = $1",
                        customer_id,
                    )
                    return {
                        "applied": False,
                        "tx_id": tx_id,
                        "customer_id": customer_id,
                        "balance": float(balance) if balance is not None else None,
                    }
                balance = await conn.fetchval(
                    """
                    UPDATE accounts
                    SET balance = LEAST(target, balance + $2),
                        updated_at = now()
                    WHERE customer_id = $1
                    RETURNING balance
                    """,
                    customer_id,
                    Decimal(str(amount)),
                )
                return {
                    "applied": True,
                    "tx_id": tx_id,
                    "customer_id": customer_id,
                    "balance": float(balance),
                }

    async def listen_transactions(
        self, queue: asyncio.Queue, stop_event: asyncio.Event
    ) -> None:
        """Dedicated connection running `LISTEN tx_committed`, pushing each
        payload onto `queue`. Auto-reconnects until `stop_event` is set."""
        dsn = os.environ["DATABASE_URL"]
        loop = asyncio.get_event_loop()
        while not stop_event.is_set():
            conn = None
            try:
                conn = await asyncpg.connect(dsn=dsn)

                def _on_notify(_conn, _pid, _channel, payload):
                    loop.call_soon_threadsafe(queue.put_nowait, payload)

                await conn.add_listener("tx_committed", _on_notify)
                await stop_event.wait()
                return
            except Exception:
                # Connection dropped, retry with a small backoff.
                await asyncio.sleep(1.0)
            finally:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass

    async def count_transactions(self, execution_run_id: int) -> int:
        """Total credits committed under this run — authoritative for the
        UI's `applied_total`, even if a workflow crashes before report_done."""
        val = await self.pool.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE execution_run_id = $1",
            execution_run_id,
        )
        return int(val or 0)

    async def get_transaction_ids(self, execution_run_id: int) -> set[str]:
        """Return the set of tx_ids that have committed for this run. Used
        by the orchestrator's reconciliation step to detect ghosts —
        tx_ids the orchestrator believes applied but that aren't in the DB."""
        rows = await self.pool.fetch(
            "SELECT tx_id FROM transactions WHERE execution_run_id = $1",
            execution_run_id,
        )
        return {r["tx_id"] for r in rows}

    async def current_execution_run(self) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT id, started_at, ended_at, customers, credits_per_customer, target
            FROM execution_runs
            ORDER BY id DESC
            LIMIT 1
            """
        )
        if row is None:
            raise RuntimeError("no execution_runs row — schema seed missing")
        return _to_jsonable(row)  # type: ignore[return-value]

    async def start_execution_run(
        self,
        customers: int,
        credits_per_customer: int,
        target: float,
    ) -> dict[str, Any]:
        """Mark any open run as ended, insert a new run, reset balances to the
        per-customer starting point (target - credits_per_customer)."""
        start_balance = target - credits_per_customer
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE execution_runs SET ended_at = now() WHERE ended_at IS NULL"
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO execution_runs (customers, credits_per_customer, target)
                    VALUES ($1, $2, $3)
                    RETURNING id, started_at, ended_at, customers, credits_per_customer, target
                    """,
                    customers,
                    credits_per_customer,
                    Decimal(str(target)),
                )
                await conn.execute(
                    """
                    UPDATE accounts
                    SET balance = $1, target = $2, updated_at = now()
                    """,
                    Decimal(str(start_balance)),
                    Decimal(str(target)),
                )
        return _to_jsonable(row)  # type: ignore[return-value]
