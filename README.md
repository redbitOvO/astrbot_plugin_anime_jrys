# 动漫今日运势

这是一个 AstrBot 今日运势插件。用户发送 `jrys`、`今日运势`、`运势`，或使用 `/jrys`、`/今日运势`、`/运势` 指令后，插件会返回一张带有随机动漫/二游横图的运势海报。

## 功能

- 今日运势分数范围为 `0-100`。
- 同一用户同一天结果固定，重复触发不会增加连续天数。
- 跨自然日连续触发会累计“您已连续测运 X 天”，中断后重置。
- 图片上半部分展示随机动漫横图，下半部分展示分数、文案和连续天数。
- 默认只请求 SFW 内容。

## 图片源

插件不内置任何第三方图片资源，也不打包图片库，只会在运行时请求第三方公开接口并缓存返回图片。

关键词源：

- Wallhaven: `https://wallhaven.cc/help/api`
- Konachan: `https://konachan.net/help/api`

兜底源：

- ZHUQIY: `https://r.zhuqiy.com/en/`
- Waifu.im: `https://docs.waifu.im/docs/api/`

## 配置

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

### `konachan_tags`

Konachan 标签。多个标签使用英文分号 `;` 分隔。

示例：

```text
wuthering_waves;genshin_impact;arknights
```

Konachan 在部分云服务器上可能返回 `403 Forbidden`。因此插件默认不启用 Konachan；如果你的服务器可正常访问，可以手动填写标签：

```text
genshin_impact;honkai:_star_rail;honkai_impact;zenless_zone_zero;wuthering_waves;arknights;blue_archive;azur_lane;girls_frontline;goddess_of_victory:_nikke;punishing:_gray_raven;snowbreak:_containment_zone;path_to_nowhere;reverse:1999;fate/grand_order
```

当 `wallhaven_keywords` 或 `konachan_tags` 不为空时，插件会优先使用这两个关键词源。配置项中存在多个关键词时，每次返图会随机选择其中一个。

### 其他配置

- `enable_zhuqiy_fallback`: 关键词源失败后是否启用 ZHUQIY 兜底。
- `enable_waifu_im_fallback`: 关键词源和 ZHUQIY 失败后是否启用 Waifu.im 兜底。
- `show_image_source_notice`: 是否在海报底部显示图片来源。
- `font_path`: 自定义中文字体路径。插件已内置中文子集字体，一般无需配置。

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
- Waifu.im 使用 `IsNsfw=False`
