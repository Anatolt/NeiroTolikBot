import logging
import asyncio
import aiohttp
from typing import List, Dict, Any
from urllib.parse import quote

logger = logging.getLogger(__name__)


async def search_web(query: str, max_results: int = 5) -> str:
    """
    Выполняет веб-поиск через DuckDuckGo API и возвращает отформатированную строку.
    
    Args:
        query: Поисковый запрос
        max_results: Максимальное количество результатов (по умолчанию 5)
    
    Returns:
        Отформатированная строка с результатами поиска для передачи модели
    """
    try:
        encoded_query = quote(query)
        instant_answer_url = f"https://api.duckduckgo.com/?q={encoded_query}&format=json&no_html=1&skip_disambig=1"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(instant_answer_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    results = []
                    
                    # Добавляем Abstract (краткое описание)
                    if data.get("Abstract"):
                        results.append({
                            "title": data.get("Heading", "Результат поиска"),
                            "url": data.get("AbstractURL", ""),
                            "snippet": data.get("Abstract", "")
                        })
                    
                    # Добавляем RelatedTopics (связанные темы)
                    related_topics = data.get("RelatedTopics", [])
                    for topic in related_topics[:max_results - len(results)]:
                        if isinstance(topic, dict) and "Text" in topic:
                            results.append({
                                "title": topic.get("Text", "").split(" - ")[0] if " - " in topic.get("Text", "") else "Результат",
                                "url": topic.get("FirstURL", ""),
                                "snippet": topic.get("Text", "")
                            })
                    
                    # Если результатов недостаточно, пробуем HTML поиск
                    if len(results) < max_results:
                        try:
                            html_results = await _search_duckduckgo_html(query, max_results - len(results))
                            results.extend(html_results)
                        except Exception as e:
                            logger.warning(f"Failed to get HTML results: {str(e)}")
                    
                    # Форматируем результаты для модели
                    if not results:
                        return f"По запросу '{query}' не найдено результатов. Попробуйте изменить формулировку запроса."
                    
                    formatted_results = "Результаты поиска в интернете:\n\n"
                    for i, result in enumerate(results[:max_results], 1):
                        formatted_results += f"{i}. {result['title']}\n"
                        if result['url']:
                            formatted_results += f"   URL: {result['url']}\n"
                        if result['snippet']:
                            formatted_results += f"   {result['snippet']}\n"
                        formatted_results += "\n"
                    
                    return formatted_results.strip()
                else:
                    logger.error(f"DuckDuckGo API returned status {response.status}")
                    return f"Ошибка при выполнении поиска по запросу '{query}'. Попробуйте позже."
                    
    except asyncio.TimeoutError:
        logger.error(f"Timeout while searching for: {query}")
        return f"Поиск по запросу '{query}' занял слишком много времени. Попробуйте позже."
    except Exception as e:
        logger.error(f"Error during web search: {str(e)}", exc_info=True)
        return f"Произошла ошибка при выполнении поиска по запросу '{query}': {str(e)}"


async def _search_duckduckgo_html(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """
    Альтернативный метод поиска через парсинг HTML страницы DuckDuckGo.
    """
    import re
    
    try:
        encoded_query = quote(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_query}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    html = await response.text()
                    
                    results = []
                    pattern = r'<a class="result__a".*?href="([^"]+)".*?>(.*?)</a>'
                    matches = re.findall(pattern, html, re.DOTALL)
                    
                    for url_match, title_match in matches[:max_results]:
                        title = re.sub(r'<[^>]+>', '', title_match).strip()
                        if title and url_match:
                            results.append({
                                "title": title,
                                "url": url_match,
                                "snippet": ""
                            })
                    
                    return results
                else:
                    logger.warning(f"DuckDuckGo HTML API returned status {response.status}")
                    return []
    except Exception as e:
        logger.warning(f"HTML search fallback failed: {str(e)}")
        return []

