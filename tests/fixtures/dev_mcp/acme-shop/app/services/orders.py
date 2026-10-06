"""Order creation."""

from dataclasses import dataclass


@dataclass
class Order:
    id: int
    user_id: str
    total_cents: int


class OrderService:
    """Creates and reads orders."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def create_order(self, user_id: str, total_cents: int) -> Order:
        """Insert an order and return it."""
        with self.engine.begin() as conn:
            row = conn.execute(
                "INSERT INTO orders (user_id, total_cents) VALUES (%s, %s) RETURNING id",
                (user_id, total_cents),
            ).one()
        return Order(id=row[0], user_id=user_id, total_cents=total_cents)

    def get_order(self, order_id: int) -> Order | None:
        with self.engine.connect() as conn:
            row = conn.execute("SELECT id, user_id, total_cents FROM orders WHERE id = %s",
                               (order_id,)).first()
        return Order(*row) if row else None
