# 动漫今日运势

这是一个 AstrBot 今日运势插件。用户发送 `jrys`、`今日运势`、`运势`，或使用 `/jrys`、`/今日运势`、`/运势` 指令后，插件会返回一张带有随机动漫/二游横图的运势海报。

## 功能

- 今日运势分数范围为 `0-100`。
- 同一用户同一天结果固定，重复触发不会增加连续天数。
- 跨自然日连续触发会累计“您已连续测运 X 天”，中断后重置。
- 图片上半部分展示随机动漫横图，下半部分展示分数、文案和连续天数。
- 群聊中会在海报左下角显示触发用户的头像和昵称，方便区分是谁测的运势。
- 默认只请求 SFW 内容。

## 图片源

插件不内置任何第三方图片资源，也不打包图片库，只会在运行时请求第三方公开接口并缓存返回图片。

关键词源：

- Wallhaven: `https://wallhaven.cc/help/api`，按关键词搜索动漫/二游横图。
- Konachan: `https://konachan.net/help/api`，按标签搜索图片，但部分云服务器 IP 会被返回 `403 Forbidden`，因此默认关闭。
- Safebooru: `https://safebooru.org/index.php?page=help&topic=dapi`，按标签搜索图片。插件会优先取站内 `score` 较高、横图、且不含排除标签的结果。

兜底源：

- ZHUQIY: `https://r.zhuqiy.com/en/`
- Waifu.im: `https://docs.waifu.im/docs/api/`

## 配置

### `enable_wallhaven_source`

是否启用 Wallhaven 关键词源，默认开启。

如果你的云服务器访问 `wallhaven.cc` 经常 `TimeoutError`，可以关闭它，让插件直接使用兜底图源。

### `wallhaven_keywords`

Wallhaven 关键词。多个关键词使用英文分号 `;` 分隔。

示例：

```text
wuthering waves;genshin impact;arknights
```

默认内置热门二游关键词：

```text
genshin impact;honkai star rail;honkai impact 3rd;zenless zone zero;wuthering waves;arknights;blue archive;azur lane;girls frontline;nikke;punishing gray raven;snowbreak containment zone;path to nowhere;reverse 1999;fate grand order;umamusume
```

### `enable_konachan_source`

是否启用 Konachan 标签源，默认关闭。

Konachan 在部分云服务器上会直接返回 `403 Forbidden`。这不是插件参数写错，而是对方站点或中间防护对访问来源做了限制。只有确认自己的服务器能正常访问 Konachan 时，才建议开启。

### `konachan_tags`

Konachan 标签。多个标签使用英文分号 `;` 分隔。

示例：

```text
wuthering_waves;genshin_impact;arknights
```

如果你的服务器可正常访问 Konachan，可以开启 `enable_konachan_source`，再填写标签：

```text
genshin_impact;honkai:_star_rail;honkai_impact;zenless_zone_zero;wuthering_waves;arknights;blue_archive;azur_lane;girls_frontline;goddess_of_victory:_nikke;punishing:_gray_raven;snowbreak:_containment_zone;path_to_nowhere;reverse:1999;fate/grand_order
```

当 `enable_wallhaven_source` 或 `enable_konachan_source` 开启，并且对应关键词/标签配置不为空时，插件会优先使用关键词源。配置项中存在多个关键词时，每次返图会随机选择其中一个。

### `enable_safebooru_source`

是否启用 Safebooru 高分关键词源，默认开启。

Safebooru 可以按二游标签搜索。它没有公开点击量字段，因此插件使用返回数据里的 `score` 作为受欢迎程度的近似指标，并使用 `sort:score:desc` 和 `score:>=N` 优先筛选站内分数更高的作品。

### `safebooru_tags`

Safebooru 标签。多个标签使用英文分号 `;` 分隔。

示例：

```text
wuthering_waves;genshin_impact;honkai:_star_rail;arknights;blue_archive
```

默认内置：

```text
genshin_impact;honkai:_star_rail;honkai_impact;zenless_zone_zero;wuthering_waves;arknights;blue_archive;azur_lane;girls_frontline;goddess_of_victory:_nikke;punishing:_gray_raven;path_to_nowhere;reverse:1999;fate/grand_order;umamusume
```

### `safebooru_min_score`

Safebooru 最低 `score`，默认 `5`。

调高这个值可以进一步减少低质量练习图，但部分冷门标签可能更容易无结果。若某些关键词一直取不到图，可以改成 `3` 或 `0`。

### `safebooru_excluded_tags`

Safebooru 排除标签。多个标签使用英文分号 `;` 分隔。

默认排除：

```text
underwear;bikini;swimsuit;lingerie;nude;naked;sex;explicit;animated_gif;character_sheet;reference_sheet;sketch;comic;manga;ass;bottomless;kiss;french_kiss;tongue;tongue_out;armpits;sports_bra;leotard;midriff;navel;nipples;pectorals;bulge;spread_legs;wide_spread_legs;feet;soles
```

这些标签用于避开擦边图、GIF、设定图、草稿、漫画页等不太适合做运势海报背景的结果。

每次生成图片时，插件对每个已启用关键词源只尝试一个随机关键词/标签。关键词源按 Wallhaven、Konachan、Safebooru 的顺序尝试；关键词源全部失败后，才进入 ZHUQIY / Waifu.im 兜底源。

### `keyword_source_timeout_seconds`

Wallhaven / Konachan 单次请求超时秒数，默认 `8`，范围 `3-30`。

如果云服务器访问 Wallhaven 很慢，可以改成 `5`，让插件更快进入兜底源。

### `keyword_source_retries`

Wallhaven 请求失败后的重试次数，默认 `1`，范围 `0-3`。Konachan 返回 `403` 时不会重试，因为重试通常不会改变结果。

