# 图片工具箱 (astrbot_plugin_img_toolbox)

一个整合了「GIF 万能工具箱」与「图像对称镜像」的 AstrBot 图片处理插件，涵盖动图加工、静态图美化与镜像鬼畜效果。

由以下两个插件合并而成：

- [`astrbot_plugin_gifcaijian`](https://github.com/iris1598/astrbot_plugin_gifcaijian) — GIF/APNG/WebP 转换、裁剪、变速、倒放、分解、合成、做旧
- [`astrbot-plugin-pic-mirror`](https://github.com/iris1598/astrbot-plugin-pic-mirror) — 图像对称（左/右/上/下对称、反色），支持 GIF 动画与 @用户头像

## 功能总览

### 🎬 GIF 动图

| 功能 | 指令 | 说明 |
| :--- | :--- | :--- |
| 视频转动图 | `视频转gif [参数]` | 将视频转为 GIF/APNG/WebP，支持时间区间、帧率、缩放控制与自动压体积 |
| 精灵图合成 | `合成1gif` / `合成2gif [网格 时长 边距]` | 将雪碧图按网格逐格合成为动图 |
| GIF变速 | `gif变速 2x` / `gif变速 30fps` | 倍速或帧率变速，>50fps 自动抽帧 |
| 快捷变速 | `加速 [倍数]` / `减速 [倍数]` | 旧指令别名 |
| GIF倒放 | `gif倒放` / `倒放` | 动画倒放 |
| GIF分解 | `gif分解` | 逐帧拆解为静态图（合并转发） |
| 多图合成 | `多图合成gif [每帧秒数]` | 多张图片合成动图，支持转发/合并转发 |

### 🖼️ 静态图

| 功能 | 指令 | 说明 |
| :--- | :--- | :--- |
| 网格裁剪 | `裁剪 3x4 [边距10]` | 宫格裁剪并以合并转发发送 |
| 图片转线稿 | `图片转线稿` | 本地算法生成线稿素描效果 |
| 表情包做旧 | `表情包做旧 [次数]` | 模拟早期互联网传播的做旧效果，动态/静态均可 |

### 🪞 图像对称镜像

| 功能 | 指令 | 说明 |
| :--- | :--- | :--- |
| 左对称 | `左对称` / `mirror left` | 左半边对称到右边 |
| 右对称 | `右对称` / `mirror right` | 右半边对称到左边 |
| 上对称 | `上对称` / `mirror top` | 上半边对称到下面 |
| 下对称 | `下对称` / `mirror bottom` | 下半边对称到上面 |
| 反色 | `反色` / `invert` / `颜色反转` | 反转图像颜色 |
| 帮助 | `图片帮助` / `工具箱帮助` / `对称帮助` / `镜像帮助` | 显示本帮助 |

镜像指令可直接发送（无需唤醒前缀），也支持 `@用户 指令` 处理用户头像。

## 使用示例

```
视频转gif -start 2 -end 4.5 -fps 15 -scale 0.5
合成1gif 8x8 0.05s 边距10
gif变速 2x
gif变速 60fps      # 自动抽帧
多图合成gif 0.5
裁剪 3x3 边距10
表情包做旧 10
左对称
右对称 @张三
```

> 群聊中 GIF 指令需要 @ 机器人（或以 `/` 开头）；镜像指令可直接发送。

## 安装

1. 从 AstrBot Web 管理界面「插件市场」安装，或手动上传本目录 zip。
2. 依赖自动安装：`Pillow`、`aiohttp`、`PyYAML`、`imageio`、`imageio-ffmpeg`。
3. 若想启用「视频转gif」功能，请确认已完成 `pip install imageio[ffmpeg]`。

## 配置说明

可在 AstrBot Web 界面调整，主要配置项：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `output_format` | GIF | 视频转动图的输出格式: GIF / APNG / WEBP |
| `default_scale` | 0.3 | 视频转动图默认缩放比例 |
| `default_fps` | 10 | 视频转动图默认帧率 |
| `max_gif_duration` | 10.0 | 允许截取视频的最大时长（秒） |
| `max_video_size_mb` | 50.0 | 允许处理视频的最大体积（MB） |
| `gif_max_colors` | 256 | 生成 GIF 的最大颜色数 (2-256) |
| `image_size_limit_mb` | 10 | 镜像处理普通图像最大大小 |
| `gif_size_limit_mb` | 15 | 镜像处理 GIF 最大大小 |
| `silent_mode` | true | 镜像处理是否静默（不发提示） |
| `enable_gif` | true | 是否允许处理 GIF 动画 |
| `enable_at_avatar` | true | 是否允许 @用户获取头像处理 |
| `rate_limit_per_minute` | 10 | 每用户每分钟请求频率上限（0 不限制） |
| `max_concurrent_tasks` | 3 | 并发处理上限 |
| `max_gif_frames` | 200 | 镜像处理 GIF 最大帧数 |

完整配置项见 `_conf_schema.json`。

## 安全与稳定性

- 镜像模块内置 SSRF 防护、DNS Rebinding 防护、路径遍历防护、解压炸弹检测（文件大小 / GIF 帧数 / 总像素）。
- 用户频率限制与全局并发上限，避免极端并发打爆资源。
- 生成文件自动清理（`enable_auto_cleanup`）。

## LICENSE

GPL-3.0，详见 [LICENSE](./LICENSE)。