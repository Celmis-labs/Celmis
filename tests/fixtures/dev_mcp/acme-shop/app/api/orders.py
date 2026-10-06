"""HTTP routes for orders."""

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.db import build_engine
from app.services.orders import OrderService

router = APIRouter()
_service = OrderService(build_engine())


@router.post("/orders")
def post_order(total_cents: int, user: dict = Depends(get_current_user)) -> dict:
    """Create an order for the authenticated user."""
    order = _service.create_order(user["sub"], total_cents)
    return {"id": order.id, "total_cents": order.total_cents}


@router.get("/orders/{order_id}")
def get_order(order_id: int, user: dict = Depends(get_current_user)) -> dict:
    order = _service.get_order(order_id)
    return {"id": order.id} if order else {}
