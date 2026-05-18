"""
WhatsApp Bot Dashboard — FastAPI Entry Point
"""
import os
import asyncio
import logging
import yaml

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from routes import chats, bot, marketing, models, training
from db import sql_query, bridge_query, INSTANCE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("dashboard")

app = FastAPI(title="WhatsApp Bot Dashboard")

# Static files + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Include route modules
app.include_router(chats.router)
app.include_router(bot.router)
app.include_router(marketing.router)
app.include_router(models.router)
app.include_router(training.router)

# Shared config
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "cleaner")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/context.yaml")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Dashboard home page with live statistics."""
    # Count chats
    try:
        chats_data = bridge_query("SELECT COUNT(*) as count FROM chats")
        chat_count = chats_data[0]["count"] if chats_data else 0
    except Exception:
        chat_count = 0
        
    # Count training chunks
    try:
        chunks = sql_query(f"SELECT COUNT(*) as count FROM {INSTANCE_NAME}_training_chunks")
        chunk_count = chunks[0]["count"] if chunks else 0
    except Exception:
        chunk_count = 0
        
    # Count appointments
    try:
        apps = sql_query(f"SELECT COUNT(*) as count FROM {INSTANCE_NAME}_appointments")
        app_count = apps[0]["count"] if apps else 0
    except Exception:
        app_count = 0
        
    # Loaded model
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
        loaded_model = config.get("llm", {}).get("model", "local-model")
    except Exception:
        loaded_model = "local-model"

    return templates.TemplateResponse(request, "base.html", {
        "page": "home",
        "instance_name": INSTANCE_NAME,
        "chat_count": chat_count,
        "chunk_count": chunk_count,
        "app_count": app_count,
        "loaded_model": loaded_model,
    })
