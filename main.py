from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import textwrap
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star
except Exception:  # pragma: no cover - local syntax/render testing outside AstrBot.
    import logging

    logger = logging.getLogger("astrbot_plugin_anime_jrys")

    class Context:  # type: ignore[no-redef]
        pass

    class Star:  # type: ignore[no-redef]
        def __init__(self, context: Context):
            self.context = context
            self.name = "astrbot_plugin_anime_jrys"

    class AstrMessageEvent:  # type: ignore[no-redef]
        pass

    class _DummyFilter:
        def command(self, *_args, **_kwargs):
            def deco(func):
                return func

            return deco

        def event_message_type(self, *_args, **_kwargs):
            def deco(func):
                return func

            return deco

        class EventMessageType:
            ALL = "all"

    filter = _DummyFilter()  # type: ignore[assignment]


PLUGIN_NAME = "astrbot_plugin_anime_jrys"
CANVAS_SIZE = (1080, 1440)
IMAGE_AREA_HEIGHT = 850

DEFAULT_WALLHAVEN_KEYWORDS = (
    "genshin impact;honkai star rail;honkai impact 3rd;zenless zone zero;"
    "wuthering waves;arknights;blue archive;azur lane;girls frontline;nikke;"
    "punishing gray raven;snowbreak containment zone;path to nowhere;reverse 1999;"
    "fate grand order;umamusume"
)
DEFAULT_KONACHAN_TAGS = ""
DEFAULT_SAFEBOORU_TAGS = (
    "genshin_impact;honkai:_star_rail;honkai_impact;zenless_zone_zero;"
    "wuthering_waves;arknights;blue_archive;azur_lane;girls_frontline;"
    "goddess_of_victory:_nikke;punishing:_gray_raven;path_to_nowhere;"
    "reverse:1999;fate/grand_order;umamusume"
)
DEFAULT_SAFEBOORU_EXCLUDED_TAGS = (
    "underwear;bikini;swimsuit;lingerie;nude;naked;sex;explicit;animated_gif;"
    "character_sheet;reference_sheet;sketch;comic;manga;ass;bottomless;kiss;"
    "french_kiss;tongue;tongue_out;armpits;sports_bra;leotard;midriff;navel;"
    "nipples;pectorals;bulge;spread_legs;wide_spread_legs;feet;soles"
)

TRIGGER_WORDS = {"jrys", "今日运势", "运势"}
TREND_TRIGGER_WORDS = {"ysqs", "运势趋势", "趋势"}
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


@dataclass(frozen=True)
class ImageCandidate:
    source: str
    url: str
    keyword: str = ""
    credit_url: str = ""


@dataclass(frozen=True)
class UserDisplay:
    user_id: str
    name: str
    avatar_url: str = ""


@dataclass
class FortuneRecord:
    day: str
    score: int
    tier: str
    text: str
    streak: int
    image_path: str
    image_source: str
    image_keyword: str
    card_path: str
    display_name: str = ""
    avatar_url: str = ""
    avatar_path: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)


