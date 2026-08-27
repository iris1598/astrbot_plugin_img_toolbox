"""
图片工具箱（Image Toolbox）
=========================

整合自两个插件：

1. astrbot_plugin_gifcaijian（GIF 万能工具箱）
   - 视频转GIF / APNG / WebP
   - 精灵图（Sprite Sheet）合成动图
   - GIF 变速（倍速 / 帧率，>50fps 自动抽帧）
   - GIF 倒放 / GIF 分解 / 多图合成 GIF
   - 网格裁剪 / 图片转线稿 / 表情包做旧

2. astrbot-plugin-pic-mirror（图像对称）
   - 左/右/上/下对称、反色
   - 支持静态图、GIF 动画、@用户头像
   - 内置 SSRF 防护、路径遍历防护、频率限制等安全机制

使用方式：回复含图消息后发送对应指令；群聊请 @ 机器人。
"""

import asyncio
import io
import os
import re
import tempfile

import aiohttp
from PIL import (
    Image as PILImage,
    ImageSequence,
    ImageFilter,
    ImageOps,
    ImageEnhance,
)

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.event_message_type import EventMessageType

import astrbot.api.message_components as Comp

# 尝试导入 imageio（视频转GIF 功能依赖）
try:
    import imageio
except ImportError:
    imageio = None

from .services.config_service import ConfigService
from .core.image_handler import ImageHandler
from .utils.message_utils import MessageUtils


