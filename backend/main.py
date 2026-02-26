"""
O.D.I.N. — Orchestrated Dispatch & Inventory Network API

Application entry point. The app is fully assembled by the factory in
core/app.py — lifespan, middleware, CORS, module discovery, and route
registration all live there.

Uvicorn entry point: uvicorn main:app
"""

from core.app import create_app

app = create_app()