def _config_get(config: Any, key: str, default: Any) -> Any:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            value = getter(key)
            return default if value is None else value
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _config_bool(config: Any, key: str, default: bool) -> bool:
    value = _config_get(config, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enable", "enabled", "开启", "是"}:
            return True
        if lowered in {"0", "false", "no", "off", "disable", "disabled", "关闭", "否", ""}:
            return False
    return bool(value)


def _config_int(config: Any, key: str, default: int, minimum: int, maximum: int) -> int:
    value = _config_get(config, key, default)
    try:
        result = int(float(str(value).strip()))
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


def _split_semicolon(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _today_str() -> str:
    return date.today().isoformat()


def _raw_user_id(event: AstrMessageEvent) -> str:
    try:
        user_id = str(event.get_sender_id())
    except Exception:
        user_id = ""
    if not user_id:
        try:
            user_id = str(event.message_obj.sender.user_id)
        except Exception:
            user_id = "unknown"
    return user_id


def _safe_user_id(event: AstrMessageEvent) -> str:
    user_id = _raw_user_id(event)
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    return digest


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AnimeJrysPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._data_dir = self._get_plugin_data_dir()
        self._cache_dir = self._data_dir / "cache"
        self._avatars_dir = self._data_dir / "avatars"
        self._cards_dir = self._data_dir / "cards"
        self._trend_cards_dir = self._data_dir / "trend_cards"
        self._base_cards_dir = self._data_dir / "base_cards"
        self._users_file = self._data_dir / "users.json"
        self._pool_file = self._data_dir / "image_pool.json"
        self._source_cooldowns: dict[str, float] = {}
        self._prefetch_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._background_task: asyncio.Task | None = None
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._avatars_dir.mkdir(parents=True, exist_ok=True)
        self._cards_dir.mkdir(parents=True, exist_ok=True)
        self._trend_cards_dir.mkdir(parents=True, exist_ok=True)
        self._base_cards_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        self._ensure_background_task()

    @filter.command("jrys", alias={"今日运势", "运势"})
    async def jrys(self, event: AstrMessageEvent):
        """生成今日动漫/二游运势图。"""
        async for result in self._reply_fortune(event):
            yield result

    @filter.command("ysqs", alias={"运势趋势", "趋势"})
    async def ysqs(self, event: AstrMessageEvent):
        """生成近 7 天运势趋势图。"""
        async for result in self._reply_trend(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_plain_trigger(self, event: AstrMessageEvent):
        """兼容用户直接发送触发词。"""
        text = getattr(event, "message_str", "").strip()
        if text in TRIGGER_WORDS:
            async for result in self._reply_fortune(event):
                yield result
            return
        if text in TREND_TRIGGER_WORDS:
            async for result in self._reply_trend(event):
                yield result

    async def _reply_fortune(self, event: AstrMessageEvent):
        try:
            self._ensure_background_task()
            record = await self._get_or_create_today_record(event)
            yield event.image_result(record.card_path)
            event.stop_event()
        except Exception as exc:
            logger.error(f"生成今日运势失败: {exc}")
            yield event.plain_result("今日运势图生成失败了，请稍后再试。")
            event.stop_event()

    async def _reply_trend(self, event: AstrMessageEvent):
        try:
            self._ensure_background_task()
            record = await self._get_or_create_today_record(event)
            user_key = _safe_user_id(event)
            user_display = self._get_user_display(event)
            avatar_path = await self._fetch_user_avatar(user_display)
            trend_path = self._trend_cards_dir / f"{_today_str()}_{user_key}.jpg"
            points = self._recent_fortune_points(record)
            image_path = Path(record.image_path)
            image_source = record.image_source
            image_keyword = record.image_keyword
            if not image_path.exists():
                replacement = await self._fetch_random_image()
                image_path = Path(replacement.url)
                image_source = replacement.source
                image_keyword = replacement.keyword
            await asyncio.to_thread(
                self._render_trend_card,
                image_path,
                trend_path,
                points,
                user_display.name,
                avatar_path,
                image_source,
                image_keyword,
            )
            yield event.image_result(str(trend_path))
            event.stop_event()
        except Exception as exc:
            logger.error(f"生成运势趋势图失败: {exc}")
            yield event.plain_result("运势趋势图生成失败了，请稍后再试。")
            event.stop_event()

    async def _get_or_create_today_record(self, event: AstrMessageEvent) -> FortuneRecord:
        user_key = _safe_user_id(event)
        today = _today_str()
        user_display = self._get_user_display(event)
        async with self._lock:
            users = await asyncio.to_thread(self._load_users)
            existing = users.get(user_key, {})
            if existing.get("day") == today and existing.get("card_path"):
                card_path = Path(existing["card_path"])
                if card_path.exists() and self._record_has_display_badge(existing):
                    if self._ensure_record_history(existing):
                        users[user_key] = existing
                        await asyncio.to_thread(self._save_users, users)
                    return FortuneRecord(**existing)
                if card_path.exists() and existing.get("image_path"):
                    image_path = Path(str(existing["image_path"]))
                    if image_path.exists():
                        avatar_path = await self._fetch_user_avatar(user_display)
                        await asyncio.to_thread(
                            self._render_card,
                            image_path,
                            card_path,
                            int(existing.get("score", 0) or 0),
                            str(existing.get("tier", "")),
                            str(existing.get("text", "")),
                            int(existing.get("streak", 1) or 1),
                            str(existing.get("image_source", "")),
                            str(existing.get("image_keyword", "")),
                            user_display.name,
                            avatar_path,
                        )
                        existing["display_name"] = user_display.name
                        existing["avatar_url"] = user_display.avatar_url
                        existing["avatar_path"] = avatar_path
                        self._ensure_record_history(existing)
                        users[user_key] = existing
                        await asyncio.to_thread(self._save_users, users)
                        return FortuneRecord(**existing)

            streak = self._next_streak(existing, today)
            score, tier, text = self._roll_fortune()
            used_image_paths = self._today_used_image_paths(users)
            image = await self._fetch_random_image(used_image_paths)
            avatar_path = await self._fetch_user_avatar(user_display)
            card_path = self._cards_dir / f"{today}_{user_key}.jpg"
            await asyncio.to_thread(
                self._render_card,
                Path(image.url),
                card_path,
                score,
                tier,
                text,
                streak,
                image.source,
                image.keyword,
                user_display.name,
                avatar_path,
            )

            record = FortuneRecord(
                day=today,
                score=score,
                tier=tier,
                text=text,
                streak=streak,
                image_path=image.url,
                image_source=image.source,
                image_keyword=image.keyword,
                card_path=str(card_path),
                display_name=user_display.name,
                avatar_url=user_display.avatar_url,
                avatar_path=avatar_path,
                history=self._updated_history(existing, today, score),
            )
            users[user_key] = record.__dict__
            await asyncio.to_thread(self._save_users, users)
            return record

    def _record_has_display_badge(self, record: dict[str, Any]) -> bool:
        if not _config_bool(self.config, "show_user_badge", True):
            return True
        return bool(str(record.get("display_name", "")).strip())

    def _next_streak(self, existing: dict[str, Any], today: str) -> int:
        previous_day = str(existing.get("day", ""))
        previous_streak = int(existing.get("streak", 0) or 0)
        if not previous_day:
            return 1
        try:
            prev = date.fromisoformat(previous_day)
            now = date.fromisoformat(today)
        except ValueError:
            return 1
        if prev == now - timedelta(days=1):
            return max(1, previous_streak + 1)
        if prev == now:
            return max(1, previous_streak)
        return 1

    def _ensure_record_history(self, record: dict[str, Any]) -> bool:
        day = str(record.get("day", "") or "")
        if not day:
            return False
        try:
            score = int(record.get("score", 0) or 0)
        except (TypeError, ValueError):
            return False
        history = self._updated_history(record, day, score)
        if history == record.get("history"):
            return False
        record["history"] = history
        return True

    def _updated_history(
        self, existing: dict[str, Any], day: str, score: int
    ) -> list[dict[str, Any]]:
        by_day: dict[str, int] = {}

        def put(day_text: str, score_value: Any) -> None:
            try:
                parsed_day = date.fromisoformat(str(day_text))
                parsed_score = int(score_value)
            except (TypeError, ValueError):
                return
            by_day[parsed_day.isoformat()] = max(0, min(100, parsed_score))

        history = existing.get("history", [])
        if isinstance(history, list):
            for item in history:
                if isinstance(item, dict):
                    put(str(item.get("day", "")), item.get("score"))

        put(str(existing.get("day", "")), existing.get("score"))
        put(day, score)

        return [
            {"day": day_text, "score": by_day[day_text]}
            for day_text in sorted(by_day.keys())[-60:]
        ]

    def _recent_fortune_points(self, record: FortuneRecord) -> list[tuple[str, int | None]]:
        try:
            end_day = date.fromisoformat(record.day)
        except ValueError:
            end_day = date.today()
        history = self._updated_history(record.__dict__, record.day, record.score)
        by_day = {str(item["day"]): int(item["score"]) for item in history}
        days = [end_day - timedelta(days=offset) for offset in range(6, -1, -1)]
        return [(day.isoformat(), by_day.get(day.isoformat())) for day in days]

    def _roll_fortune(self) -> tuple[int, str, str]:
        bands = [
            (4, 0, 10, "极低运势", LOW_TEXTS),
            (16, 11, 24, "低运势", LOW_TEXTS),
            (25, 25, 49, "中运势", MID_TEXTS),
            (25, 50, 69, "偏高运势", HIGH_TEXTS),
            (20, 70, 89, "高运势", HIGH_TEXTS),
            (8, 90, 99, "极高运势", GREAT_TEXTS),
            (2, 100, 100, "最高运势", PERFECT_TEXTS),
        ]
        pick = random.uniform(0, sum(b[0] for b in bands))
        cursor = 0.0
        for weight, start, end, tier, texts in bands:
            cursor += weight
            if pick <= cursor:
                score = random.randint(start, end)
                return score, tier, random.choice(texts)
        return 100, "最高运势", random.choice(PERFECT_TEXTS)

    async def _fetch_random_image(self, blocked_paths: set[str] | None = None) -> ImageCandidate:
        blocked_paths = blocked_paths or set()
        if _config_bool(self.config, "enable_image_prefetch", True):
            pooled = await self._take_from_image_pool(blocked_paths)
            if pooled:
                self._schedule_pool_refill()
                return pooled

        image = await self._fetch_random_image_live(blocked_paths)
        if _config_bool(self.config, "enable_image_prefetch", True):
            self._schedule_pool_refill()
        return image

    async def _fetch_random_image_live(
        self, blocked_paths: set[str] | None = None
    ) -> ImageCandidate:
        blocked_paths = blocked_paths or set()
        session = await self._get_session()
        attempts = self._build_source_attempts()

        for source_name, keyword in attempts:
            if self._is_source_in_cooldown(source_name):
                continue
            try:
                candidate = await self._resolve_candidate(session, source_name, keyword)
                if not candidate:
                    continue
                local_path = await self._download_and_validate(session, candidate)
                if local_path:
                    if self._image_path_in_set(local_path, blocked_paths):
                        logger.info(f"图片源 {source_name} 返回了今日已使用图片，已跳过")
                        continue
                    return ImageCandidate(
                        source=candidate.source,
                        url=str(local_path),
                        keyword=candidate.keyword,
                        credit_url=candidate.credit_url,
                    )
            except Exception as exc:
                status = self._status_from_exception(exc)
                if status in {403, 429}:
                    self._cooldown_source(source_name, status)
                detail = f"{type(exc).__name__}: {exc}".strip()
                logger.info(f"图片源 {source_name} 本次不可用，继续尝试其他图源: {detail}")

        raise RuntimeError("所有图片源均获取失败")

    async def _take_from_image_pool(self, blocked_paths: set[str] | None = None) -> ImageCandidate | None:
        if self._image_pool_size() <= 0:
            return None
        blocked_paths = blocked_paths or set()
        async with self._prefetch_lock:
            entries = await asyncio.to_thread(self._load_image_pool)
            valid_entries = [entry for entry in entries if self._pool_entry_is_valid(entry)]
            available_entries = [
                entry
                for entry in valid_entries
                if not self._pool_entry_is_blocked(entry, blocked_paths)
            ]
            if not available_entries:
                await asyncio.to_thread(self._save_image_pool, available_entries)
                return None
            selected_index = random.randrange(len(available_entries))
            entry = available_entries.pop(selected_index)
            await asyncio.to_thread(self._save_image_pool, available_entries)
        return ImageCandidate(
            source=str(entry.get("source", "ImagePool")),
            url=str(entry.get("image_path", "")),
            keyword=str(entry.get("keyword", "cached image")),
            credit_url=str(entry.get("credit_url", "")),
        )

    async def _add_image_to_pool(self, image: ImageCandidate) -> None:
        target = self._image_pool_size()
        if target <= 0:
            await asyncio.to_thread(self._save_image_pool, [])
            return
        if not image.url or not Path(image.url).exists():
            return
        async with self._prefetch_lock:
            blocked_paths = self._today_used_image_paths()
            if self._image_path_in_set(Path(image.url), blocked_paths):
                return
            entries = await asyncio.to_thread(self._load_image_pool)
            entries = [
                entry
                for entry in entries
                if self._pool_entry_is_valid(entry)
                and not self._pool_entry_is_blocked(entry, blocked_paths)
            ]
            if any(str(entry.get("image_path", "")) == image.url for entry in entries):
                return
            entries.append(
                {
                    "image_path": image.url,
                    "source": image.source,
                    "keyword": image.keyword,
                    "credit_url": image.credit_url,
                    "created_at": int(time.time()),
                }
            )
            await asyncio.to_thread(self._save_image_pool, entries[-target:])

    async def _ensure_image_pool(self) -> None:
        if not _config_bool(self.config, "enable_image_prefetch", True):
            return
        async with self._prefetch_lock:
            blocked_paths = self._today_used_image_paths()
            entries = await asyncio.to_thread(self._load_image_pool)
            entries = [
                entry
                for entry in entries
                if self._pool_entry_is_valid(entry)
                and not self._pool_entry_is_blocked(entry, blocked_paths)
            ]
            target = self._image_pool_size()
            if target <= 0:
                await asyncio.to_thread(self._save_image_pool, [])
                return
            batch = self._image_pool_refill_batch()
            missing = max(0, target - len(entries))
            fetch_count = min(batch, missing)
            fetch_attempts = 0
            max_fetch_attempts = max(fetch_count * 3, fetch_count)
            while fetch_count > 0 and fetch_attempts < max_fetch_attempts:
                fetch_attempts += 1
                blocked_for_fetch = blocked_paths | {
                    self._normalize_path_key(Path(str(entry.get("image_path", ""))))
                    for entry in entries
                    if str(entry.get("image_path", "")).strip()
                }
                try:
                    image = await self._fetch_random_image_live(blocked_for_fetch)
                except Exception as exc:
                    logger.info(f"图片池预取失败，稍后重试: {type(exc).__name__}: {exc}")
                    break
                if any(str(entry.get("image_path", "")) == image.url for entry in entries):
                    continue
                entries.append(
                    {
                        "image_path": image.url,
                        "source": image.source,
                        "keyword": image.keyword,
                        "credit_url": image.credit_url,
                        "created_at": int(time.time()),
                    }
                )
                if _config_bool(self.config, "enable_shared_base_cards", True):
                    await asyncio.to_thread(
                        self._render_base_card,
                        Path(image.url),
                        self._base_card_path(Path(image.url), image.source, image.keyword),
                        image.source,
                        image.keyword,
                    )
                fetch_count -= 1
            await asyncio.to_thread(self._save_image_pool, entries[-target:])

    def _load_image_pool(self) -> list[dict[str, Any]]:
        if not self._pool_file.exists():
            return []
        try:
            data = json.loads(self._pool_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [entry for entry in data if isinstance(entry, dict)]
        except Exception as exc:
            logger.info(f"读取图片池失败，将重建: {exc}")
        return []

    def _save_image_pool(self, entries: list[dict[str, Any]]) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._pool_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._pool_file)

    def _pool_entry_is_valid(self, entry: dict[str, Any]) -> bool:
        path = Path(str(entry.get("image_path", "")))
        return path.exists() and self._is_valid_landscape_image(path)

    def _pool_entry_is_blocked(self, entry: dict[str, Any], blocked_paths: set[str]) -> bool:
        path_text = str(entry.get("image_path", "") or "")
        return bool(path_text) and self._normalize_path_key(Path(path_text)) in blocked_paths

    def _image_pool_size(self) -> int:
        return _config_int(self.config, "image_pool_size", 8, 0, 100)

    def _image_pool_refill_batch(self) -> int:
        return _config_int(self.config, "image_pool_refill_batch", 2, 1, 20)

    def _schedule_pool_refill(self) -> None:
        if not _config_bool(self.config, "enable_image_prefetch", True):
            return
        if self._image_pool_size() <= 0:
            return
        try:
            asyncio.create_task(self._ensure_image_pool())
        except RuntimeError:
            return

    def _ensure_background_task(self) -> None:
        if self._background_task and not self._background_task.done():
            return
        try:
            self._background_task = asyncio.create_task(self._background_loop())
        except RuntimeError:
            return

    async def _background_loop(self) -> None:
        while True:
            try:
                await self._run_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info(f"缓存维护任务失败，稍后重试: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(60, self._cleanup_interval_hours() * 3600))

    async def _run_maintenance(self) -> None:
        async with self._maintenance_lock:
            await asyncio.to_thread(self._cleanup_cache_files)
            await self._ensure_image_pool()

    def _cleanup_interval_hours(self) -> int:
        return _config_int(self.config, "cleanup_interval_hours", 12, 1, 168)

    def _cleanup_cache_files(self) -> None:
        users = self._load_users()
        protected = self._protected_cache_paths(users)
        now = time.time()
        self._delete_old_files(
            self._cards_dir,
            _config_int(self.config, "card_retention_days", 7, 1, 3650),
            now,
            protected,
        )
        self._delete_old_files(
            self._base_cards_dir,
            _config_int(self.config, "base_card_retention_days", 7, 1, 3650),
            now,
            protected,
        )
        self._delete_old_files(
            self._trend_cards_dir,
            _config_int(self.config, "card_retention_days", 7, 1, 3650),
            now,
            protected,
        )
        self._delete_old_files(
            self._cache_dir,
            _config_int(self.config, "image_cache_retention_days", 14, 1, 3650),
            now,
            protected,
        )
        avatar_days = _config_int(self.config, "avatar_cache_days", 5, -1, 3650)
        if avatar_days >= 0:
            self._delete_old_files(self._avatars_dir, max(1, avatar_days), now, protected)

        self._trim_cache_size(protected)
        blocked_paths = self._today_used_image_paths(users)
        pool = [
            entry
            for entry in self._load_image_pool()
            if self._pool_entry_is_valid(entry)
            and not self._pool_entry_is_blocked(entry, blocked_paths)
        ]
        pool_size = self._image_pool_size()
        self._save_image_pool(pool[-pool_size:] if pool_size > 0 else [])

    def _today_used_image_paths(self, users: dict[str, Any] | None = None) -> set[str]:
        users = users if users is not None else self._load_users()
        today = _today_str()
        used: set[str] = set()
        for value in users.values():
            if not isinstance(value, dict):
                continue
            if str(value.get("day", "")) != today:
                continue
            image_path = str(value.get("image_path", "") or "").strip()
            if image_path:
                used.add(self._normalize_path_key(Path(image_path)))
        return used

    def _image_path_in_set(self, path: Path, path_set: set[str]) -> bool:
        return self._normalize_path_key(path) in path_set

    def _protected_cache_paths(self, users: dict[str, Any]) -> set[Path]:
        protected: set[Path] = {self._users_file, self._pool_file}
        today = _today_str()
        for value in users.values():
            if not isinstance(value, dict):
                continue
            if str(value.get("day", "")) != today:
                continue
            for key in ("card_path", "image_path", "avatar_path"):
                path_text = str(value.get(key, "") or "")
                if path_text:
                    protected.add(self._normalize_path(Path(path_text)))
        for entry in self._load_image_pool():
            path_text = str(entry.get("image_path", "") or "")
            if path_text:
                protected.add(self._normalize_path(Path(path_text)))
        return protected

    def _delete_old_files(
        self,
        directory: Path,
        retention_days: int,
        now: float,
        protected: set[Path],
    ) -> None:
        if not directory.exists():
            return
        cutoff = now - retention_days * 86400
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if self._normalize_path(path) in protected:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _trim_cache_size(self, protected: set[Path]) -> None:
        max_mb = _config_int(self.config, "cache_max_mb", 300, 0, 10240)
        if max_mb <= 0:
            return
        max_bytes = max_mb * 1024 * 1024
        files: list[tuple[float, int, Path]] = []
        total = 0
        for directory in (
            self._cache_dir,
            self._avatars_dir,
            self._cards_dir,
            self._trend_cards_dir,
            self._base_cards_dir,
        ):
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                total += stat.st_size
                if self._normalize_path(path) not in protected:
                    files.append((stat.st_mtime, stat.st_size, path))
        if total <= max_bytes:
            return
        files.sort(key=lambda item: item[0])
        for _mtime, size, path in files:
            if total <= max_bytes:
                break
            try:
                path.unlink(missing_ok=True)
                total -= size
            except OSError:
                continue

    def _normalize_path(self, path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    def _normalize_path_key(self, path: Path) -> str:
        return str(self._normalize_path(path))

    def _build_source_attempts(self) -> list[tuple[str, str]]:
        enable_wallhaven = _config_bool(self.config, "enable_wallhaven_source", True)
        enable_konachan = _config_bool(self.config, "enable_konachan_source", False)
        enable_safebooru = _config_bool(self.config, "enable_safebooru_source", True)
        wallhaven_keywords = _split_semicolon(
            _config_get(self.config, "wallhaven_keywords", DEFAULT_WALLHAVEN_KEYWORDS)
        )
        konachan_tags = _split_semicolon(
            _config_get(self.config, "konachan_tags", DEFAULT_KONACHAN_TAGS)
        )
        safebooru_tags = _split_semicolon(
            _config_get(self.config, "safebooru_tags", DEFAULT_SAFEBOORU_TAGS)
        )
        keyword_attempts: list[tuple[str, str]] = []
        fallback_attempts: list[tuple[str, str]] = []

        keyword_sources: list[tuple[str, list[str]]] = []
        if enable_wallhaven and wallhaven_keywords:
            keyword_sources.append(("wallhaven", wallhaven_keywords))
        if enable_konachan and konachan_tags:
            keyword_sources.append(("konachan", konachan_tags))
        if enable_safebooru and safebooru_tags:
            keyword_sources.append(("safebooru", safebooru_tags))

        if keyword_sources:
            for name, values in keyword_sources:
                keyword_attempts.append((name, random.choice(values)))

        if _config_bool(self.config, "enable_zhuqiy_fallback", True):
            fallback_attempts.append(("zhuqiy", ""))
        if _config_bool(self.config, "enable_waifu_im_fallback", True):
            fallback_attempts.append(("waifu_im", ""))

        random.shuffle(fallback_attempts)
        return keyword_attempts + fallback_attempts

    def _is_source_in_cooldown(self, source_name: str) -> bool:
        until = self._source_cooldowns.get(source_name, 0)
        if until <= time.time():
            if source_name in self._source_cooldowns:
                self._source_cooldowns.pop(source_name, None)
            return False
        return True

    def _cooldown_source(self, source_name: str, status: int) -> None:
        seconds = 3600 if status == 403 else 600
        self._source_cooldowns[source_name] = time.time() + seconds
        logger.info(
            f"图片源 {source_name} 返回 HTTP {status}，已临时跳过 {seconds // 60} 分钟"
        )

    def _status_from_exception(self, exc: Exception) -> int | None:
        status = getattr(exc, "status", None)
        if isinstance(status, int):
            return status
        return None

    def _keyword_source_timeout_seconds(self) -> int:
        return _config_int(self.config, "keyword_source_timeout_seconds", 8, 3, 30)

    def _keyword_source_retries(self) -> int:
        return _config_int(self.config, "keyword_source_retries", 1, 0, 3)

    async def _resolve_candidate(
        self,
        session: aiohttp.ClientSession,
        source_name: str,
        keyword: str,
    ) -> ImageCandidate | None:
        if source_name == "wallhaven":
            query = quote_plus(keyword)
            url = (
                "https://wallhaven.cc/api/v1/search"
                f"?q={query}&categories=010&purity=100&sorting=random"
                "&atleast=1920x1080&ratios=16x9&per_page=8"
            )
            data = await self._get_json(
                session,
                url,
                timeout_seconds=self._keyword_source_timeout_seconds(),
                retries=self._keyword_source_retries(),
            )
            items = data.get("data", []) if isinstance(data, dict) else []
            random.shuffle(items)
            for item in items:
                path = item.get("path")
                if path:
                    return ImageCandidate(
                        source="Wallhaven",
                        url=path,
                        keyword=keyword,
                        credit_url=item.get("url", ""),
                    )
            return None

        if source_name == "konachan":
            tags = quote_plus(f"rating:safe {keyword} width:>=1920 height:>=1080")
            url = f"https://konachan.net/post.json?limit=8&tags={tags}"
            data = await self._get_json(
                session,
                url,
                timeout_seconds=self._keyword_source_timeout_seconds(),
                retries=0,
            )
            items = data if isinstance(data, list) else []
            random.shuffle(items)
            for item in items:
                path = item.get("file_url") or item.get("sample_url")
                if path:
                    return ImageCandidate(
                        source="Konachan",
                        url=path,
                        keyword=keyword,
                        credit_url=f"https://konachan.net/post/show/{item.get('id', '')}",
                    )
            return None

        if source_name == "safebooru":
            min_score = _config_int(self.config, "safebooru_min_score", 5, 0, 100)
            excluded_tags = _split_semicolon(
                _config_get(
                    self.config,
                    "safebooru_excluded_tags",
                    DEFAULT_SAFEBOORU_EXCLUDED_TAGS,
                )
            )
            query_parts = [
                "rating:safe",
                keyword,
                "sort:score:desc",
                f"score:>={min_score}",
            ]
            query_parts.extend(f"-{tag}" for tag in excluded_tags)
            tags = quote_plus(" ".join(query_parts))
            url = (
                "https://safebooru.org/index.php"
                f"?page=dapi&s=post&q=index&json=1&limit=100&tags={tags}"
            )
            data = await self._get_json(
                session,
                url,
                timeout_seconds=self._keyword_source_timeout_seconds(),
                retries=self._keyword_source_retries(),
            )
            items = data if isinstance(data, list) else []
            picked = self._pick_safebooru_item(items, min_score, excluded_tags)
            if not picked:
                return None
            path = picked.get("file_url") or picked.get("sample_url")
            if not path:
                return None
            return ImageCandidate(
                source="Safebooru",
                url=path,
                keyword=keyword,
                credit_url=f"https://safebooru.org/index.php?page=post&s=view&id={picked.get('id', '')}",
            )

        if source_name == "zhuqiy":
            return ImageCandidate(
                source="ZHUQIY",
                url="https://rimg.zhuqiy.top/api/random?type=pc",
                keyword="random anime landscape",
                credit_url="https://r.zhuqiy.com/en/",
            )

        if source_name == "waifu_im":
            url = (
                "https://api.waifu.im/images?Orientation=Landscape"
                "&Width=%3E%3D1920&Height=%3E%3D1080&IsNsfw=False&PageSize=1"
            )
            data = await self._get_json(session, url)
            items = data.get("items", []) if isinstance(data, dict) else []
            if items and items[0].get("url"):
                return ImageCandidate(
                    source="Waifu.im",
                    url=items[0]["url"],
                    keyword="random anime landscape",
                    credit_url=items[0].get("source", "") or "https://www.waifu.im/",
                )
            return None

        return None

    def _pick_safebooru_item(
        self,
        items: list[Any],
        min_score: int,
        excluded_tags: list[str],
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        excluded = {tag.lower().strip().replace(" ", "_") for tag in excluded_tags if tag.strip()}
        for item in items:
            if not isinstance(item, dict):
                continue
            tags = {
                tag.lower().strip()
                for tag in str(item.get("tags", "")).replace("\n", " ").split()
                if tag.strip()
            }
            if tags.intersection(excluded):
                continue
            try:
                width = int(item.get("width", 0) or 0)
                height = int(item.get("height", 0) or 0)
                score = int(item.get("score", 0) or 0)
            except (TypeError, ValueError):
                continue
            if score < min_score:
                continue
            if width < 1000 or height < 700 or width / max(height, 1) < 1.1:
                continue
            path = str(item.get("file_url") or item.get("sample_url") or "")
            if not path:
                continue
            suffix = Path(urlparse(path).path).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            candidates.append(item)

        if not candidates:
            return None
        candidates.sort(key=lambda item: int(item.get("score", 0) or 0), reverse=True)
        return random.choice(candidates[: min(len(candidates), 12)])

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        timeout_seconds: int | None = None,
        retries: int = 0,
    ) -> Any:
        attempts = max(0, retries) + 1
        timeout = aiohttp.ClientTimeout(total=timeout_seconds) if timeout_seconds else None
        last_exc: Exception | None = None
        for index in range(attempts):
            try:
                kwargs: dict[str, Any] = {"headers": HTTP_HEADERS}
                if timeout:
                    kwargs["timeout"] = timeout
                async with session.get(url, **kwargs) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            except Exception as exc:
                last_exc = exc
                status = self._status_from_exception(exc)
                if index >= attempts - 1 or status in {401, 403, 404}:
                    raise
                await asyncio.sleep(0.35 * (index + 1))
        if last_exc:
            raise last_exc
        raise RuntimeError("JSON 请求失败")

    async def _download_and_validate(
        self,
        session: aiohttp.ClientSession,
        candidate: ImageCandidate,
    ) -> Path | None:
        cache_key = _sha256_text(candidate.url)
        ext = self._extension_from_url(candidate.url)
        dest = self._cache_dir / f"{cache_key}{ext}"
        is_random_endpoint = "api/random" in candidate.url
        if (not is_random_endpoint) and dest.exists() and self._is_valid_landscape_image(dest):
            return dest

        tmp = self._cache_dir / f"{cache_key}.tmp"
        try:
            async with session.get(candidate.url, headers=HTTP_HEADERS) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")
                data = await resp.read()
                if "image" not in content_type.lower() and not data.startswith(
                    (b"\xff\xd8", b"\x89PNG", b"RIFF")
                ):
                    logger.info(f"跳过非图片响应: {candidate.source} {content_type}")
                    return None
            await asyncio.to_thread(tmp.write_bytes, data)
            if not await asyncio.to_thread(self._is_valid_landscape_image, tmp):
                tmp.unlink(missing_ok=True)
                return None
            content_ext = self._extension_from_content_type(content_type)
            if is_random_endpoint:
                cache_key = hashlib.sha256(data).hexdigest()
                dest = self._cache_dir / f"{cache_key}{content_ext or ext}"
                if dest.exists() and self._is_valid_landscape_image(dest):
                    return dest
            tmp.replace(dest)
            return dest
        finally:
            tmp.unlink(missing_ok=True)

    def _is_valid_landscape_image(self, path: Path) -> bool:
        try:
            with Image.open(path) as img:
                width, height = img.size
                return width >= 1000 and height >= 700 and width / max(height, 1) >= 1.1
        except Exception as exc:
            logger.info(f"图片校验失败，继续尝试其他图片: {path} | {exc}")
            return False

    def _extension_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            return suffix
        return ".jpg"

    def _extension_from_content_type(self, content_type: str) -> str:
        lowered = content_type.lower()
        if "webp" in lowered:
            return ".webp"
        if "png" in lowered:
            return ".png"
        if "jpeg" in lowered or "jpg" in lowered:
            return ".jpg"
        return ""

    def _get_user_display(self, event: AstrMessageEvent) -> UserDisplay:
        user_id = _raw_user_id(event)
        name = self._first_non_empty(
            self._raw_sender_value(event, "card"),
            self._raw_sender_value(event, "nickname"),
            self._call_event_getter(event, "get_sender_name"),
            self._message_sender_value(event, "nickname"),
            user_id,
        )
        avatar_url = self._first_non_empty(
            self._raw_sender_value(event, "avatar"),
            self._raw_sender_value(event, "avatar_url"),
            self._raw_sender_value(event, "face"),
        )
        if not avatar_url and user_id.isdigit():
            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        return UserDisplay(
            user_id=user_id,
            name=self._clean_display_name(name, user_id),
            avatar_url=avatar_url,
        )

    def _call_event_getter(self, event: AstrMessageEvent, name: str) -> str:
        getter = getattr(event, name, None)
        if not callable(getter):
            return ""
        try:
            return str(getter() or "")
        except Exception:
            return ""

    def _raw_sender_value(self, event: AstrMessageEvent, key: str) -> str:
        try:
            raw = getattr(event.message_obj, "raw_message", None)
        except Exception:
            raw = None
        sender = self._item_from_mapping_or_object(raw, "sender")
        return self._value_from_mapping_or_object(sender, key)

    def _message_sender_value(self, event: AstrMessageEvent, key: str) -> str:
        try:
            sender = getattr(event.message_obj, "sender", None)
        except Exception:
            sender = None
        return self._value_from_mapping_or_object(sender, key)

    def _value_from_mapping_or_object(self, obj: Any, key: str) -> str:
        value = self._item_from_mapping_or_object(obj, key)
        if value is None:
            return ""
        return str(value)

    def _item_from_mapping_or_object(self, obj: Any, key: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _first_non_empty(self, *values: str) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _clean_display_name(self, name: str, user_id: str) -> str:
        cleaned = "".join(ch for ch in str(name or "").strip() if ch.isprintable())
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            cleaned = f"QQ {user_id[-4:]}" if user_id and user_id != "unknown" else "今日测运者"
        return cleaned[:32]

    async def _fetch_user_avatar(self, user: UserDisplay) -> str:
        if not _config_bool(self.config, "show_user_badge", True):
            return ""
        if not user.avatar_url:
            return ""
        cache_key = _sha256_text(user.avatar_url)
        dest = self._avatars_dir / f"{cache_key}.jpg"
        if (
            dest.exists()
            and not self._avatar_cache_expired(dest)
            and await asyncio.to_thread(self._is_valid_avatar_image, dest)
        ):
            return str(dest)

        session = await self._get_session()
        tmp = self._avatars_dir / f"{cache_key}.tmp"
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with session.get(user.avatar_url, headers=HTTP_HEADERS, timeout=timeout) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")
                data = await resp.read()
                if "image" not in content_type.lower() and not data.startswith(
                    (b"\xff\xd8", b"\x89PNG", b"RIFF")
                ):
                    return ""
            await asyncio.to_thread(tmp.write_bytes, data)
            if not await asyncio.to_thread(self._is_valid_avatar_image, tmp):
                return ""
            tmp.replace(dest)
            return str(dest)
        except Exception as exc:
            logger.info(f"QQ 头像获取失败，将使用占位头像: {user.user_id} | {exc}")
            return ""
        finally:
            tmp.unlink(missing_ok=True)

    def _is_valid_avatar_image(self, path: Path) -> bool:
        try:
            with Image.open(path) as img:
                width, height = img.size
                return width >= 16 and height >= 16
        except Exception:
            return False

    def _avatar_cache_expired(self, path: Path) -> bool:
        days = _config_int(self.config, "avatar_cache_days", 5, -1, 3650)
        if days < 0:
            return False
        if days == 0:
            return True
        try:
            age_seconds = time.time() - path.stat().st_mtime
        except OSError:
            return True
        return age_seconds > days * 86400

    def _render_trend_card(
        self,
        image_path: Path,
        output_path: Path,
        points: list[tuple[str, int | None]],
        display_name: str,
        avatar_path: str,
        image_source: str,
        image_keyword: str,
    ) -> None:
        bg = self._create_base_card_image(image_path, image_source, image_keyword)
        draw = ImageDraw.Draw(bg)

        title_font = self._font(44, bold=True)
        body_font = self._font(28)

        valid_scores = [score for _day, score in points if score is not None]
        latest_score = valid_scores[-1] if valid_scores else 0
        accent = self._accent_for_score(latest_score)

        draw.text((90, 850), "近7天运势曲线", font=title_font, fill=(53, 48, 43))
        if valid_scores:
            average = round(sum(valid_scores) / len(valid_scores), 1)
            best = max(valid_scores)
            summary = f"记录 {len(valid_scores)} 天  平均 {average}  最高 {best}"
        else:
            summary = "暂无运势记录"
        draw.text((92, 910), summary, font=body_font, fill=(98, 87, 78))

        self._draw_trend_chart(draw, (118, 990, 962, 1190), points, accent)

        if _config_bool(self.config, "show_user_badge", True):
            self._draw_user_badge(bg, draw, display_name, avatar_path)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_card_image(bg, output_path)

    def _draw_trend_chart(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        points: list[tuple[str, int | None]],
        accent: tuple[int, int, int],
    ) -> None:
        left, top, right, bottom = box
        width = right - left
        height = bottom - top
        axis_color = (182, 170, 157)
        grid_color = (226, 218, 207)
        text_color = (98, 87, 78)
        label_font = self._font(20)
        score_font = self._font(24, bold=True)

        for tick in (100, 75, 50, 25, 0):
            y = top + int((100 - tick) / 100 * height)
            draw.line((left, y, right, y), fill=grid_color, width=1)
            label = str(tick)
            bbox = draw.textbbox((0, 0), label, font=label_font)
            draw.text(
                (left - 14 - (bbox[2] - bbox[0]), y - 11),
                label,
                font=label_font,
                fill=text_color,
            )

        draw.line((left, top, left, bottom), fill=axis_color, width=2)
        draw.line((left, bottom, right, bottom), fill=axis_color, width=2)

        coordinates: list[tuple[int, int] | None] = []
        total = max(1, len(points) - 1)
        for index, (day_text, score) in enumerate(points):
            x = left + int(width * index / total)
            short_day = self._format_short_date(day_text)
            day_bbox = draw.textbbox((0, 0), short_day, font=label_font)
            draw.text(
                (x - (day_bbox[2] - day_bbox[0]) / 2, bottom + 18),
                short_day,
                font=label_font,
                fill=text_color,
            )
            if score is None:
                draw.ellipse((x - 5, bottom - 5, x + 5, bottom + 5), fill=(214, 205, 194))
                coordinates.append(None)
                continue
            y = top + int((100 - score) / 100 * height)
            coordinates.append((x, y))

        segment: list[tuple[int, int]] = []
        for point in coordinates + [None]:
            if point is None:
                if len(segment) >= 2:
                    draw.line(segment, fill=(184, 143, 98), width=8, joint="curve")
                    draw.line(segment, fill=accent, width=5, joint="curve")
                segment = []
                continue
            segment.append(point)

        for (_day_text, score), point in zip(points, coordinates):
            if point is None or score is None:
                continue
            x, y = point
            draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=(255, 253, 248), outline=accent, width=4)
            label = str(score)
            bbox = draw.textbbox((0, 0), label, font=score_font)
            draw.text(
                (x - (bbox[2] - bbox[0]) / 2, y - 42),
                label,
                font=score_font,
                fill=accent,
            )

    def _format_short_date(self, day_text: str) -> str:
        try:
            parsed = date.fromisoformat(day_text)
            return parsed.strftime("%m/%d")
        except ValueError:
            return day_text[-5:]

    def _render_card(
        self,
        image_path: Path,
        output_path: Path,
        score: int,
        tier: str,
        text: str,
        streak: int,
        image_source: str,
        image_keyword: str,
        display_name: str = "",
        avatar_path: str = "",
    ) -> None:
        if _config_bool(self.config, "enable_shared_base_cards", True):
            base_path = self._base_card_path(image_path, image_source, image_keyword)
            if not base_path.exists():
                self._render_base_card(image_path, base_path, image_source, image_keyword)
            with Image.open(base_path) as raw_base:
                bg = raw_base.convert("RGBA")
        else:
            bg = self._create_base_card_image(image_path, image_source, image_keyword)

        draw = ImageDraw.Draw(bg)

        font_large = self._font(160, bold=True)
        font_body = self._font(38)
        font_small = self._font(28)

        accent = self._accent_for_score(score)
        draw.text((86, 838), str(score), font=font_large, fill=accent)
        draw.text((90, 1006), "今日运势", font=font_small, fill=(98, 87, 78))

        wrapped = self._wrap_text(text, font_body, 820)
        y = 1046
        for line in wrapped[:4]:
            draw.text((90, y), line, font=font_body, fill=(53, 48, 43))
            y += 54

        streak_text = f"您已连续测运 {streak} 天"
        streak_bbox = draw.textbbox((0, 0), streak_text, font=font_small)
        draw.text(
            (1004 - (streak_bbox[2] - streak_bbox[0]), 1304),
            streak_text,
            font=font_small,
            fill=(80, 74, 68),
        )

        if _config_bool(self.config, "show_user_badge", True):
            self._draw_user_badge(bg, draw, display_name, avatar_path)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_card_image(bg, output_path)

    def _render_base_card(
        self,
        image_path: Path,
        output_path: Path,
        image_source: str,
        image_keyword: str,
    ) -> None:
        base = self._create_base_card_image(image_path, image_source, image_keyword)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        base.save(output_path, "PNG")

    def _create_base_card_image(
        self,
        image_path: Path,
        image_source: str,
        image_keyword: str,
    ) -> Image.Image:
        bg = Image.new("RGB", CANVAS_SIZE, (246, 242, 236))
        with Image.open(image_path) as raw:
            hero = ImageOps.exif_transpose(raw).convert("RGB")
        hero = ImageOps.fit(hero, (CANVAS_SIZE[0], IMAGE_AREA_HEIGHT), Image.Resampling.LANCZOS)
        bg.paste(hero, (0, 0))

        overlay = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        panel_top = 800
        draw.rounded_rectangle(
            (42, panel_top, 1038, 1392),
            radius=34,
            fill=(255, 253, 248, 238),
            outline=(255, 255, 255, 180),
            width=2,
        )
        bg = Image.alpha_composite(bg.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(bg)

        font_tiny = self._font(22)

        if _config_bool(self.config, "show_image_source_notice", True):
            source_text = f"Image source: {image_source}"
            if image_keyword:
                source_text += f" / {image_keyword}"
            draw.text((76, 1360), source_text[:86], font=font_tiny, fill=(124, 116, 108))

        return bg

    def _base_card_path(self, image_path: Path, image_source: str, image_keyword: str) -> Path:
        try:
            stat = image_path.stat()
            image_sig = f"{image_path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            image_sig = str(image_path)
        key = _sha256_text(
            "|".join(
                [
                    image_sig,
                    image_source,
                    image_keyword,
                    str(_config_bool(self.config, "show_image_source_notice", True)),
                    "base-card-v2",
                ]
            )
        )
        return self._base_cards_dir / f"{key}.png"

    def _save_card_image(self, image: Image.Image, output_path: Path) -> None:
        output_width = _config_int(self.config, "output_width", 1080, 720, 1440)
        output_height = _config_int(self.config, "output_height", 1440, 960, 1920)
        if image.size != (output_width, output_height):
            image = image.resize((output_width, output_height), Image.Resampling.LANCZOS)
        image.convert("RGB").save(
            output_path,
            "JPEG",
            quality=_config_int(self.config, "jpeg_quality", 88, 60, 95),
            optimize=_config_bool(self.config, "jpeg_optimize", False),
        )

    def _draw_user_badge(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        display_name: str,
        avatar_path: str,
    ) -> None:
        x, y, size = 76, 1284, 58
        name = self._ellipsize_text(display_name or "今日测运者", self._font(28), 360)

        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.ellipse((x - 2, y + 2, x + size + 2, y + size + 6), fill=(0, 0, 0, 34))
        canvas.alpha_composite(shadow)

        avatar_drawn = False
        if avatar_path:
            try:
                with Image.open(avatar_path) as raw:
                    avatar = ImageOps.exif_transpose(raw).convert("RGBA")
                avatar = ImageOps.fit(avatar, (size, size), Image.Resampling.LANCZOS)
                mask = Image.new("L", (size, size), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse((0, 0, size, size), fill=255)
                canvas.paste(avatar, (x, y), mask)
                avatar_drawn = True
            except Exception as exc:
                logger.info(f"头像绘制失败，将使用占位头像: {avatar_path} | {exc}")

        if not avatar_drawn:
            draw.ellipse((x, y, x + size, y + size), fill=(229, 218, 204))
            initial = (name[:1] or "运").upper()
            initial_font = self._font(28, bold=True)
            initial_bbox = draw.textbbox((0, 0), initial, font=initial_font)
            draw.text(
                (
                    x + (size - (initial_bbox[2] - initial_bbox[0])) / 2,
                    y + (size - (initial_bbox[3] - initial_bbox[1])) / 2 - 2,
                ),
                initial,
                font=initial_font,
                fill=(122, 103, 84),
            )

        draw.ellipse((x, y, x + size, y + size), outline=(255, 255, 255, 220), width=3)
        draw.text((x + size + 14, y + 13), name, font=self._font(28), fill=(74, 66, 59))

    def _wrap_text(self, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        leading_punctuation = "，。！？；：、,.!?;:"
        lines: list[str] = []
        for paragraph in text.splitlines():
            current = ""
            for char in paragraph:
                trial = current + char
                bbox = font.getbbox(trial)
                if bbox[2] - bbox[0] <= max_width:
                    current = trial
                else:
                    if char in leading_punctuation and current:
                        current += char
                        lines.append(current)
                        current = ""
                        continue
                    if current:
                        lines.append(current)
                    current = char
            if current:
                lines.append(current)
        if not lines:
            return textwrap.wrap(text, width=18) or [text]
        return lines

    def _ellipsize_text(self, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        if measure.textbbox((0, 0), value, font=font)[2] <= max_width:
            return value
        ellipsis = "..."
        while value:
            value = value[:-1]
            trial = value.rstrip() + ellipsis
            bbox = measure.textbbox((0, 0), trial, font=font)
            if bbox[2] - bbox[0] <= max_width:
                return trial
        return ellipsis

    def _font(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        cache_key = (size, bold)
        font_cache = getattr(self, "_font_cache", None)
        if font_cache is None:
            font_cache = {}
            setattr(self, "_font_cache", font_cache)
        if cache_key in font_cache:
            return font_cache[cache_key]

        configured = str(_config_get(self.config, "font_path", "") or "").strip()
        candidates: list[str] = []
        if configured:
            candidates.append(configured)
        bundled_font = (
            Path(__file__).resolve().parent
            / "assets"
            / "fonts"
            / "NotoSansSC-JrysSubset.ttf"
        )
        candidates.append(str(bundled_font))
        if os.name == "nt":
            candidates.extend(
                [
                    r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
                    r"C:\Windows\Fonts\simhei.ttf",
                    r"C:\Windows\Fonts\simsun.ttc",
                ]
            )
        candidates.extend(
            [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
                if bold
                else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf"
                if bold
                else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
                "/usr/share/fonts/truetype/noto/NotoSansSC-Bold.ttf"
                if bold
                else "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/truetype/arphic/uming.ttc",
                "/usr/share/fonts/truetype/arphic/ukai.ttc",
                "/usr/share/fonts",
                "/usr/local/share/fonts",
                "/System/Library/Fonts/PingFang.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                if bold
                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
        )
        for path in candidates:
            try:
                font_file = self._resolve_font_file(path, bold)
                if font_file:
                    font = ImageFont.truetype(font_file, size=size)
                    self._apply_font_weight(font, bold)
                    font_cache[cache_key] = font
                    return font
            except Exception as exc:
                logger.warning(f"加载字体失败: {path} | {exc}")
                continue

        logger.warning(
            "未找到可用 TrueType/OpenType 字体，海报文字可能过小或无法显示中文。"
            "请安装 fonts-noto-cjk 或在插件配置 font_path 中填写中文字体路径。"
        )
        font = ImageFont.load_default(size=max(10, min(size, 64)))
        font_cache[cache_key] = font
        return font

    def _resolve_font_file(self, path: str, bold: bool = False) -> str:
        if not path:
            return ""
        target = Path(path)
        if target.is_file():
            return str(target)
        if not target.is_dir():
            return ""

        preferred = [
            "NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc",
            "NotoSansCJKsc-Bold.otf" if bold else "NotoSansCJKsc-Regular.otf",
            "NotoSansSC-Bold.ttf" if bold else "NotoSansSC-Regular.ttf",
            "SourceHanSansSC-Bold.otf" if bold else "SourceHanSansSC-Regular.otf",
            "SourceHanSansCN-Bold.otf" if bold else "SourceHanSansCN-Regular.otf",
            "wqy-microhei.ttc",
            "wqy-zenhei.ttc",
            "simhei.ttf",
            "msyh.ttc",
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        ]
        for name in preferred:
            try:
                matches = list(target.rglob(name))
                if matches:
                    return str(matches[0])
            except Exception:
                continue
        return ""

    def _apply_font_weight(self, font: ImageFont.ImageFont, bold: bool) -> None:
        setter = getattr(font, "set_variation_by_axes", None)
        if not callable(setter):
            return
        try:
            setter([700 if bold else 400])
        except Exception:
            return

    def _accent_for_score(self, score: int) -> tuple[int, int, int]:
        if score < 25:
            return (115, 103, 94)
        if score < 50:
            return (80, 124, 154)
        if score < 70:
            return (67, 142, 118)
        if score < 90:
            return (205, 127, 52)
        if score < 100:
            return (207, 83, 104)
        return (218, 58, 74)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            connector = aiohttp.TCPConnector(limit=8)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    def _get_plugin_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data_root = Path(get_astrbot_data_path())
            plugin_name = getattr(self, "name", PLUGIN_NAME) or PLUGIN_NAME
            return data_root / "plugin_data" / plugin_name
        except Exception:
            return Path(__file__).resolve().parent / "data"

    def _load_users(self) -> dict[str, Any]:
        if not self._users_file.exists():
            return {}
        try:
            return json.loads(self._users_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"读取用户运势数据失败，将重建: {exc}")
            return {}

    def _save_users(self, users: dict[str, Any]) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._users_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._users_file)

    async def terminate(self):
        if self._background_task and not self._background_task.done():
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()


LOW_TEXTS = [
    "今天的气流有点逆，不过低运势不是坏结局。把目标拆小一点，稳住节奏，少做冲动决定。",
    "今日适合保守推进。遇到卡顿先暂停，补给心情和体力，等风向变好再全力冲刺。",
    "运势偏低时更需要温柔地对待自己。少硬碰硬，多做确定的小事，夜晚会轻一点。",
]
MID_TEXTS = [
    "今天风平浪静，适合整理、复盘和推进手边任务。没有大爆发，但每一步都算数。",
    "中运势的一天很适合稳扎稳打。把该完成的事情收尾，给之后的小幸运腾出空间。",
    "今日没有明显阻力，也没有太多捷径。按自己的节奏来，平稳本身就是一种好消息。",
]
HIGH_TEXTS = [
    "今日有小幸运路过。适合主动沟通、推进计划，也适合给自己安排一点期待。",
    "运势正在抬头，很多事会比想象中顺一些。抓住轻松的窗口，把想做的事往前推一步。",
    "今天适合行动。灵感和效率都在身边，保持清醒和热情，你会得到不错的反馈。",
]
GREAT_TEXTS = [
    "好运浓度很高的一天。适合尝试、表达和做决定，记得把机会接稳。",
    "今天的你很容易被世界温柔回应。大胆一点，重要的事情可以往前推进。",
    "高光感很强的一天。保持专注，别浪费这份手感，你值得把好事接住。",
]
PERFECT_TEXTS = [
    "今日心想事成。适合许愿、开局、告白、抽卡和迈出关键一步，愿好运站在你这边。",
    "满分运势降临。今天请相信自己的直觉，把最想做的事放到最前面。",
]
