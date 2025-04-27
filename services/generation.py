import logging
import json
import asyncio
import aiohttp
from openai import AsyncOpenAI
from config import BOT_CONFIG
from services.memory import get_history, get_user_summary

logger = logging.getLogger(__name__)

# Глобальная переменная для клиента OpenRouter
client = None

def init_client():
    """Инициализация клиента OpenRouter после загрузки конфигурации."""
    global client
    if client is None:
        logger.info("Initializing OpenRouter client")
        client = AsyncOpenAI(
            api_key=BOT_CONFIG["OPENROUTER_API_KEY"],
            base_url=BOT_CONFIG["OPENROUTER_BASE_URL"],
            default_headers={
                "HTTP-Referer": BOT_CONFIG["BOT_REFERER"],
                "X-Title": BOT_CONFIG["BOT_TITLE"]
            }
        )
        logger.info("OpenRouter client initialized successfully")
    return client

async def check_model_availability(model: str) -> bool:
    """Проверка доступности модели в OpenRouter API."""
    try:
        client = init_client()
        logger.info(f"Checking availability of model: {model}")
        response = await client.models.list()
        
        if not response or not hasattr(response, 'data'):
            logger.error("Failed to get models list from OpenRouter API")
            return False
            
        # Проверяем наличие модели в списке
        for available_model in response.data:
            model_data = available_model if isinstance(available_model, dict) else available_model.model_dump()
            if model_data.get('id') == model:
                logger.info(f"Model {model} is available")
                return True
                
        logger.error(f"Model {model} is not available in OpenRouter API")
        return False
    except Exception as e:
        logger.error(f"Error checking model availability: {str(e)}")
        return False