class ImgToolboxPlugin(Star):
    """图片工具箱：GIF 动图处理 + 图像对称镜像"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.cfg = config if config is not None else {}

        if imageio is None:
            logger.warning(
                "插件[图片工具箱]检测到缺少 imageio 库。请运行 pip install imageio[ffmpeg] 以启用 视频转gif 功能。"
            )

        # --- 图像对称模块 ---
        self.config_service = ConfigService(self)
        self.image_handler = ImageHandler(self.config_service, context=context)
        self._initialized = False
        self._init_task = None
        self._init_lock = asyncio.Lock()  # 防止初始化竞态条件

        logger.info("图片工具箱插件已加载")
        logger.info(f"当前配置: {self.config_service.get_config_summary()}")

    # ============================================================
    # 一、平台检测 & 统一回复（QQ Official 绕过 ResultDecorateStage）
    # ============================================================

    def _is_qqofficial(self, event: AstrMessageEvent) -> bool:
        """检测当前消息是否来自 QQ Official 平台"""
        try:
            name = event.get_platform_name()
            return name in ("qq_official_full", "qq_official_full_webhook")
        except Exception:
            return False

    async def _emit_text(self, event: AstrMessageEvent, text: str, stop: bool = False) -> bool:
        """
        发送纯文本回复。
        - QQ Official 平台：使用 event.send() 直接发送，绕过 ResultDecorateStage（避免无效 At 组件插入）
        - 其他平台：返回 False，调用方通过 yield event.plain_result() 发送
        返回 True 表示已通过 event.send() 直接发送，调用方不应再 yield。
        """
        if stop:
            event.stop_event()
        if self._is_qqofficial(event):
            chain = MessageChain()
            chain.chain = [Comp.Plain(text)]
            await event.send(chain)
            return True
        return False

    async def _emit_chain(self, event: AstrMessageEvent, components: list, stop: bool = False) -> bool:
        """
        发送组合消息回复（文本 + 图片等）。
        - QQ Official 平台：使用 event.send() 直接发送，绕过 ResultDecorateStage
        - 其他平台：返回 False，调用方通过 yield event.chain_result() 发送
        返回 True 表示已通过 event.send() 直接发送，调用方不应再 yield。
        """
        if stop:
            event.stop_event()
        if self._is_qqofficial(event):
            chain = MessageChain()
            chain.chain = components
            await event.send(chain)
            return True
        return False

    # ============================================================
    # 二、GIF 核心工具
    # ============================================================

    def _save_animation(self, output: io.BytesIO, frames: list, duration_ms: int, loop: int = 0):
        fmt = self.cfg.get('output_format', 'GIF').upper()
        if fmt == 'GIF':
            frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:], duration=duration_ms,
                           loop=loop, optimize=True, disposal=2)
        elif fmt == 'APNG':
            frames[0].save(output, format='PNG', save_all=True, append_images=frames[1:], duration=duration_ms,
                           loop=loop, optimize=True, default_image=True)
        elif fmt == 'WEBP':
            frames[0].save(output, format='WEBP', save_all=True, append_images=frames[1:], duration=duration_ms,
                           loop=loop, method=3, quality=80)
        else:
            frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:], duration=duration_ms,
                           loop=loop, optimize=True, disposal=2)

    # --- 辅助方法: 获取单张图片URL (增强版) ---
    def _get_image_url(self, event: AstrMessageEvent) -> str:
        """获取目标图片URL：优先回复的图片 -> 当前消息的图片 -> At对象的头像"""

        # 1. 检查回复链
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Reply) and seg.chain:
                    for item in seg.chain:
                        if isinstance(item, Comp.Image) and item.url:
                            return item.url
                        if isinstance(item, dict) and item.get('type') == 'image':
                            return item.get('data', {}).get('url') or item.get('url') or item.get('file')

        # 2. 检查当前消息中的图片
        if hasattr(event, "get_images"):
            images = event.get_images()
            if images:
                return images[0].url

        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Image) and seg.url:
                    return seg.url
                if isinstance(seg, dict) and seg.get('type') == 'image':
                    return seg.get('data', {}).get('url') or seg.get('url') or seg.get('file')

        # 3. 检查 At (获取头像)
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.At):
                    user_id = str(seg.qq)
                    return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

        return None

    # --- 递归提取所有图片 (支持合并转发、回复等) ---
    def _extract_images_from_chain(self, chain: list) -> list[str]:
        urls = []
        for item in chain:
            if isinstance(item, Comp.Image) and item.url:
                urls.append(item.url)
            elif isinstance(item, dict):
                if item.get('type') == 'image':
                    url = item.get('data', {}).get('url') or item.get('url') or item.get('file')
                    if url and isinstance(url, str) and url.startswith('http'):
                        urls.append(url)
                elif item.get('type') == 'node':
                    content = item.get('data', {}).get('content') or item.get('content')
                    if isinstance(content, list):
                        urls.extend(self._extract_images_from_chain(content))
            elif isinstance(item, Comp.Reply) and item.chain:
                urls.extend(self._extract_images_from_chain(item.chain))
            elif isinstance(item, Comp.Nodes):
                if item.nodes:
                    for node in item.nodes:
                        if isinstance(node.content, list):
                            urls.extend(self._extract_images_from_chain(node.content))
        return urls

    async def _get_all_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """获取上下文中所有的图片链接（包括当前消息、回复的消息、转发消息、At头像）"""
        urls = []

        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            urls.extend(self._extract_images_from_chain(event.message_obj.message))

        if hasattr(event, "get_images"):
            imgs = event.get_images()
            for img in imgs:
                if img.url and img.url not in urls:
                    urls.append(img.url)

        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.At):
                    uid = str(seg.qq)
                    url = f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
                    if url not in urls:
                        urls.append(url)

        seen = set()
        unique_urls = []
        for u in urls:
            if u not in seen:
                unique_urls.append(u)
                seen.add(u)
        return unique_urls

    # --- 辅助方法: 智能获取视频源 ---
    def _get_video_source(self, event: AstrMessageEvent) -> str:
        candidates = []

        def extract_from_item(item):
            url = getattr(item, 'url', None)
            if not url and isinstance(item, dict):
                url = item.get('data', {}).get('url') or item.get('url')
            if url and isinstance(url, str) and url.startswith('http'):
                return 100, url
            path = getattr(item, 'path', None)
            if not path and isinstance(item, dict):
                path = item.get('data', {}).get('path') or item.get('path')
            if path and isinstance(path, str) and os.path.isabs(path) and os.path.exists(path):
                return 90, path
            file_info = getattr(item, 'file', None)
            if not file_info and isinstance(item, dict):
                file_info = item.get('data', {}).get('file') or item.get('file')
            if file_info and isinstance(file_info, str):
                return 50, file_info
            return 0, None

        items_to_check = []
        if hasattr(event, "get_videos"):
            videos = event.get_videos()
            if videos:
                items_to_check.extend(videos)

        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Reply) and seg.chain:
                    items_to_check.extend(seg.chain)
                elif isinstance(seg, (Comp.Video, dict)):
                    items_to_check.append(seg)
                elif isinstance(seg, dict) and seg.get('type') == 'video':
                    items_to_check.append(seg)

        for item in items_to_check:
            score, val = extract_from_item(item)
            if val:
                candidates.append((score, val))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # --- 通过API解析文件ID ---
    async def _resolve_file_via_api(self, event: AstrMessageEvent, file_id: str) -> str:
        try:
            logger.info(f"尝试通过API解析文件ID: {file_id}")
            res = await event.bot.api.call_action("get_file", file_id=file_id)
            if not res or not isinstance(res, dict):
                return None
            url = res.get('url')
            if url and url.startswith('http'):
                return url
            path = res.get('file')
            if path and os.path.exists(path):
                return path
            return url or path
        except Exception as e:
            logger.warning(f"API解析文件失败: {e}")
            return None

    # --- 智能参数解析 (视频转gif) ---
    # 语法: -参数 值 / -参数=值 / -参数值 (兼容无间隔)
    # 支持: -start/-s 开始秒, -end/-e 结束秒, -dur/-d/-len/-time 时长秒,
    #       -fps 帧率, -step/-st 每n帧取1帧, -scale/-sc 缩放(0.1-1.0)
    def _parse_video_args(self, text: str):
        default_scale = self.cfg.get('default_scale', 0.3)
        default_fps = self.cfg.get('default_fps', 10)
        params = {
            'start': 0.0, 'end': None, 'fps': default_fps,
            'step': 1, 'scale': default_scale, 'force_step': False
        }

        def grab(aliases: str):
            """尝试三种写法: -key=val / -key val / -keyval, 返回数值或 None"""
            pat = r'-(' + aliases + r')\s*=\s*(\d+(?:\.\d+)?)'
            m = re.search(pat, text)
            if not m:
                m = re.search(r'-(' + aliases + r')\s+(\d+(?:\.\d+)?)', text)
            if not m:
                m = re.search(r'-(' + aliases + r')(\d+(?:\.\d+)?)', text)
            if not m:
                return None
            return float(m.group(2))

        start = grab(r's(?:tart)?')
        if start is not None:
            params['start'] = start

        end = grab(r'e(?:nd)?')
        if end is not None:
            params['end'] = end

        dur = grab(r'd(?:ur)?|len|time')
        if dur is not None:
            params['end'] = params['start'] + dur

        step = grab(r'st(?:ep)?')
        if step is not None and step > 0:
            params['step'] = int(step)
            params['fps'] = None
            params['force_step'] = True

        fps = grab(r'fps')
        if fps is not None and not params['force_step']:
            params['fps'] = int(fps)

        scale = grab(r'sc(?:ale)?')
        if scale is not None:
            params['scale'] = scale
        if params['scale'] < 0.1:
            params['scale'] = 0.1
        if params['scale'] > 1.0:
            params['scale'] = 1.0
        return params

    # --- 核心处理逻辑: 视频帧抽帧生成动画 ---
    def _process_gif_core(self, video_path: str, params: dict, max_colors: int = 256):
        try:
            reader = imageio.get_reader(video_path, format='FFMPEG')
            meta = reader.get_meta_data()
            video_duration = meta.get('duration', 100)
            src_fps = meta.get('fps', 30) or 30
            start_t = params['start']
            end_t = params['end'] if params['end'] is not None else video_duration
            max_dur_conf = self.cfg.get('max_gif_duration', 10.0)
            warn_msg = ""
            if (end_t - start_t) > max_dur_conf:
                end_t = start_t + max_dur_conf
                warn_msg = f"(限时{max_dur_conf}s)"
            end_t = min(end_t, video_duration)
            if start_t >= video_duration:
                return None, "❌ 开始时间超限", 0

            step = 1
            target_fps = 0
            if params.get('force_step'):
                step = params['step']
                target_fps = src_fps / step
            elif params.get('fps'):
                target_fps = params['fps']
                if target_fps > src_fps:
                    target_fps = src_fps
                step = max(1, int(src_fps / target_fps))
            else:
                step = 3
                target_fps = src_fps / step

            frames = []
            output_fmt = self.cfg.get('output_format', 'GIF').upper()
            for i, frame in enumerate(reader):
                current_time = i / src_fps
                if current_time < start_t:
                    continue
                if current_time > end_t:
                    break
                if i % step == 0:
                    pil_img = PILImage.fromarray(frame)
                    w, h = pil_img.size
                    new_w = int(w * params['scale'])
                    new_h = int(h * params['scale'])
                    pil_img = pil_img.resize((new_w, new_h), PILImage.Resampling.BILINEAR)
                    if output_fmt == 'GIF' and max_colors < 256:
                        pil_img = pil_img.quantize(colors=max_colors, method=1, dither=PILImage.Dither.FLOYDSTEINBERG)
                    frames.append(pil_img)
                if len(frames) > 400:
                    warn_msg += " [帧数截断]"
                    break
            reader.close()
            if not frames:
                return None, "❌ 无有效帧", 0
            output = io.BytesIO()
            duration_ms = int(1000 / target_fps) if target_fps > 0 else 100
            self._save_animation(output, frames, duration_ms, loop=0)
            output.seek(0)
            size_mb = output.getbuffer().nbytes / 1024 / 1024
            info = f"时间:{start_t}-{end_t:.1f}s {warn_msg}\n格式:{output_fmt} | FPS:{target_fps:.1f}\n缩放:{params['scale']} | 体积:{size_mb:.2f}MB"
            return output, info, size_mb
        except Exception as e:
            return None, f"内部错误: {repr(e)}", 0

    def _worker_video_to_gif_wrapper(self, video_path: str, params: dict):
        if imageio is None:
            return "❌ 缺少依赖库 imageio", None
        max_colors = self.cfg.get('gif_max_colors', 256)
        gif_io, msg, size_mb = self._process_gif_core(video_path, params, max_colors)
        if not gif_io:
            return msg, None
        output_fmt = self.cfg.get('output_format', 'GIF').upper()
        if size_mb > 10.0 and output_fmt == 'GIF':
            new_params = params.copy()
            new_msg_prefix = f"⚠️ 初次体积{size_mb:.1f}MB过大，自动压缩中...\n"
            new_colors = 128 if max_colors > 128 else 64
            new_params['scale'] = round(params['scale'] * 0.8, 2)
            if new_params['scale'] < 0.1:
                new_params['scale'] = 0.1
            retry_io, retry_msg, retry_size = self._process_gif_core(video_path, new_params, new_colors)
            if retry_io and retry_size < size_mb:
                return new_msg_prefix + retry_msg, retry_io
            else:
                return f"⚠️ 压缩失败({retry_size:.1f}MB)，原版:\n" + msg, gif_io
        return "✅ 转换成功\n" + msg, gif_io

    async def _read_local_file(self, path: str) -> bytes:
        """异步读取本地文件（兼容 v4.26.2 PreProcessStage 把 url 替换成本地路径的情况）。"""
        try:
            return await asyncio.to_thread(lambda: open(path, 'rb').read())
        except Exception as e:
            logger.error(f"读取本地文件失败: {path} -> {type(e).__name__}: {e}")
            return None

    async def _download_content(self, url: str) -> bytes:
        if not url.startswith("http"):
            return await self._read_local_file(url)
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=60) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.read()
            except Exception as e:
                logger.error(f"_download_content 下载失败: {url} -> {type(e).__name__}: {e}")
                return None

    def _worker_local_line_art(self, img_bytes: bytes) -> bytes:
        """本地线稿生成算法"""
        try:
            img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
            gray = img.convert("L")
            edges = gray.filter(ImageFilter.FIND_EDGES)
            result = ImageOps.invert(edges)
            enhancer = ImageEnhance.Contrast(result)
            result = enhancer.enhance(3.0)
            output = io.BytesIO()
            result.save(output, format='JPEG', quality=90)
            return output.getvalue()
        except Exception:
            return None

    # --- 本地图片转线稿 (无需API) ---
    @filter.command("图片转线稿")
    async def img_to_line_art(self, event: AstrMessageEvent):
        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 请发送图片或回复图片", stop=True):
                yield event.plain_result("❌ 请发送图片或回复图片")
            return

        if not await self._emit_text(event, "⏳ 正在处理(本地模式)..."):
            yield event.plain_result("⏳ 正在处理(本地模式)...")

        img_bytes = await self._download_content(img_url)
        if not img_bytes:
            if not await self._emit_text(event, "❌ 图片下载失败 (Bot无法访问该图片链接)", stop=True):
                yield event.plain_result("❌ 图片下载失败 (Bot无法访问该图片链接)")
            return

        result_bytes = await asyncio.to_thread(self._worker_local_line_art, img_bytes)

        if result_bytes:
            if not await self._emit_chain(event, [
                Comp.Plain("✅ 转换成功"),
                Comp.Image.fromBytes(result_bytes)
            ], stop=True):
                yield event.chain_result([
                    Comp.Plain("✅ 转换成功"),
                    Comp.Image.fromBytes(result_bytes)
                ])
        else:
            if not await self._emit_text(event, "❌ 转换处理失败 (图片格式错误?)", stop=True):
                yield event.plain_result("❌ 转换处理失败 (图片格式错误?)")

    # --- 视频转GIF ---
    @filter.command("视频转gif")
    async def video_to_gif_cmd(self, event: AstrMessageEvent):
        if imageio is None:
            if not await self._emit_text(event, "❌ 无法使用此功能：服务器缺少 imageio 库。", stop=True):
                yield event.plain_result("❌ 无法使用此功能：服务器缺少 imageio 库。")
            return
        msg_text = event.message_str.replace("视频转gif", "")
        params = self._parse_video_args(msg_text)
        raw_source = self._get_video_source(event)
        if not raw_source:
            if not await self._emit_text(event, "❌ 请回复一个视频或发送视频链接。", stop=True):
                yield event.plain_result("❌ 请回复一个视频或发送视频链接。")
            return
        valid_source = None
        if raw_source.startswith("http") or os.path.exists(raw_source):
            valid_source = raw_source
        else:
            if not await self._emit_text(event, "⏳ 正在请求视频地址..."):
                yield event.plain_result("⏳ 正在请求视频地址...")
            valid_source = await self._resolve_file_via_api(event, raw_source)
            if not valid_source:
                if not await self._emit_text(event, f"❌ 无法解析视频地址: {raw_source}", stop=True):
                    yield event.plain_result(f"❌ 无法解析视频地址: {raw_source}")
                return
        fmt = self.cfg.get('output_format', 'GIF')
        time_info = f"{params['start']}s-" + (f"{params['end']}s" if params['end'] else "末尾")
        if not await self._emit_text(event, f"⏳ 任务已接收 ({fmt})\n区间: {time_info}\n缩放: {params['scale']}"):
            yield event.plain_result(f"⏳ 任务已接收 ({fmt})\n区间: {time_info}\n缩放: {params['scale']}")
        tmp_path = ""
        is_temp_file = False
        try:
            if valid_source.startswith("http"):
                max_size = self.cfg.get('max_video_size_mb', 50.0) * 1024 * 1024
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
                    tmp_path = tmp_file.name
                    is_temp_file = True
                headers = {"User-Agent": "Mozilla/5.0"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(valid_source, headers=headers, timeout=120) as resp:
                        if resp.status != 200:
                            if not await self._emit_text(event, f"❌ 下载失败 HTTP {resp.status}", stop=True):
                                yield event.plain_result(f"❌ 下载失败 HTTP {resp.status}")
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                            return
                        content_len = resp.headers.get('Content-Length')
                        if content_len and int(content_len) > max_size:
                            if not await self._emit_text(event, "❌ 视频超过大小限制", stop=True):
                                yield event.plain_result("❌ 视频超过大小限制")
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                            return
                        with open(tmp_path, 'wb') as f:
                            f.write(await resp.read())
            else:
                tmp_path = valid_source
                is_temp_file = False
            result_msg, gif_bytes = await asyncio.to_thread(self._worker_video_to_gif_wrapper, tmp_path, params)
            if is_temp_file and os.path.exists(tmp_path):
                os.remove(tmp_path)
            if gif_bytes:
                if not await self._emit_chain(event, [Comp.Plain(result_msg), Comp.Image.fromBytes(gif_bytes.getvalue())], stop=True):
                    yield event.chain_result([Comp.Plain(result_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
            else:
                if not await self._emit_text(event, result_msg, stop=True):
                    yield event.plain_result(result_msg)
        except Exception as e:
            if is_temp_file and tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
            if not await self._emit_text(event, f"❌ 处理异常: {repr(e)}", stop=True):
                yield event.plain_result(f"❌ 处理异常: {repr(e)}")

    # --- 边距解析 ---
    def _parse_margins(self, text: str):
        margins = {'top': 0, 'bottom': 0, 'left': 0, 'right': 0}
        pattern = r'边距\s*([上下左右])?边?\s*(\d+)'
        matches = re.findall(pattern, text)
        for direction, amount_str in matches:
            try:
                amount = int(amount_str)
                if not direction:
                    for k in margins:
                        margins[k] += amount
                elif direction == '上':
                    margins['top'] += amount
                elif direction == '下':
                    margins['bottom'] += amount
                elif direction == '左':
                    margins['left'] += amount
                elif direction == '右':
                    margins['right'] += amount
            except ValueError:
                pass
        clean_text = re.sub(pattern, " ", text)
        return clean_text, margins

    def _crop_image_data(self, img_data: bytes, margins: dict) -> tuple[bytes, str]:
        if all(v == 0 for v in margins.values()):
            return img_data, ""
        try:
            img = PILImage.open(io.BytesIO(img_data)).convert("RGBA")
            w, h = img.size
            left, top, right, bottom = margins['left'], margins['top'], w - margins['right'], h - margins['bottom']
            if left >= right or top >= bottom:
                return img_data, f"\n⚠️ 边距无效: {w}x{h} -> {left},{top},{right},{bottom}"
            output = io.BytesIO()
            img.crop((left, top, right, bottom)).save(output, format='PNG')
            return output.getvalue(), f"\n✂️ 已裁边距: 上{margins['top']} 下{margins['bottom']} 左{margins['left']} 右{margins['right']}"
        except Exception as e:
            return img_data, f"\n⚠️ 边距裁剪出错: {e}"

    async def _download_image(self, url: str) -> bytes:
        """下载图片/动图。支持 HTTP URL 和本地文件路径。"""
        if not url.startswith("http"):
            return await self._read_local_file(url)
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=30) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.read()
            except Exception as e:
                logger.error(f"_download_image 下载失败: {url} -> {type(e).__name__}: {e}")
                return None

    # --- 精灵图合成GIF (共用入口) ---
    async def _handle_gif_task(self, event: AstrMessageEvent, algorithm_mode: int):
        msg_text = event.message_str
        clean_text, margins = self._parse_margins(msg_text)
        clean_text = clean_text.replace("合成1gif", "").replace("合成2gif", "").replace("合成gif", "")
        rows, cols, duration = 6, 6, 0.1
        grid_match = re.search(r'(\d+)\s*[*x×]\s*(\d+)', clean_text)
        if grid_match:
            rows, cols = int(grid_match.group(1)), int(grid_match.group(2))
            clean_text = clean_text.replace(grid_match.group(0), " ")
        dur_match = re.search(r'(\d+(?:\.\d+)?)', clean_text)
        if dur_match:
            try:
                val = float(dur_match.group(1))
                if 0 < val <= 60:
                    duration = val
            except Exception:
                pass
        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 未检测到图片", stop=True):
                yield event.plain_result("❌ 未检测到图片")
            return
        if not await self._emit_text(event, f"⏳ 正在合成(算法{algorithm_mode})... ({rows}x{cols}, 每帧{duration}s)"):
            yield event.plain_result(f"⏳ 正在合成(算法{algorithm_mode})... ({rows}x{cols}, 每帧{duration}s)")
        img_data = await self._download_image(img_url)
        if not img_data:
            if not await self._emit_text(event, "❌ 图片下载失败", stop=True):
                yield event.plain_result("❌ 图片下载失败")
            return
        img_data, crop_msg = await asyncio.to_thread(self._crop_image_data, img_data, margins)
        func = self.process_mode_1 if algorithm_mode == 1 else self.process_mode_2
        res_msg, gif_bytes = await asyncio.to_thread(func, img_data, rows, cols, duration)
        if gif_bytes:
            if not await self._emit_chain(event, [Comp.Plain(res_msg + crop_msg), Comp.Image.fromBytes(gif_bytes.getvalue())], stop=True):
                yield event.chain_result([Comp.Plain(res_msg + crop_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            if not await self._emit_text(event, f"❌ 失败：\n{res_msg}", stop=True):
                yield event.plain_result(f"❌ 失败：\n{res_msg}")

    @filter.command("合成1gif")
    async def make_gif_v1(self, event: AstrMessageEvent):
        async for res in self._handle_gif_task(event, 1):
            yield res

    @filter.command("合成2gif")
    async def make_gif_v2(self, event: AstrMessageEvent):
        async for res in self._handle_gif_task(event, 2):
            yield res

    def process_mode_1(self, img_data: bytes, rows: int, cols: int, duration_sec: float):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if getattr(img, "is_animated", False):
                img.seek(0)
            img = img.convert("RGBA")
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 2 or ch < 2:
                return f"⚠️ 单格太小 ({cw}x{ch})", None
            frames = []
            for r in range(rows):
                for c in range(cols):
                    frames.append(img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)))
            output = io.BytesIO()
            self._save_animation(output, frames, int(duration_sec * 1000), loop=0)
            output.seek(0)
            return f"✅ 合成成功\n算法1 | {w}x{h} | {rows}行{cols}列", output
        except Exception as e:
            return f"逻辑异常: {e}", None

    def process_mode_2(self, img_data: bytes, rows: int, cols: int, duration_sec: float):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if getattr(img, "is_animated", False):
                img.seek(0)
            img = img.convert("RGBA")
            datas = img.getdata()
            new_data = [(0, 0, 0, 0) if item[3] < 128 else (item[0], item[1], item[2], 255) for item in datas]
            img.putdata(new_data)
            has_trans = any(d[3] == 0 for d in new_data)
            master_pal = img.convert("RGB").quantize(colors=255 if has_trans else 256, method=1)
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 2 or ch < 2:
                return f"⚠️ 单格太小 ({cw}x{ch})", None
            frames = []
            for r in range(rows):
                for c in range(cols):
                    crop = img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
                    frame = crop.convert("RGB").quantize(palette=master_pal)
                    if has_trans:
                        mask = crop.split()[3].point(lambda a: 255 if a < 128 else 0)
                        frame.paste(255, mask=mask)
                    frames.append(frame)
            output = io.BytesIO()
            fmt = self.cfg.get('output_format', 'GIF').upper()
            if fmt == 'GIF':
                frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                               duration=int(duration_sec * 1000), loop=0, disposal=2,
                               transparency=255 if has_trans else None, optimize=True)
            else:
                self._save_animation(output, frames, int(duration_sec * 1000), loop=0)
            output.seek(0)
            return f"✅ 合成成功\n算法2 | {w}x{h} | {rows}行{cols}列", output
        except Exception as e:
            return f"逻辑异常: {e}", None

    # --- 统一变速处理逻辑 (v2: 支持倍速/帧率两种模式, fps>50自动抽帧) ---
    @filter.command("gif变速")
    async def gif_speed_change(self, event: AstrMessageEvent):
        """GIF 变速: /gif变速 2x (倍速) 或 /gif变速 30fps (帧率)"""
        msg = event.message_str.replace("gif变速", "", 1).strip()

        is_fps_mode = False
        value = 2.0

        fps_match = re.search(r'(\d+\.?\d*)\s*(?:fps|FPS|帧)', msg)
        mult_match = re.search(r'(\d+\.?\d*)\s*[xX×]', msg)
        num_match = re.search(r'(\d+\.?\d*)', msg)

        if fps_match:
            is_fps_mode = True
            value = float(fps_match.group(1))
        elif mult_match:
            value = float(mult_match.group(1))
        elif num_match:
            value = float(num_match.group(1))

        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 请回复一个GIF或发送GIF图片\n用法: /gif变速 2x (倍速) 或 /gif变速 30fps (帧率)", stop=True):
                yield event.plain_result("❌ 请回复一个GIF或发送GIF图片\n用法: /gif变速 2x (倍速) 或 /gif变速 30fps (帧率)")
            return

        if is_fps_mode:
            if not await self._emit_text(event, f"⏳ 正在变速到 {value:.0f}fps..."):
                yield event.plain_result(f"⏳ 正在变速到 {value:.0f}fps...")
        else:
            action = "加速" if value > 1 else ("减速" if value < 1 else "不变")
            if not await self._emit_text(event, f"⏳ 正在{action} {value}倍..."):
                yield event.plain_result(f"⏳ 正在{action} {value}倍...")

        img_data = await self._download_image(img_url)
        if not img_data:
            if not await self._emit_text(event, "❌ 下载失败", stop=True):
                yield event.plain_result("❌ 下载失败")
            return

        res_msg, gif_bytes = await asyncio.to_thread(
            self.process_speed_v2, img_data, value, is_fps_mode
        )

        if gif_bytes:
            if not await self._emit_chain(event, [Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())], stop=True):
                yield event.chain_result([Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            if not await self._emit_text(event, f"❌ 失败：{res_msg}", stop=True):
                yield event.plain_result(f"❌ 失败：{res_msg}")

    # 保留旧指令作为别名，内部走统一逻辑
    @filter.command("加速")
    @filter.regex(r"(?:gif)?(加速|变快)\s*[*x×]?\s*(\d+\.?\d*)?")
    async def accelerate_gif(self, event: AstrMessageEvent):
        """GIF加速 (旧指令, 等效于 /gif变速 Nx)"""
        msg = event.message_str
        factor = 2.0
        num_match = re.search(r"(\d+\.?\d*)", msg)
        if num_match:
            factor = float(num_match.group(1))

        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 请回复一个GIF或发送GIF图片", stop=True):
                yield event.plain_result("❌ 请回复一个GIF或发送GIF图片")
            return
        if not await self._emit_text(event, f"⏳ 正在加速 {factor}倍..."):
            yield event.plain_result(f"⏳ 正在加速 {factor}倍...")
        img_data = await self._download_image(img_url)
        if not img_data:
            if not await self._emit_text(event, "❌ 下载失败", stop=True):
                yield event.plain_result("❌ 下载失败")
            return
        res_msg, gif_bytes = await asyncio.to_thread(self.process_speed_v2, img_data, factor, False)
        if gif_bytes:
            if not await self._emit_chain(event, [Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())], stop=True):
                yield event.chain_result([Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            if not await self._emit_text(event, f"❌ 失败：{res_msg}", stop=True):
                yield event.plain_result(f"❌ 失败：{res_msg}")

    @filter.command("减速")
    @filter.regex(r"(?:gif)?(减速|变慢)\s*[*x×]?\s*(\d+\.?\d*)?")
    async def decelerate_gif(self, event: AstrMessageEvent):
        """GIF减速 (旧指令, 等效于 /gif变速 Nx)"""
        msg = event.message_str
        factor = 2.0
        num_match = re.search(r"(\d+\.?\d*)", msg)
        if num_match:
            factor = float(num_match.group(1))

        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 请回复一个GIF或发送GIF图片", stop=True):
                yield event.plain_result("❌ 请回复一个GIF或发送GIF图片")
            return
        if not await self._emit_text(event, f"⏳ 正在减速 {factor}倍..."):
            yield event.plain_result(f"⏳ 正在减速 {factor}倍...")
        img_data = await self._download_image(img_url)
        if not img_data:
            if not await self._emit_text(event, "❌ 下载失败", stop=True):
                yield event.plain_result("❌ 下载失败")
            return
        res_msg, gif_bytes = await asyncio.to_thread(
            self.process_speed_v2, img_data, 1.0 / factor, False
        )
        if gif_bytes:
            if not await self._emit_chain(event, [Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())], stop=True):
                yield event.chain_result([Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            if not await self._emit_text(event, f"❌ 失败：{res_msg}", stop=True):
                yield event.plain_result(f"❌ 失败：{res_msg}")

    def process_speed_v2(self, img_data: bytes, value: float, is_fps_mode: bool):
        """
        统一变速处理 (v2): 支持倍速模式和帧率模式。
        - 倍速模式: value = 速度倍数 (如 2.0 = 2倍速)
        - 帧率模式: value = 目标 fps (如 30 = 30fps)
        - 当目标 fps > 50 时自动抽帧实现
        """
        MAX_FPS = 50
        MIN_DURATION_MS = 20  # 1000ms / 50fps

        try:
            img = PILImage.open(io.BytesIO(img_data))
            if not getattr(img, "is_animated", False):
                return "这不是GIF动画", None

            frames = []
            orig_durations = []
            for frame in ImageSequence.Iterator(img):
                dur = frame.info.get('duration', 100)
                if dur <= 0:
                    dur = 100
                orig_durations.append(dur)
                frames.append(frame.copy())

            if not frames:
                return "无有效帧", None

            total_duration = sum(orig_durations)
            avg_duration = total_duration / len(frames)
            orig_fps = 1000.0 / avg_duration if avg_duration > 0 else 10.0

            if is_fps_mode:
                target_fps = value

                if target_fps > MAX_FPS:
                    keep_ratio = MAX_FPS / target_fps
                    frames_to_keep = max(2, int(len(frames) * keep_ratio))

                    new_frames = []
                    for i in range(frames_to_keep):
                        idx = int(i * len(frames) / frames_to_keep)
                        new_frames.append(frames[idx])

                    new_durations = [MIN_DURATION_MS] * len(new_frames)

                    output = io.BytesIO()
                    new_frames[0].save(
                        output, format='GIF', save_all=True,
                        append_images=new_frames[1:],
                        duration=new_durations, loop=0,
                        disposal=2, optimize=True
                    )
                    output.seek(0)

                    msg = (f"✅ 变速完成\n"
                           f"目标: {target_fps:.0f}fps | 原始: {orig_fps:.1f}fps\n"
                           f"💡 已自动抽帧实现 ({len(new_frames)}/{len(frames)}帧)")
                    return msg, output
                else:
                    target_duration = int(1000.0 / target_fps)
                    new_durations = [target_duration] * len(frames)

                    output = io.BytesIO()
                    frames[0].save(
                        output, format='GIF', save_all=True,
                        append_images=frames[1:],
                        duration=new_durations, loop=0,
                        disposal=2, optimize=True
                    )
                    output.seek(0)

                    msg = f"✅ 变速完成\n目标: {target_fps:.0f}fps | 原始: {orig_fps:.1f}fps"
                    return msg, output
            else:
                speed_factor = value
                ratio = 1.0 / speed_factor

                raw_durations = []
                for d in orig_durations:
                    raw_durations.append(int(d * ratio))

                MAX_RAW_DURATION = 1000
                raw_durations = [min(d, MAX_RAW_DURATION) for d in raw_durations]

                avg_raw_dur = sum(raw_durations) / len(raw_durations)

                if avg_raw_dur < MIN_DURATION_MS:
                    keep_ratio = avg_raw_dur / MIN_DURATION_MS
                    frames_to_keep = max(2, int(len(frames) * keep_ratio))

                    new_frames = []
                    for i in range(frames_to_keep):
                        idx = int(i * len(frames) / frames_to_keep)
                        new_frames.append(frames[idx])

                    new_durations = [MIN_DURATION_MS] * len(new_frames)

                    output = io.BytesIO()
                    new_frames[0].save(
                        output, format='GIF', save_all=True,
                        append_images=new_frames[1:],
                        duration=new_durations, loop=0,
                        disposal=2, optimize=True
                    )
                    output.seek(0)

                    effective_fps = 1000.0 / MIN_DURATION_MS
                    action = "加速" if speed_factor > 1 else ("减速" if speed_factor < 1 else "")
                    msg = (f"✅ 变速完成\n{speed_factor}x{action} | 原始 {orig_fps:.1f}fps → 等效 {effective_fps:.0f}fps\n"
                           f"💡 已自动抽帧实现 ({len(new_frames)}/{len(frames)}帧)")
                    return msg, output
                else:
                    new_durations = raw_durations

                    output = io.BytesIO()
                    frames[0].save(
                        output, format='GIF', save_all=True,
                        append_images=frames[1:],
                        duration=new_durations, loop=0,
                        disposal=2, optimize=True
                    )
                    output.seek(0)

                    effective_fps = 1000.0 / (sum(new_durations) / len(new_durations))
                    action = "加速" if speed_factor > 1 else ("减速" if speed_factor < 1 else "")
                    msg = f"✅ 变速完成\n{speed_factor}x{action} | 原始 {orig_fps:.1f}fps → 等效 {effective_fps:.1f}fps"
                    return msg, output

        except Exception as e:
            return f"异常: {e}", None

    def _worker_crop_grid(self, img_data: bytes, margins: dict, rows: int, cols: int):
        img_data, crop_msg = self._crop_image_data(img_data, margins)
        try:
            img = PILImage.open(io.BytesIO(img_data)).convert("RGBA")
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 1 or ch < 1:
                return f"❌ 图片太小 {crop_msg}", None
            res_list = []
            for r in range(rows):
                for c in range(cols):
                    out = io.BytesIO()
                    img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)).save(out, format='PNG')
                    res_list.append(out.getvalue())
            return crop_msg, res_list
        except Exception as e:
            return f"❌ 出错: {e}", None

    @filter.command("裁剪")
    async def crop_and_forward(self, event: AstrMessageEvent):
        clean, margins = self._parse_margins(event.message_str)
        match = re.search(r'(\d+)\s*[*x×]\s*(\d+)', clean)
        rows, cols = (int(match.group(1)), int(match.group(2))) if match else (1, 1)
        if rows > 20 or cols > 20:
            if not await self._emit_text(event, "⚠️ 行列数过大", stop=True):
                yield event.plain_result("⚠️ 行列数过大")
            return
        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 请发送图片", stop=True):
                yield event.plain_result("❌ 请发送图片")
            return
        if not await self._emit_text(event, "⏳ 处理中..."):
            yield event.plain_result("⏳ 处理中...")
        img_data = await self._download_image(img_url)
        if not img_data:
            if not await self._emit_text(event, "❌ 下载失败", stop=True):
                yield event.plain_result("❌ 下载失败")
            return
        msg, bytes_list = await asyncio.to_thread(self._worker_crop_grid, img_data, margins, rows, cols)
        if not bytes_list:
            if not await self._emit_text(event, msg, stop=True):
                yield event.plain_result(msg)
            return
        nodes = [Comp.Node(name="裁剪", content=[Comp.Plain(f"结果 {rows}x{cols}{msg}")])]
        for b in bytes_list:
            nodes.append(Comp.Node(name="裁剪", content=[Comp.Image.fromBytes(b)]))
        if not await self._emit_chain(event, [Comp.Nodes(nodes=nodes)], stop=True):
            yield event.chain_result([Comp.Nodes(nodes=nodes)])

    @filter.command("gif分解")
    async def decompose_gif(self, event: AstrMessageEvent):
        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 请发送GIF", stop=True):
                yield event.plain_result("❌ 请发送GIF")
            return
        if not await self._emit_text(event, "⏳ 分解中..."):
            yield event.plain_result("⏳ 分解中...")
        img_data = await self._download_image(img_url)
        frames = await asyncio.to_thread(self._worker_decompose, img_data)
        if isinstance(frames, str):
            if not await self._emit_text(event, frames, stop=True):
                yield event.plain_result(frames)
            return
        nodes = [Comp.Node(name="GIF助手", content=[Comp.Plain(f"第{i + 1}帧"), Comp.Image.fromBytes(b)]) for i, b in
                 enumerate(frames)]
        if not await self._emit_chain(event, [Comp.Nodes(nodes=nodes)], stop=True):
            yield event.chain_result([Comp.Nodes(nodes=nodes)])

    def _worker_decompose(self, img_data: bytes):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if not getattr(img, "is_animated", False):
                return "⚠️ 不是GIF动画"
            frames = []
            for i, frame in enumerate(ImageSequence.Iterator(img)):
                if i >= 100:
                    break
                out = io.BytesIO()
                frame.copy().convert("RGBA").save(out, format='PNG')
                frames.append(out.getvalue())
            return frames
        except Exception as e:
            return f"❌ 出错: {e}"

    # --- GIF倒放: 将动画帧顺序反转实现倒放播放 ---
    @filter.command("gif倒放")
    async def gif_reverse(self, event: AstrMessageEvent):
        async for res in self._handle_reverse_gif(event):
            yield res

    @filter.command("倒放")
    async def gif_reverse_alias(self, event: AstrMessageEvent):
        """倒放别名: 兼容不带gif前缀的指令"""
        async for res in self._handle_reverse_gif(event):
            yield res

    async def _handle_reverse_gif(self, event: AstrMessageEvent):
        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 请回复一个GIF或发送GIF图片\n用法: gif倒放", stop=True):
                yield event.plain_result("❌ 请回复一个GIF或发送GIF图片\n用法: gif倒放")
            return
        if not await self._emit_text(event, "⏳ 正在倒放..."):
            yield event.plain_result("⏳ 正在倒放...")
        img_data = await self._download_image(img_url)
        if not img_data:
            if not await self._emit_text(event, "❌ 下载失败", stop=True):
                yield event.plain_result("❌ 下载失败")
            return
        res_msg, gif_bytes = await asyncio.to_thread(self._worker_reverse_gif, img_data)
        if gif_bytes:
            if not await self._emit_chain(event, [Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())], stop=True):
                yield event.chain_result([Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            if not await self._emit_text(event, f"❌ 失败：{res_msg}", stop=True):
                yield event.plain_result(f"❌ 失败：{res_msg}")

    def _worker_reverse_gif(self, img_data: bytes):
        """将GIF动画帧顺序反转，实现倒放效果"""
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if not getattr(img, "is_animated", False):
                return "⚠️ 不是GIF动画", None

            frames = []
            durations = []
            for frame in ImageSequence.Iterator(img):
                dur = frame.info.get('duration', 100)
                if dur <= 0:
                    dur = 100
                durations.append(dur)
                frames.append(frame.copy().convert("RGBA"))

            if len(frames) < 2:
                return "⚠️ 帧数不足，无需倒放", None

            frames.reverse()
            durations.reverse()

            has_trans = any(f.getchannel("A").getextrema()[0] < 255 for f in frames)
            w, h = frames[0].size

            master = PILImage.new("RGB", (w * min(len(frames), 16), h), (255, 255, 255))
            for i, f in enumerate(frames[:16]):
                master.paste(f.convert("RGB"), (i * w, 0))
            master_pal = master.quantize(colors=255 if has_trans else 256, method=1)

            gif_frames = []
            for f in frames:
                pf = f.convert("RGB").quantize(palette=master_pal)
                if has_trans:
                    mask = f.getchannel("A").point(lambda a: 255 if a < 128 else 0)
                    pf.paste(255, mask=mask)
                gif_frames.append(pf)

            output = io.BytesIO()
            save_kwargs = dict(
                format='GIF', save_all=True,
                append_images=gif_frames[1:],
                duration=durations, loop=0,
                disposal=2,
                optimize=not has_trans,
            )
            if has_trans:
                save_kwargs['transparency'] = 255
                save_kwargs['background'] = 255
            gif_frames[0].save(output, **save_kwargs)
            output.seek(0)
            total_ms = sum(durations)
            return f"✅ 倒放完成 ({len(frames)}帧, 时长{total_ms / 1000:.2f}s)", output
        except Exception as e:
            return f"❌ 出错: {e}", None

    # --- 多图合成 GIF 核心处理逻辑 ---
    def _worker_multi_image_gif(self, images_bytes: list[bytes], duration_sec: float):
        try:
            pil_images = []
            max_w, max_h = 0, 0

            for b in images_bytes:
                try:
                    img = PILImage.open(io.BytesIO(b)).convert("RGBA")
                    if getattr(img, "is_animated", False):
                        img.seek(0)
                        img = img.copy()
                    pil_images.append(img)
                    max_w = max(max_w, img.width)
                    max_h = max(max_h, img.height)
                except Exception as e:
                    logger.warning(f"加载图片失败: {e}")

            if not pil_images:
                return "❌ 没有有效的图片", None

            frames = []
            for img in pil_images:
                bg = PILImage.new("RGBA", (max_w, max_h), (255, 255, 255, 0))

                src_ratio = img.width / img.height
                tgt_ratio = max_w / max_h

                if src_ratio > tgt_ratio:
                    new_w = max_w
                    new_h = int(max_w / src_ratio)
                else:
                    new_h = max_h
                    new_w = int(max_h * src_ratio)

                img_resized = img.resize((new_w, new_h), PILImage.Resampling.BILINEAR)

                paste_x = (max_w - new_w) // 2
                paste_y = (max_h - new_h) // 2
                bg.paste(img_resized, (paste_x, paste_y), mask=img_resized if 'A' in img_resized.getbands() else None)

                frames.append(bg)

            output = io.BytesIO()
            duration_ms = int(duration_sec * 1000)
            self._save_animation(output, frames, duration_ms, loop=0)
            output.seek(0)

            return f"✅ 合成成功 ({len(frames)}张)", output

        except Exception as e:
            return f"合成出错: {repr(e)}", None

    # --- 表情包做旧功能 (模拟早期互联网传播效果) ---
    def _worker_age_meme(self, img_data: bytes, times: int) -> tuple[str, bytes]:
        try:
            img = PILImage.open(io.BytesIO(img_data))

            is_animated = getattr(img, "is_animated", False)

            if is_animated:
                frames = []
                durations = []

                for frame in ImageSequence.Iterator(img):
                    dur = frame.info.get('duration', 100)
                    if dur <= 0:
                        dur = 100
                    durations.append(dur)
                    frame_copy = frame.copy().convert("RGB")
                    aged_frame = self._age_single_frame(frame_copy, times)
                    frames.append(aged_frame)

                if not frames:
                    return "❌ 无法读取动图帧", None

                gif_frames = []
                for f in frames:
                    p_frame = f.convert("P", palette=PILImage.Palette.ADAPTIVE, colors=256)
                    gif_frames.append(p_frame)

                output = io.BytesIO()
                gif_frames[0].save(
                    output,
                    format='GIF',
                    save_all=True,
                    append_images=gif_frames[1:],
                    duration=durations,
                    loop=0,
                    disposal=2,
                    optimize=False
                )
                output.seek(0)
                return f"✅ 做旧成功 (动图 {len(frames)}帧, {times}次传播)", output.getvalue()
            else:
                img = img.convert("RGB")
                aged_img = self._age_single_frame(img, times)

                output = io.BytesIO()
                final_quality = max(30, 70 - times * 3)
                aged_img.save(output, format='JPEG', quality=final_quality)
                return f"✅ 做旧成功 ({times}次传播, 质量{final_quality}%)", output.getvalue()

        except Exception as e:
            return f"❌ 处理失败: {repr(e)}", None

    def _age_single_frame(self, img: PILImage.Image, times: int) -> PILImage.Image:
        """对单帧图片进行做旧处理 - 渐进式做旧"""
        import random

        if img.mode != "RGB":
            img = img.convert("RGB")

        for i in range(times):
            if i % 3 == 0:
                r, g, b = img.split()

                green_boost = random.randint(1, 2)
                red_reduce = random.randint(0, 1)
                blue_reduce = random.randint(0, 1)

                def make_add_func(val):
                    return lambda x: min(255, x + val)

                def make_sub_func(val):
                    return lambda x: max(0, x - val)

                g = g.point(make_add_func(green_boost))
                if red_reduce > 0:
                    r = r.point(make_sub_func(red_reduce))
                if blue_reduce > 0:
                    b = b.point(make_sub_func(blue_reduce))

                img = PILImage.merge("RGB", (r, g, b))

            quality = max(25, 70 - i * 3)
            temp_io = io.BytesIO()
            img.save(temp_io, format='JPEG', quality=quality)
            temp_io.seek(0)
            img = PILImage.open(temp_io).convert("RGB")

            if i % 3 == 0:
                blur_radius = 0.2 + (i // 3) * 0.1
                img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))

            if i % 5 == 2:
                img = img.filter(ImageFilter.SHARPEN)

            if i % 2 == 0:
                enhancer = ImageEnhance.Color(img)
                saturation = max(0.85, 1.0 - 0.015)
                img = enhancer.enhance(saturation)

            if i % 2 == 1:
                enhancer = ImageEnhance.Contrast(img)
                contrast = max(0.85, 1.0 - 0.01)
                img = enhancer.enhance(contrast)

            if times >= 15 and i == times // 2:
                w, h = img.size
                if w > 50 and h > 50:
                    small = img.resize((int(w * 0.8), int(h * 0.8)), PILImage.Resampling.BILINEAR)
                    img = small.resize((w, h), PILImage.Resampling.BILINEAR)

        return img

    @filter.command("表情包做旧")
    @filter.regex(r"(?:表情包?)?做旧\s*(\d+)?")
    async def age_meme(self, event: AstrMessageEvent):
        """
        表情包做旧功能，模拟早期互联网图片传播效果
        用法：表情包做旧 [次数]
        示例：表情包做旧 10 (做旧10次，数字越大越绿越糊)
        建议：1-5次轻度做旧，5-10次中度做旧，10-20次重度做旧
        """
        msg_text = event.message_str

        times = 5  # 默认5次
        num_match = re.search(r'做旧\s*(\d+)', msg_text)
        if num_match:
            times = int(num_match.group(1))
        else:
            num_match = re.search(r'(\d+)', msg_text)
            if num_match:
                times = int(num_match.group(1))

        times = max(1, min(times, 50))  # 1-50次

        img_url = self._get_image_url(event)
        if not img_url:
            if not await self._emit_text(event, "❌ 请发送图片或回复图片\n用法: 表情包做旧 [次数]\n次数越大越绿越糊 (建议1-20)", stop=True):
                yield event.plain_result("❌ 请发送图片或回复图片\n用法: 表情包做旧 [次数]\n次数越大越绿越糊 (建议1-20)")
            return

        if times <= 5:
            level = "轻度做旧 (微微泛绿)"
        elif times <= 10:
            level = "中度做旧 (明显发绿变糊)"
        elif times <= 20:
            level = "重度做旧 (经典老图风格)"
        else:
            level = "极限做旧 (赛博遗产级别)"

        if not await self._emit_text(event, f"⏳ 正在做旧... ({times}次传播, {level})"):
            yield event.plain_result(f"⏳ 正在做旧... ({times}次传播, {level})")

        img_data = await self._download_image(img_url)
        if not img_data:
            if not await self._emit_text(event, "❌ 图片下载失败", stop=True):
                yield event.plain_result("❌ 图片下载失败")
            return

        res_msg, result_bytes = await asyncio.to_thread(
            self._worker_age_meme, img_data, times
        )

        if result_bytes:
            if not await self._emit_chain(event, [
                Comp.Plain(f"{res_msg}\n💡 {level}"),
                Comp.Image.fromBytes(result_bytes)
            ], stop=True):
                yield event.chain_result([
                    Comp.Plain(f"{res_msg}\n💡 {level}"),
                    Comp.Image.fromBytes(result_bytes)
                ])
        else:
            if not await self._emit_text(event, res_msg, stop=True):
                yield event.plain_result(res_msg)

    @filter.command("多图合成gif")
    async def multi_img_gif(self, event: AstrMessageEvent):
        """
        多图合成GIF，支持直接发送图片、回复含图消息、转发消息。
        用法：多图合成gif [速度/时长]
        示例：多图合成gif 0.5 (每帧0.5秒)
        """
        msg_text = event.message_str.replace("多图合成gif", "")
        duration = 0.5  # 默认0.5秒

        fps_match = re.search(r'(\d+)\s*(?:fps|帧)', msg_text, re.I)
        if fps_match:
            try:
                fps = float(fps_match.group(1))
                if fps > 0:
                    duration = 1.0 / fps
            except Exception:
                pass
        else:
            sec_match = re.search(r'(\d+(?:\.\d+)?)', msg_text)
            if sec_match:
                try:
                    val = float(sec_match.group(1))
                    if 0.01 <= val <= 60:
                        duration = val
                except Exception:
                    pass

        if not await self._emit_text(event, "⏳ 正在搜集图片资源..."):
            yield event.plain_result("⏳ 正在搜集图片资源...")

        img_urls = await self._get_all_image_urls(event)

        if not img_urls or len(img_urls) < 1:
            if not await self._emit_text(event, "❌ 未检测到足够的图片资源 (请回复图片消息，或发送包含图片的合并转发)", stop=True):
                yield event.plain_result("❌ 未检测到足够的图片资源 (请回复图片消息，或发送包含图片的合并转发)")
            return

        if not await self._emit_text(event, f"⏳ 正在下载 {len(img_urls)} 张图片并合成 (每帧{duration:.2f}s)..."):
            yield event.plain_result(f"⏳ 正在下载 {len(img_urls)} 张图片并合成 (每帧{duration:.2f}s)...")

        tasks = [self._download_content(url) for url in img_urls]
        results = await asyncio.gather(*tasks)
        valid_bytes = [b for b in results if b is not None]

        if len(valid_bytes) < 1:
            if not await self._emit_text(event, "❌ 图片下载失败", stop=True):
                yield event.plain_result("❌ 图片下载失败")
            return

        res_msg, gif_io = await asyncio.to_thread(self._worker_multi_image_gif, valid_bytes, duration)

        if gif_io:
            if not await self._emit_chain(event, [
                Comp.Plain(f"{res_msg}\n画布适应最大尺寸，自动居中填充"),
                Comp.Image.fromBytes(gif_io.getvalue())
            ], stop=True):
                yield event.chain_result([
                    Comp.Plain(f"{res_msg}\n画布适应最大尺寸，自动居中填充"),
                    Comp.Image.fromBytes(gif_io.getvalue())
                ])
        else:
            if not await self._emit_text(event, res_msg, stop=True):
                yield event.plain_result(res_msg)

    # ============================================================
    # 三、图像对称 (镜像) 模块
    # ============================================================

    async def _ensure_initialized(self):
        """确保插件已初始化（使用Lock防止竞态条件）"""
        async with self._init_lock:
            if self._initialized:
                return

            if self._init_task is not None and not self._init_task.done():
                await self._init_task
            elif self._init_task is None or self._init_task.done():
                self._init_task = asyncio.create_task(self._do_initialize())
                await self._init_task

            self._initialized = True

    async def _do_initialize(self):
        """实际执行初始化"""
        try:
            if hasattr(self, "image_handler") and self.image_handler:
                await self.image_handler.initialize()
            logger.info("图片工具箱插件初始化完成")
        except Exception as e:
            logger.error(f"插件初始化失败: {e}", exc_info=True)
            self._initialized = False  # 标记为未初始化，允许重试

    async def _send_or_return(self, event: AstrMessageEvent, result):
        """
        发送或返回结果消息。

        在 qqofficial 系列平台上，AstrBot 的「回复时 @ 发送人」功能会自动
        在结果链头插入 At 组件，但 qqofficial 适配器会忽略 At 组件，
        导致 @ 无法正常显示。因此对于 qqofficial 平台，通过 event.send()
        直接发送消息以绕过框架的 ResultDecorateStage。

        Args:
            event: 消息事件对象
            result: MessageEventResult

        Returns:
            MessageEventResult | None
        """
        if MessageUtils.is_qqofficial_platform(event):
            await event.send(result)
            return None
        return result

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_all_mirror_commands(self, event: AstrMessageEvent):
        """
        处理无斜杠的镜像指令
        格式: "指令名 @用户" (如: "左对称 @张三")
        """
        message_str = event.message_str.strip()

        # qqofficial 系列适配器中，普通用户 @ 不会生成 At 组件，
        # message_str 里仍保留 <@!{openid}> / <@{openid}> 原始标记
        # （仅 bot 自身的 @ 在适配器层被剥离），需要归一化为 "@" 才能复用
        # 下方基于 "@" 的指令解析逻辑。
        if MessageUtils.is_qqofficial_platform(event):
            message_str = re.sub(r"<@!?[A-Za-z0-9_\-]+>", "@", message_str).strip()

        plain_commands = {
            "/左对称": "left_to_right",
            "左对称": "left_to_right",
            "mirror left": "left_to_right",
            "/右对称": "right_to_left",
            "右对称": "right_to_left",
            "mirror right": "right_to_left",
            "/上对称": "top_to_bottom",
            "上对称": "top_to_bottom",
            "mirror top": "top_to_bottom",
            "/下对称": "bottom_to_top",
            "下对称": "bottom_to_top",
            "mirror bottom": "bottom_to_top",
            "/反色": "invert",
            "反色": "invert",
            "颜色反转": "invert",
            "invert": "invert",
            "mirror invert": "invert",
            "/对称帮助": "help",
            "对称帮助": "help",
            "/镜像帮助": "help",
            "镜像帮助": "help",
            "/图片帮助": "help",
            "图片帮助": "help",
            "/工具箱帮助": "help",
            "工具箱帮助": "help",
            "/图片工具箱": "help",
            "图片工具箱": "help",
        }

        actual_command = message_str
        if " @" in message_str:
            parts = message_str.split("@", 1)
            actual_command = parts[0].strip()
        elif message_str.startswith("@"):
            parts = message_str.split(None, 2)
            if len(parts) >= 2:
                actual_command = parts[1].strip()

        if actual_command in plain_commands:
            mode = plain_commands[actual_command]
            logger.info(f"收到无斜杠指令: {actual_command} -> 模式: {mode}")

            if mode == "help":
                async for result in self.mirror_help(event):
                    yield result
            else:
                async for result in self.handle_mirror_with_mode(event, mode):
                    yield result

    async def handle_mirror_with_mode(self, event: AstrMessageEvent, mode: str):
        """处理镜像请求的统一入口"""
        await self._ensure_initialized()

        if self.image_handler is None:
            logger.error("image_handler 未初始化")
            result = event.plain_result("❌ 插件尚未初始化完成，请稍后再试")
            wrapped = await self._send_or_return(event, result)
            if wrapped is not None:
                yield wrapped
            return

        async for result in self.image_handler.process_mirror(event, mode):
            if result is not None:
                yield result

    async def mirror_help(self, event: AstrMessageEvent):
        """显示图片工具箱帮助信息 (由 handle_all_mirror_commands 统一调度)"""
        await self._ensure_initialized()

        if self.config_service is None:
            logger.error("config_service 未初始化")
            result = event.plain_result("❌ 插件尚未初始化完成，请稍后再试")
            wrapped = await self._send_or_return(event, result)
            if wrapped is not None:
                yield wrapped
            return

        help_text = self.config_service.get_help_text()
        result = event.plain_result(help_text)
        wrapped = await self._send_or_return(event, result)
        if wrapped is not None:
            yield wrapped

    async def terminate(self):
        """插件卸载时调用"""
        termination_error = None

        try:
            if self._init_task is not None and not self._init_task.done():
                self._init_task.cancel()
                try:
                    await self._init_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    termination_error = f"取消初始化任务失败: {e}"

            if self.image_handler is not None:
                try:
                    await self.image_handler.cleanup()
                except AttributeError as e:
                    termination_error = f"image_handler 属性访问失败: {e}"
                except RuntimeError as e:
                    termination_error = f"image_handler 运行时错误: {e}"
                except Exception as e:
                    termination_error = f"image_handler 清理失败: {e}"
            else:
                logger.info("image_handler 未初始化，跳过清理操作")

        except asyncio.CancelledError:
            termination_error = "插件卸载被取消"
        except RuntimeError as e:
            termination_error = f"插件卸载运行时错误: {e}"
        except Exception as e:
            termination_error = f"插件卸载未知错误: {e}"
            logger.error(f"插件卸载时发生未预期异常: {e}", exc_info=True)
        finally:
            if termination_error:
                logger.warning(f"插件卸载完成（部分操作失败）: {termination_error}")
            else:
                logger.info("图片工具箱插件已成功卸载")