### 其他配置

- `enable_zhuqiy_fallback`: 关键词源失败后是否启用 ZHUQIY 兜底。
- `enable_waifu_im_fallback`: 关键词源和 ZHUQIY 失败后是否启用 Waifu.im 兜底。
- `show_image_source_notice`: 是否在海报底部显示图片来源。
- `show_user_badge`: 是否在海报左下角显示触发用户的头像和昵称，默认开启。
- `font_path`: 自定义中文字体路径。插件已内置中文子集字体，一般无需配置。

## 缓存与性能

插件会把连续测运天数保存在 `users.json`，缓存清理不会删除这个文件，所以自动清理旧图片、旧海报或旧头像不会影响用户连续签到天数。

可配置项：

- `enable_image_prefetch`: 是否启用图片预取池，默认开启。开启后插件会在后台提前下载并校验图片，用户触发时优先使用本地图片池。
- `image_pool_size`: 图片池容量，默认 `8`。设为 `0` 可关闭图片池；数值越大，突发多人触发时越容易快速出图，但占用缓存更多。
- `image_pool_refill_batch`: 每次后台补图数量，默认 `2`。服务器网络慢时建议保持较小。
- `enable_shared_base_cards`: 是否启用多用户共享预渲染底图，默认开启。共享底图不包含用户头像、昵称、分数和文案，只复用背景图、面板和来源标注。
- `cleanup_interval_hours`: 缓存维护间隔，默认 `12` 小时。
- `cache_max_mb`: 缓存总大小上限，默认 `300` MB。超过上限时会优先删除旧缓存；设为 `0` 表示不按大小裁剪。
- `image_cache_retention_days`: 原始图片缓存保留天数，默认 `14` 天。
- `card_retention_days`: 最终海报缓存保留天数，默认 `7` 天。
- `base_card_retention_days`: 共享底图缓存保留天数，默认 `7` 天。
- `avatar_cache_days`: 头像缓存过期天数，默认 `5` 天。设为 `0` 表示每次重新拉取头像；设为 `-1` 表示不过期。
- `output_width` / `output_height`: 输出图片尺寸，默认 `1080x1440`。调小可以减少文件体积、提升发送速度。
- `jpeg_quality`: JPEG 输出质量，默认 `88`，范围 `60-95`。更低更快更小，更高更清晰。
- `jpeg_optimize`: 是否启用 JPEG 优化压缩，默认关闭。开启后文件通常更小，但保存图片会多耗一点 CPU。

推荐配置：

- 速度优先：`output_width=900`、`output_height=1200`、`jpeg_quality=82`、`jpeg_optimize=false`、`image_pool_size=12`
- 画质优先：`output_width=1080`、`output_height=1440`、`jpeg_quality=90`、`jpeg_optimize=true`、`image_pool_size=8`

## QQ 用户信息

插件会优先使用 AstrBot 事件中的发送者昵称；在 QQ 群聊中，如果原始事件里包含群名片 `card`，会优先使用群名片。头像会优先读取事件里的头像字段，若没有，则使用 QQ 号构造公开头像地址：

```text
https://q1.qlogo.cn/g?b=qq&nk=<QQ号>&s=100
```

头像获取失败时，海报仍会正常生成，并显示圆形占位头像和昵称。

## 图源故障排查

如果后台出现关键词源失败：

- `Konachan 403 Forbidden`: 通常是服务器 IP 或访问链路被 Konachan / Cloudflare 拒绝。保持 `enable_konachan_source = false` 即可。
- `Wallhaven TimeoutError`: 通常是服务器到 `wallhaven.cc` 的网络路由不稳定或被防火墙影响。可以降低 `keyword_source_timeout_seconds`，或关闭 `enable_wallhaven_source`。
- `Safebooru 无结果`: 通常是标签较冷门或 `safebooru_min_score` 过高。可以降低最低分数，或为该源补充更常见的作品/角色标签。

只要 ZHUQIY / Waifu.im 兜底源可用，插件仍会正常出图。

## 字体

插件内置 `assets/fonts/NotoSansSC-JrysSubset.ttf`，这是 Noto Sans SC 的插件专用子集字体，只包含运势海报需要的字符。它来自 Noto Sans CJK / Noto Sans SC，使用 SIL Open Font License 1.1 授权，许可证见 `assets/fonts/OFL.txt`。

因此 Docker / Linux 用户默认不需要额外安装中文字体。如果你想替换成自己的字体，可以在插件配置中填写：


```text
/usr/share/fonts
```

或具体字体文件：

```text
/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
```

## 运势概率

| 分数 | 档位 | 概率 |
|---|---|---:|
| 0-10 | 极低运势 | 4% |
| 11-24 | 低运势 | 16% |
| 25-49 | 中运势 | 25% |
| 50-69 | 偏高运势 | 25% |
| 70-89 | 高运势 | 20% |
| 90-99 | 极高运势 | 8% |
| 100 | 最高运势 | 2% |

低运势整体概率约 `20%`，与中运势接近，但低于 50 分以上运势的总体概率。

## 安装

将本目录放到 AstrBot 的插件目录：

```text
AstrBot/data/plugins/astrbot_plugin_anime_jrys
```

然后在 AstrBot WebUI 的插件管理页面重载插件。

## 版权与免责声明

本插件仅提供图片接口聚合、缓存和运势海报生成能力。图片内容来自第三方公开接口，版权归原作者、画师、上传者、平台或对应权利方所有。

本插件默认仅请求 SFW 内容：

- Wallhaven 使用 `purity=100`
- Konachan 使用 `rating:safe`
- Safebooru 使用 `rating:safe`，并默认排除若干擦边、GIF、草稿和设定图标签
- Waifu.im 使用 `IsNsfw=False`
