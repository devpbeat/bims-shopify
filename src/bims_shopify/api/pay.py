"""Buyer-facing payment pages."""
from fastapi import APIRouter

router = APIRouter(prefix="/pay", tags=["pay"])
