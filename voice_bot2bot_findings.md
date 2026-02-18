# Bot-to-Bot Voice Findings (2026-02-17)

## Что проверено
- Запускались `talker` и `listener` в одном voice-канале Discord.
- Проверены 3 sender-пути:
  - `tools/voice_test_sender/talker_joker.py` (py-cord sender),
  - `tools/voice_test_sender/send_voice_disnake.py` (disnake sender),
  - `tools/voice_test_sender/send_voice.py` (py-cord sender с `FFmpegOpusAudio.from_probe`).
- Listener запускался отдельным токеном Нейротолика (`DISCORD_BOT_TOKEN`) против токена Talker (`DISCORD_TEST_BOT_TOKEN`).

## Подтвержденные наблюдения
- Bot-to-bot аудио доходит: listener получает пакеты и пишет `.wav` чанки в `data/voice_listener`.
- При этом массово сыпется декод:
  - `Error occurred while decoding opus frame.`
- Из-за этого чанки часто рваные по длительности (например 4.0s, 8.9s, 0.38s).
- STT на таких чанках дает пустой результат (`{\"text\":\"\"}` на локальном whisper endpoint).

## Вывод
- Проблема не в wake-фразах и не в выключенном приеме.
- Текущее узкое место: нестабильный bot-to-bot decode opus в текущем receive-стеке (`py-cord` sinks/voice receive path).
- Нужен альтернативный receiver-стек для валидации (вне `py-cord sinks`) и сравнения качества приема.

## Результат альтернативного receiver-стека (пункт 1)
- Добавлен receiver на `discord.py + discord-ext-voice-recv`:
  - `tools/voice_test_sender/listen_voice_recv.py`
  - `scripts/run_voice_listener_recv.sh`
- Тест `talker + listen_voice_recv` прошел стабильно:
  - без `Error occurred while decoding opus frame`,
  - ровные чанки по 4.00s (200 пакетов на чанк),
  - локальный whisper возвращает текст (не пустой ответ).
- Практический вывод:
  - bot-to-bot voice на этом сервере возможен,
  - проблема локализована в старом receive-пути (`py-cord sinks`), а не в Discord как таковом.

## Что уже изменено попутно
- В `talker_joker.py` удалены pitch/style модификации голоса (чистый TTS без трансформаций).
- Замедлен темп: `--pause-seconds` по умолчанию увеличен до `2.8`.

## Доп. прогон после интеграции `voice_recv_worker` (2026-02-17 20:47 UTC)
- Запущен `tools/voice_recv_worker/worker.py` (sidecar, пишет в `voice_logs` той же БД).
- Запущен `talker_joker.py` на 3 шутки подряд.
- В `voice_logs` зафиксировано 2 распознанных записи:
  - Сказано: `Сервер не падал, он просто резко ушёл в горизонтальное масштабирование.`
  - Распознано: `Сервер не падал, он просто резко ушел в горизонтальное масштабирование.`
  - Сказано: `В чат написали "кто сломал?" И чат внезапно стал философским.`
  - Распознано: `Чат написали, кто сломал. И Чат внезапно стал филаром.`
- Вывод:
  - прием bot-to-bot работает стабильно (текст не пустой, смысл в целом сохраняется),
  - но качество распознавания на части фраз среднее (ошибки слов/падежей),
  - основное улучшение достигнуто: стек перестал "глохнуть" и начал давать регулярные транскрипты.