async def generate_text(prompt: str, model: str, chat_id: str = None, user_id: str = None) -> str:
    """Генерация текста с помощью OpenRouter API."""
    client = init_client()
    
    messages = []
    
    # Добавляем системный промпт
    if BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"]:
        messages.append({"role": "system", "content": BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"]})
    
    # Если указаны chat_id и user_id, добавляем историю сообщений
    if chat_id and user_id:
        # Получаем историю сообщений
        history = get_history(chat_id, user_id, limit=10)
        
        # Получаем суммаризацию истории, если есть
        summary = get_user_summary(chat_id, user_id)
        
        # Если есть суммаризация, добавляем ее как системное сообщение
        if summary:
            messages.append({"role": "system", "content": f"Краткая история нашего общения: {summary}"})
        
        # Добавляем историю сообщений в контекст
        for msg in reversed(history):
            if msg["role"] in ["user", "assistant"]:
                messages.append({"role": msg["role"], "content": msg["text"]})
    
    # Добавляем текущий запрос пользователя
    messages.append({"role": "user", "content": prompt})

    try:
        logger.info(f"Sending text generation request to OpenRouter with model: {model}, prompt: {prompt}")
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=BOT_CONFIG["TEXT_GENERATION"]["MAX_TOKENS"],
            temperature=BOT_CONFIG["TEXT_GENERATION"]["TEMPERATURE"]
        )
        
        # Проверяем структуру ответа
        if not response or not hasattr(response, 'choices') or not response.choices:
            logger.error("Empty or invalid response from OpenRouter API")
            return "Извините, произошла ошибка при получении ответа от API. Пожалуйста, попробуйте позже."
            
        # Безопасное извлечение результата
        try:
            result = response.choices[0].message.content.strip()
            if not result:
                logger.error("Empty content in response from OpenRouter API")
                return "Извините, получен пустой ответ от API. Пожалуйста, попробуйте позже."
            logger.info(f"Received response from OpenRouter: {result[:100]}...")
            return result
        except (AttributeError, IndexError) as e:
            logger.error(f"Error extracting content from response: {str(e)}")
            return "Извините, произошла ошибка при обработке ответа от API. Пожалуйста, попробуйте позже."
            
    except Exception as e:
        logger.error(f"Error generating text: {str(e)}")
        return f"Произошла ошибка при генерации текста: {str(e)}"

async def generate_image(prompt: str) -> str:
    """Генерация изображения с помощью PiAPI.ai."""
    if not BOT_CONFIG["PIAPI_KEY"]:
        logger.error("PIAPI_KEY environment variable is not set.")
        return "Ошибка конфигурации: Ключ API для генерации изображений не найден."

    try:
        url = "https://api.piapi.ai/api/v1/task"
        headers = {
            "X-API-Key": BOT_CONFIG["PIAPI_KEY"],
            "Content-Type": "application/json"
        }

        payload = {
            "model": BOT_CONFIG["IMAGE_GENERATION"]["MODEL"],
            "task_type": BOT_CONFIG["IMAGE_GENERATION"]["TASK_TYPE"],
            "input": {
                "prompt": prompt,
                "negative_prompt": BOT_CONFIG["IMAGE_GENERATION"]["NEGATIVE_PROMPT"],
                "aspect_ratio": BOT_CONFIG["IMAGE_GENERATION"]["ASPECT_RATIO"]
            }
        }

        async with aiohttp.ClientSession() as session:
            # 1. Запуск задачи генерации
            logger.info(f"Sending image generation request to PiAPI.ai for prompt: {prompt}")
            async with session.post(url, headers=headers, data=json.dumps(payload)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"PiAPI.ai Error Response: {error_text} (Status: {response.status})")
                    raise Exception(f"Failed to start PiAPI.ai image generation: {error_text}")

                task_data = await response.json()
                data_dict = task_data.get("data")
                task_id = data_dict.get("task_id") if data_dict else None

                if not task_id:
                    logger.error(f"No task_id received from PiAPI.ai: {task_data}")
                    raise Exception("No task_id received from PiAPI.ai")

                logger.info(f"Started PiAPI.ai image generation task: {task_id}")

            # 2. Ожидание завершения задачи
            max_attempts = BOT_CONFIG["IMAGE_GENERATION"]["MAX_ATTEMPTS"]
            attempts = 0
            status_check_url = f"{url}/{task_id}"

            while attempts < max_attempts:
                await asyncio.sleep(BOT_CONFIG["IMAGE_GENERATION"]["POLLING_INTERVAL"])
                logger.info(f"Checking status for task {task_id} (Attempt {attempts + 1}/{max_attempts})")
                async with session.get(status_check_url, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Status check failed for task {task_id}: {error_text} (Status: {response.status})")
                        attempts += 1
                        continue

                    status_data = await response.json()
                    data_dict = status_data.get("data", {})
                    task_status = data_dict.get("status")
                    logger.info(f"Task {task_id} status: {task_status}")

                    if task_status == "completed":
                        output_dict = data_dict.get("output", {})
                        image_url = output_dict.get("image_url")
                        if image_url:
                            logger.info(f"Image generation successful for task {task_id}: {image_url}")
                            return image_url
                        else:
                            logger.error(f"Completed task {task_id} but no result URL found: {status_data}")
                            raise Exception("No image URL in successful PiAPI.ai response")
                    elif task_status == "failed":
                        error_details = data_dict.get("error", {}).get("message", "Unknown error")
                        logger.error(f"Image generation failed for task {task_id}: {error_details}")
                        raise Exception(f"PiAPI.ai image generation failed: {error_details}")
                    elif task_status in ["processing", "pending"]:
                        pass
                    else:
                        logger.warning(f"Unknown task status for {task_id}: {task_status}")

                    attempts += 1

            logger.error(f"Image generation timed out for task {task_id}")
            raise Exception("Image generation timed out with PiAPI.ai")

    except Exception as e:
        logger.error(f"Error generating image with PiAPI.ai: {str(e)}", exc_info=True)
        return f"Произошла ошибка при генерации изображения через PiAPI.ai: {str(e)}" 