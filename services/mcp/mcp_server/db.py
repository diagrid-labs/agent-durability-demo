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
    ) -> dict[str, Any]:
        # Single SQL transaction. INSERT … ON CONFLICT DO NOTHING is the
        # idempotency gate — if a previous invocation already wrote this tx_id
        # (workflow replay after a kill), the INSERT returns nothing and we
        # skip the UPDATE, returning the existing balance.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """
                    INSERT INTO transactions (tx_id, customer_id, amount, agent_id)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (tx_id) DO NOTHING
                    RETURNING tx_id
                    """,
                    tx_id,
                    customer_id,
                    Decimal(str(amount)),
                    agent_id,
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
