from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import textwrap
from dataclasses import dataclass
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
DEFAULT_KONACHAN_TAGS = (
    "genshin_impact;honkai:_star_rail;honkai_impact;zenless_zone_zero;"
    "wuthering_waves;arknights;blue_archive;azur_lane;girls_frontline;"
    "goddess_of_victory:_nikke;punishing:_gray_raven;snowbreak:_containment_zone;"
    "path_to_nowhere;reverse:1999;fate/grand_order"
)

TRIGGER_WORDS = {"jrys", "今日运势", "运势"}
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


def _split_semicolon(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _today_str() -> str:
    return date.today().isoformat()


def _safe_user_id(event: AstrMessageEvent) -> str:
    try:
        user_id = str(event.get_sender_id())
    except Exception:
        user_id = ""
    if not user_id:
        try:
            user_id = str(event.message_obj.sender.user_id)
        except Exception:
            user_id = "unknown"
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
        self._cards_dir = self._data_dir / "cards"
        self._users_file = self._data_dir / "users.json"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cards_dir.mkdir(parents=True, exist_ok=True)

    @filter.command("jrys", alias={"今日运势", "运势"})
    async def jrys(self, event: AstrMessageEvent):
        """生成今日动漫/二游运势图。"""
        async for result in self._reply_fortune(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_plain_trigger(self, event: AstrMessageEvent):
        """兼容用户直接发送 jrys / 今日运势 / 运势。"""
        text = getattr(event, "message_str", "").strip()
        if text not in TRIGGER_WORDS:
            return
        async for result in self._reply_fortune(event):
            yield result

    async def _reply_fortune(self, event: AstrMessageEvent):
        try:
            record = await self._get_or_create_today_record(event)
            yield event.image_result(record.card_path)
            event.stop_event()
        except Exception as exc:
            logger.error(f"生成今日运势失败: {exc}")
            yield event.plain_result("今日运势图生成失败了，请稍后再试。")
            event.stop_event()

    async def _get_or_create_today_record(self, event: AstrMessageEvent) -> FortuneRecord:
        user_key = _safe_user_id(event)
        today = _today_str()
        async with self._lock:
            users = await asyncio.to_thread(self._load_users)
            existing = users.get(user_key, {})
            if existing.get("day") == today and existing.get("card_path"):
                card_path = Path(existing["card_path"])
                if card_path.exists():
                    return FortuneRecord(**existing)

            streak = self._next_streak(existing, today)
            score, tier, text = self._roll_fortune()
            image = await self._fetch_random_image()
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
            )
            users[user_key] = record.__dict__
            await asyncio.to_thread(self._save_users, users)
            return record

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

    async def _fetch_random_image(self) -> ImageCandidate:
        session = await self._get_session()
        attempts = self._build_source_attempts()

        for source_name, keyword in attempts:
            try:
                candidate = await self._resolve_candidate(session, source_name, keyword)
                if not candidate:
                    continue
                local_path = await self._download_and_validate(session, candidate)
                if local_path:
                    return ImageCandidate(
                        source=candidate.source,
                        url=str(local_path),
                        keyword=candidate.keyword,
                        credit_url=candidate.credit_url,
                    )
            except Exception as exc:
                logger.warning(f"图片源 {source_name} 获取失败: {exc}")

        raise RuntimeError("所有图片源均获取失败")

    def _build_source_attempts(self) -> list[tuple[str, str]]:
        wallhaven_keywords = _split_semicolon(
            _config_get(self.config, "wallhaven_keywords", DEFAULT_WALLHAVEN_KEYWORDS)
        )
        konachan_tags = _split_semicolon(
            _config_get(self.config, "konachan_tags", DEFAULT_KONACHAN_TAGS)
        )
        keyword_attempts: list[tuple[str, str]] = []
        fallback_attempts: list[tuple[str, str]] = []

        keyword_sources: list[tuple[str, list[str]]] = []
        if wallhaven_keywords:
            keyword_sources.append(("wallhaven", wallhaven_keywords))
        if konachan_tags:
            keyword_sources.append(("konachan", konachan_tags))

        if keyword_sources:
            for name, values in keyword_sources:
                sample = random.sample(values, k=min(len(values), 4))
                keyword_attempts.extend((name, value) for value in sample)

        if bool(_config_get(self.config, "enable_zhuqiy_fallback", True)):
            fallback_attempts.append(("zhuqiy", ""))
        if bool(_config_get(self.config, "enable_waifu_im_fallback", True)):
            fallback_attempts.append(("waifu_im", ""))

        random.shuffle(keyword_attempts)
        random.shuffle(fallback_attempts)
        return keyword_attempts + fallback_attempts

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
            data = await self._get_json(session, url)
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
            data = await self._get_json(session, url)
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

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> Any:
        async with session.get(url, headers=HTTP_HEADERS) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

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
                    logger.warning(f"跳过非图片响应: {candidate.source} {content_type}")
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
            logger.warning(f"图片校验失败: {path} | {exc}")
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
    ) -> None:
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

        font_large = self._font(160, bold=True)
        font_body = self._font(38)
        font_small = self._font(28)
        font_tiny = self._font(22)

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

        if bool(_config_get(self.config, "show_image_source_notice", True)):
            source_text = f"Image source: {image_source}"
            if image_keyword:
                source_text += f" / {image_keyword}"
            draw.text((76, 1360), source_text[:86], font=font_tiny, fill=(124, 116, 108))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        bg.convert("RGB").save(output_path, "JPEG", quality=92, optimize=True)

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

    def _font(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        configured = str(_config_get(self.config, "font_path", "") or "").strip()
        candidates = []
        if configured:
            candidates.append(configured)
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
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/System/Library/Fonts/PingFang.ttc",
            ]
        )
        for path in candidates:
            try:
                if path and Path(path).exists():
                    return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

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
            timeout = aiohttp.ClientTimeout(total=12)
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
