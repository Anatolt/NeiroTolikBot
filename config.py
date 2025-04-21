# Bot Configuration
BOT_CONFIG = {
    # API Keys (loaded from .env)
    "TELEGRAM_BOT_TOKEN": None,  # Will be loaded from .env
    "OPENROUTER_API_KEY": None,  # Will be loaded from .env
    "PIAPI_KEY": None,  # Will be loaded from .env
    
    # Bot Settings
    "BOT_TITLE": "NeiroTolikBot",
    "BOT_REFERER": "https://t.me/NeiroTolikBot",
    
    # API Settings
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "DEFAULT_MODEL": "qwen/qwen2.5-vl-3b-instruct:free",  # Полный идентификатор модели
    "CUSTOM_SYSTEM_PROMPT": None,  # Will be loaded from .env
    
    # Image Generation Settings
    "IMAGE_GENERATION": {
        "MODEL": "Qubico/flux1-schnell",
        "TASK_TYPE": "txt2img",
        "NEGATIVE_PROMPT": "ugly, blurry, bad quality, distorted",
        "ASPECT_RATIO": "square",
        "MAX_ATTEMPTS": 60,
        "POLLING_INTERVAL": 2
    },
    
    # Available Models
    "MODELS": {
        "claude": "anthropic/claude-3-haiku",  # Основная модель по умолчанию
        "claude_opus": "anthropic/claude-3-opus",  # Для сложных задач
        "claude_sonnet": "anthropic/claude-3-sonnet",  # Для баланса скорости и качества
        "chatgpt": "openai/gpt-4-turbo",  # ChatGPT модель
        "mistral": "mistralai/mistral-large-2407",  # Альтернативная модель
        "llama": "meta-llama/llama-3.1-8b-instruct:free",  # Бесплатная модель
        "meta": "meta-llama/llama-3.1-8b-instruct:free",  # Бесплатная модель
        "deepseek": "deepseek/deepseek-r1-distill-qwen-14b",  # Модель DeepSeek
        "qwen": "qwen/qwen2.5-vl-3b-instruct:free",  # Модель Qwen
        "fimbulvetr": "sao10k/fimbulvetr-11b-v2",  # Модель Fimbulvetr
        "sao10k": "sao10k/fimbulvetr-11b-v2"  # Модель Fimbulvetr
    },
    
    # Text Generation Settings
    "TEXT_GENERATION": {
        "MAX_TOKENS": 1000,
        "TEMPERATURE": 0.7
    },
    
    # Keywords for routing
    "KEYWORDS": {
        "IMAGE": ["нарисуй", "картинка", "изображение", "сгенерируй картинку", "generate image", "draw", "picture", "image", "generate"],
        "CAPABILITIES": ["что ты умеешь", "твои возможности", "помощь", "справка", "help"]
    }
}
