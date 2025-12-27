# Bot Configuration
BOT_CONFIG = {
    # API Keys (loaded from .env)
    "TELEGRAM_BOT_TOKEN": None,  # Will be loaded from .env
    "DISCORD_BOT_TOKEN": None,  # Will be loaded from .env
    "OPENROUTER_API_KEY": None,  # Will be loaded from .env
    "PIAPI_KEY": None,  # Will be loaded from .env
    "OPENAI_API_KEY": None,  # Will be loaded from .env
    
    # Bot Settings
    "BOT_TITLE": "NeiroTolikBot",
    "BOT_REFERER": "https://t.me/NeiroTolikBot",
    
    # API Settings
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "DEFAULT_MODEL": "deepseek/deepseek-r1-distill-qwen-14b",  # Базовая DeepSeek
    "ROUTER_MODEL": "openai/gpt-4o-mini",  # Легкая модель для сортировки запросов
    "ROUTING_MODE": "rules",  # rules | llm
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
    "IMAGE_MODELS": [
        "Qubico/flux1-schnell",
        "gpt-4o-image",
        "gemini-2.5-flash-image",
        "nano-banana-pro",
    ],

    # Voice Recognition Settings
    "VOICE_MODELS": [
        "whisper-1",
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe",
    ],
    "VOICE_MODEL": "whisper-1",

    # Admin
    "ADMIN_PASS": None,
    "BOOT_TIME": None,

    # Available Models
    "MODELS": {
        "claude": "anthropic/claude-3-haiku",  # Основная модель по умолчанию
        "claude_opus": "anthropic/claude-3-opus",  # Для сложных задач
        "claude_sonnet": "anthropic/claude-3-sonnet",  # Для баланса скорости и качества
        "chatgpt": "openai/gpt-4-turbo",  # ChatGPT модель
        "gpt4": "openai/gpt-4-turbo",  # Алиас на GPT-4
        "gpt3": "openai/gpt-3.5-turbo",  # Алиас на GPT-3.5
        "gpt5": "openai/gpt-4o",  # Алиас на самую мощную доступную модель
        "gpt": "openai/gpt-4o-mini",  # Обобщенный алиас на GPT
        "mistral": "mistralai/mistral-large-2407",  # Альтернативная модель
        "llama": "meta-llama/llama-3.3-70b-instruct:free",  # Бесплатная модель (актуальная)
        "meta": "meta-llama/llama-3.3-70b-instruct:free",  # Бесплатная модель (актуальная)
        "deepseek": "deepseek/deepseek-r1-distill-qwen-14b",  # Модель DeepSeek
        "qwen": "qwen/qwen2.5-vl-3b-instruct:free",  # Модель Qwen
        "gemini": "google/gemini-2.0-flash-exp:free",  # Модель Gemini
        "fimbulvetr": "sao10k/fimbulvetr-11b-v2",  # Модель Fimbulvetr
        "sao10k": "sao10k/fimbulvetr-11b-v2"  # Модель Fimbulvetr
    },
    
    # Text Generation Settings
    "TEXT_GENERATION": {
        "MAX_TOKENS": 1000,
        "TEMPERATURE": 0.7
    },

    # Настройки контроля объема контекста
    "CONTEXT_GUARD": {
        "DEFAULT_CONTEXT_LENGTH": 32768,
        "WARNING_RATIO": 0.8,  # предупреждаем при заполнении 80%
        "HARD_RATIO": 0.95,  # стараемся уложиться в 95% лимита
        "OVERFLOW_STRATEGY": "summarize",  # truncate | summarize
        "MIN_MESSAGES_TO_SUMMARIZE": 4,
        "SUMMARIZATION_MODEL": None,  # если None — используем запрошенную модель
        "SUMMARY_MAX_TOKENS": 256,
    },

    # Настройки дополнительных инструкций (подготовка под будущий функционал)
    "INSTRUCTION_SETTINGS": {
        "USER_INSTRUCTIONS": {  # персональные подсказки от пользователя в каждом чате
            "ENABLED": True,
            "MAX_LENGTH": 2000,
        },
        "ADMIN_USER_NOTES": {  # заметки админа про конкретных пользователей
            "ENABLED": True,
            "MAX_LENGTH": 2000,
        },
    },
    
    # Consilium Settings
    "CONSILIUM_CONFIG": {
        "DEFAULT_MODELS_COUNT": 3,
        "TIMEOUT_PER_MODEL": 60,  # секунд
        "SAVE_TO_HISTORY": True,  # сохранять ли ответы в историю
        "SHOW_TIMING": True,  # показывать время выполнения
    },

    # Исключенные модели (например, требующие аудио)
    "EXCLUDED_MODELS": [
        "google/gemini-2.0-flash-exp:free",
    ],
    # Модели, которые не принимают system/developer инструкции
    "NO_SYSTEM_MODELS": [
        "google/gemma",
    ],

    # Ordered list of fallback моделей, если запрошенная недоступна
    "FALLBACK_MODELS": [
        "mistralai/mistral-large-2407",
        "qwen/qwen2.5-vl-3b-instruct:free",
        "anthropic/claude-3-haiku",
        "google/gemini-2.0-flash-exp:free",
        "openai/gpt-4o-mini",
    ],

    # Приоритет последовательности моделей для фолбэков и консилиума
    "PREFERRED_MODEL_ORDER": [
        "deepseek",
        "mistral",
        "qwen",
        "claude",
        "gemini",
        "gpt",
    ],
    
    # Keywords for routing
    "KEYWORDS": {
        "IMAGE": ["нарисуй", "картинка", "изображение", "сгенерируй картинку", "generate image", "draw", "picture", "image", "generate"],
        "CAPABILITIES": ["что ты умеешь", "твои возможности", "помощь", "справка", "help"]
    }
}
