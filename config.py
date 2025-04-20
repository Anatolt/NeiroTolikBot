import os
from dotenv import load_dotenv
from pathlib import Path

# Get the directory containing this file
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / '.env')

# API Keys
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not TELEGRAM_BOT_TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("Please set TELEGRAM_BOT_TOKEN and OPENROUTER_API_KEY in .env file")

# Model Configuration
DEFAULT_MODEL = "anthropic/claude-3-haiku"

# OpenRouter API Configuration
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
BOT_REFERER = "https://t.me/NeiroTolikBot"
BOT_TITLE = "NeiroTolikBot" 