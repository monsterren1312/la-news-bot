#!/usr/bin/env python3
"""
LA News Telegram Bot
Парсит RSS-ленты новостей Лос-Анджелеса, переводит на русский через Anthropic API,
публикует в Telegram-канал с картинками (если есть в источнике).

Запускается по расписанию (GitHub Actions cron), максимум MAX_POSTS_PER_RUN постов за запуск.
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone

import feedparser
import requests
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("la-news-bot")

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]          # например -1001234567890 или @channelusername
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "1"))
MIN_INTERVAL_MINUTES = int(os.environ.get("MIN_INTERVAL_MINUTES", "15"))
MAX_INTERVAL_MINUTES = int(os.environ.get("MAX_INTERVAL_MINUTES", "45"))
STATE_FILE = os.environ.get("STATE_FILE", "state/seen.json")

CHANNEL_SIGNATURE = "🇺🇸 LA News"
CHANNEL_URL = os.environ.get("CHANNEL_URL", "https://t.me/YOUR_CHANNEL_USERNAME")  # заменить на реальный

# Источники — можно добавлять сколько угодно, каждый со своим именем.
RSS_SOURCES = [
    {"name": "LA Times", "url": "https://www.latimes.com/local/rss2.0.xml"},
    {"name": "LAist", "url": "https://laist.com/rss-feed"},
    {"name": "LA Daily News", "url": "https://www.dailynews.com/feed"},
    {"name": "NBC Los Angeles", "url": "https://www.nbclosangeles.com/?rss=y"},
]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Состояние (какие новости уже публиковали) — защита от повторов
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"seen_hashes": [], "seen_links": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    # Ограничиваем размер файла, чтобы он не рос бесконечно — храним последние 500 записей
    state["seen_hashes"] = state["seen_hashes"][-500:]
    state["seen_links"] = state["seen_links"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def content_hash(title: str, summary: str) -> str:
    """Хэш по смыслу заголовка+описания — помогает поймать дубли одной новости
    из разных источников, а не только повторную публикацию одной и той же ссылки."""
    normalized = (title + summary).lower().strip()
    normalized = "".join(ch for ch in normalized if ch.isalnum() or ch.isspace())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Сбор новостей из RSS
# ---------------------------------------------------------------------------

def fetch_candidates(state: dict) -> list[dict]:
    """Собирает новые (ещё не публиковавшиеся) записи из всех источников."""
    candidates = []

    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
        except Exception as e:
            log.warning(f"Не удалось загрузить {source['name']}: {e}")
            continue

        if feed.bozo and not feed.entries:
            log.warning(f"Лента {source['name']} вернула ошибку без записей, пропускаю")
            continue

        for entry in feed.entries[:10]:  # смотрим только последние 10 записей каждой ленты
            link = entry.get("link", "")
            title = entry.get("title", "").strip()
            summary = entry.get("summary", "") or entry.get("description", "")
            summary = strip_html(summary)[:800]

            if not link or not title:
                continue
            if link in state["seen_links"]:
                continue

            h = content_hash(title, summary)
            if h in state["seen_hashes"]:
                continue  # похожая новость уже была (возможно, из другого источника)

            image_url = extract_image(entry)

            candidates.append({
                "source": source["name"],
                "link": link,
                "title": title,
                "summary": summary,
                "image_url": image_url,
                "hash": h,
                "published": entry.get("published", ""),
            })

    # Сортируем по дате публикации (если есть), самые свежие — первыми
    candidates.sort(key=lambda c: c["published"], reverse=True)
    return candidates


def strip_html(text: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_image(entry) -> str | None:
    """Пытается найти картинку в записи RSS разными способами (зависит от формата ленты)."""
    if "media_content" in entry and entry.media_content:
        url = entry.media_content[0].get("url")
        if url:
            return url
    if "media_thumbnail" in entry and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url")
        if url:
            return url
    if "links" in entry:
        for link in entry.links:
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    if "summary" in entry:
        import re
        match = re.search(r'<img[^>]+src="([^"]+)"', entry.summary)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Перевод и переписывание через Anthropic API
# ---------------------------------------------------------------------------

def rewrite_in_russian(title: str, summary: str, source_name: str) -> str | None:
    """Просит Claude перевести и оформить новость на русском в нужном стиле.
    Возвращает готовый текст поста БЕЗ подписи канала (её добавляем отдельно)."""

    prompt = f"""Ты редактор Telegram-канала новостей Лос-Анджелеса на русском языке.

Вот новость на английском (источник: {source_name}):

Заголовок: {title}
Описание: {summary}

Переведи и оформи это как короткий пост для Telegram на русском языке:
- Заголовок с эмодзи по теме (1 эмодзи), выделенный жирным (Telegram Markdown: *текст*)
- 2-4 предложения по существу, нейтральный новостной тон, никакой "воды"
- НЕ упоминай название источника и НЕ добавляй ссылки на источник в текст
- НЕ добавляй хэштеги
- НЕ добавляй никакую подпись/подвал — это будет добавлено отдельно
- Пиши только сам текст поста, без пояснений от себя, без кавычек вокруг всего текста

Ответь только готовым текстом поста."""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
        return text if text else None
    except Exception as e:
        log.error(f"Ошибка при обращении к Anthropic API: {e}")
        return None


# ---------------------------------------------------------------------------
# Публикация в Telegram
# ---------------------------------------------------------------------------

def build_final_text(body: str) -> str:
    signature = f"[{CHANNEL_SIGNATURE}]({CHANNEL_URL})"
    return f"{body}\n\n{signature}"


def send_to_telegram(text: str, image_url: str | None) -> bool:
    try:
        if image_url:
            # Сначала пробуем отправить с картинкой (по прямой ссылке)
            resp = requests.post(
                f"{TELEGRAM_API}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": image_url,
                    "caption": text,
                    "parse_mode": "Markdown",
                },
                timeout=30,
            )
            if resp.ok and resp.json().get("ok"):
                return True
            log.warning(f"sendPhoto не удался ({resp.text[:200]}), пробую без картинки")

        # Без картинки (либо она отсутствовала, либо не загрузилась)
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if resp.ok and resp.json().get("ok"):
            return True
        log.error(f"sendMessage не удался: {resp.text[:300]}")
        return False

    except Exception as e:
        log.error(f"Ошибка при отправке в Telegram: {e}")
        return False


# ---------------------------------------------------------------------------
# Основной цикл
# ---------------------------------------------------------------------------

def main():
    log.info("Запуск LA News Bot")
    state = load_state()

    candidates = fetch_candidates(state)
    log.info(f"Найдено {len(candidates)} новых кандидатов из {len(RSS_SOURCES)} источников")

    if not candidates:
        log.info("Новых новостей нет, завершение")
        return

    posted = 0
    for item in candidates:
        if posted >= MAX_POSTS_PER_RUN:
            break

        log.info(f"Обрабатываю: [{item['source']}] {item['title'][:80]}")

        body = rewrite_in_russian(item["title"], item["summary"], item["source"])
        if not body:
            log.warning("Не удалось переписать текст, пропускаю эту новость")
            continue

        final_text = build_final_text(body)
        success = send_to_telegram(final_text, item["image_url"])

        if success:
            log.info("Опубликовано успешно")
            state["seen_links"].append(item["link"])
            state["seen_hashes"].append(item["hash"])
            posted += 1
            save_state(state)  # сохраняем после каждого поста, чтобы не потерять прогресс при сбое
            if posted < MAX_POSTS_PER_RUN:
                time.sleep(5)  # небольшая пауза между постами
        else:
            log.error("Публикация не удалась, эта новость будет предложена повторно в следующий раз")

    log.info(f"Готово. Опубликовано постов за этот запуск: {posted}")


if __name__ == "__main__":
    main()
