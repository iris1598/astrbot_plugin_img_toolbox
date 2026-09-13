"""
图像对称处理模块
"""

import asyncio
import os
from pathlib import Path
from typing import Tuple, Optional
from PIL import Image, ImageOps, ImageSequence, ImageFile

# 注意：PIL全局设置已移除，避免影响其他插件

# 统一使用相对导入
from .utils.file_utils import FileUtils
from .utils.gif_writer import (
    GIF_TRANSPARENT_INDEX,
    has_low_level_api,
    rgba_to_gif_frame,
    write_gif_streaming,
)
from .config import PluginConfig
from astrbot.api import logger


class MirrorProcessor:
    """图像对称处理器"""

    @staticmethod
    def _check_image_size(img: Image.Image) -> bool:
        """
        检查图像尺寸，防止解压炸弹 - 简化版

        Args:
            img: PIL图像对象

        Returns:
            bool: 图像尺寸是否在安全范围内
        """
        pixels = img.width * img.height

        # 1亿像素硬限制
        if pixels > 10000 * 10000:
            return False

        # 2500万像素警告
        if pixels > 5000 * 5000:
            logger.warning(f"处理大图像: {pixels}像素 ({img.width}x{img.height})")

        return True

    @staticmethod
    def _check_image_before_open(
        file_path: str,
        config: Optional[PluginConfig] = None,
    ) -> Tuple[bool, str]:
        """
        打开图像前进行安全检查（解压炸弹防护）

        检查项:
        - 文件大小限制（使用配置中的 precheck_file_size_mb）
        - 文件头校验
        - 文件是否为空

        Args:
            file_path: 文件路径
            config: 插件配置，用于读取文件大小限制

        Returns:
            Tuple[bool, str]: (是否安全, 错误信息)
        """
        try:
            max_size = config.precheck_file_size_bytes if config else 100 * 1024 * 1024

            file_size = os.path.getsize(file_path)
            if file_size > max_size:
                max_size_mb = max_size / 1024 / 1024
                return False, f"文件过大 ({file_size / 1024 / 1024:.1f}MB > {max_size_mb:.0f}MB)"

            with open(file_path, "rb") as f:
                header = f.read(100)
                if not header:
                    return False, "文件为空或无法读取"

            return True, ""
        except FileNotFoundError:
            return False, "文件不存在"
        except (PermissionError, OSError) as e:
            logger.error(f"图像预检查异常: {e}")
            return False, "文件检查失败"

    @staticmethod
    async def _check_image_before_open_async(
        file_path: str,
        config: Optional[PluginConfig] = None,
    ) -> Tuple[bool, str]:
        """
        异步版文件预检查（将I/O操作放入线程池执行）
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: MirrorProcessor._check_image_before_open(file_path, config)
        )

    @staticmethod
    async def process_image(
        input_path: str,
        output_path: str,
        mode: str,
        config: Optional[PluginConfig] = None,
    ) -> Tuple[bool, str]:
        """
        处理图像的主函数

        Args:
            input_path: 输入图像路径
            output_path: 输出图像路径
            mode: 对称模式
            config: 插件配置

        Returns:
            Tuple[bool, str]: (是否成功, 错误信息或成功信息)
        """
        try:
            # 验证文件存在
            if not Path(input_path).exists():
                return False, f"输入文件不存在: {input_path}"

            # 1. 解压炸弹预检查（文件大小和文件头）- 异步执行避免阻塞事件循环
            is_safe, msg = await MirrorProcessor._check_image_before_open_async(input_path, config)
            if not is_safe:
                return False, msg

            # 验证文件大小（使用配置）
            is_valid, error_msg = FileUtils.validate_image_size(input_path, config)
            if not is_valid:
                return False, error_msg

            # 获取文件扩展名
            ext = FileUtils.get_file_extension(input_path)
            if not ext:
                return False, "无法识别图像格式"

            # 检查GIF支持
            if ext == ".gif":
                if config and not config.enable_gif:
                    return False, "GIF处理功能已禁用"

            # 处理GIF
            if ext in FileUtils.SUPPORTED_GIF_FORMAT:
                return await MirrorProcessor._process_gif(
                    input_path, output_path, mode, config
                )
            # 处理静态图像
            elif ext in FileUtils.SUPPORTED_STATIC_FORMATS:
                return await MirrorProcessor._process_static_image(
                    input_path, output_path, mode, config
                )
            else:
                return False, f"不支持的图像格式: {ext}"

        except Exception as e:
            return False, f"图像处理失败: {str(e)}"

    @staticmethod
    async def _process_static_image(
        input_path: str,
        output_path: str,
        mode: str,
        config: Optional[PluginConfig] = None,
    ) -> Tuple[bool, str]:
        """
        处理静态图像（全异步，避免阻塞）
        """
        try:
            loop = asyncio.get_running_loop()

            # 将所有图像处理放入executor执行
            def process_in_thread():
                with Image.open(input_path) as img:
                    # 检查图像尺寸安全性（防止解压炸弹）
                    if not MirrorProcessor._check_image_size(img):
                        return (
                            None,
                            f"图像尺寸过大，可能存在安全风险: {img.width}x{img.height}像素",
                        )

                    # 转换模式（确保透明度处理正确）
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    elif img.mode == "LA":
                        img = img.convert("RGBA")

                    # 应用图像变换
                    mirrored = MirrorProcessor._apply_mirror(img, mode)

                    # 应用压缩（如果启用）
                    if config and config.enable_compression:
                        mirrored = MirrorProcessor._compress_image(mirrored, config)

                    # 保存图像（使用配置的质量设置）
                    quality = config.output_quality if config else 85

                    # 根据输出路径扩展名判断保存格式
                    output_ext = Path(output_path).suffix.lower()

                    # 确保图像模式兼容输出格式
                    mirrored = MirrorProcessor._ensure_compatible_image_mode(
                        mirrored, output_ext
                    )

                    # 保存图像
                    if output_ext == ".png":
                        mirrored.save(output_path, optimize=True)
                    elif output_ext == ".webp":
                        mirrored.save(output_path, quality=quality, method=6)
                    else:
                        mirrored.save(output_path, quality=quality, optimize=True)

                    return mirrored, quality

            result = await loop.run_in_executor(None, process_in_thread)

            if result[0] is None:
                return False, result[1]

            return True, "图像处理成功"

        except Exception as e:
            return False, f"静态图像处理失败: {str(e)}"

    @staticmethod
    def _convert_rgba_to_rgb_for_jpeg(image: Image.Image) -> Image.Image:
        """
        将RGBA图像转换为RGB格式（用于JPEG保存）

        Args:
            image: PIL图像对象

        Returns:
            转换后的RGB图像
        """
        if image.mode != "RGBA":
            return image

        try:
            alpha = image.getchannel("A")
            if alpha.getextrema() == (255, 255):
                return image.convert("RGB")

            background = Image.new("RGB", image.size, (255, 255, 255))
            mask = image.split()[3] if len(image.split()) > 3 else alpha
            background.paste(image, mask=mask)
            return background
        except (ValueError, KeyError, IndexError) as e:
            logger.debug(f"RGBA转RGB失败，使用简单转换: {e}")
            return image.convert("RGB")

    @staticmethod
    def _ensure_compatible_image_mode(
        image: Image.Image,
        output_ext: str,
    ) -> Image.Image:
        """
        确保图像模式兼容输出格式

        Args:
            image: PIL图像对象
            output_ext: 输出文件扩展名

        Returns:
            兼容的图像对象
        """
        if output_ext in [".jpg", ".jpeg"] and image.mode == "RGBA":
            return MirrorProcessor._convert_rgba_to_rgb_for_jpeg(image)

        if output_ext not in [".png"] and image.mode not in ("RGB", "L", "P"):
            try:
                return image.convert("RGB")
            except Exception as e:
                logger.warning(f"图像模式转换失败: {e}")
                return image

        return image

    @staticmethod
    def _compress_image(image: Image.Image, config: PluginConfig) -> Image.Image:
        """
        压缩图像

        Args:
            image: PIL图像对象
            config: 插件配置

        Returns:
            压缩后的图像
        """
        try:
            if image.mode == "RGBA":
                try:
                    alpha = image.getchannel("A")
                    if alpha.getextrema() == (255, 255):
                        image = image.convert("RGB")
                except (ValueError, KeyError) as e:
                    logger.debug(f"无法获取alpha通道或检查透明度: {e}")

            width, height = image.size
            max_dimension = config.max_compression_dimension

            if width > max_dimension or height > max_dimension:
                ratio = min(max_dimension / width, max_dimension / height)
                new_width = int(width * ratio)
                new_height = int(height * ratio)
                image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

            return image
        except (ValueError, OSError, MemoryError) as e:
            logger.warning(f"图像压缩失败，返回原图: {type(e).__name__}: {e}")
            return image

    @staticmethod
    async def _process_gif(
        input_path: str,
        output_path: str,
        mode: str,
        config: Optional[PluginConfig] = None,
    ) -> Tuple[bool, str]:
        """
        处理GIF动画（全异步，避免阻塞）
        """
        try:
            loop = asyncio.get_running_loop()

            # 将整个GIF处理放入executor执行
            def process_gif_in_thread():
                # 临时启用 LOAD_TRUNCATED_IMAGES 以处理带透明度的 GIF
                original_load_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
                ImageFile.LOAD_TRUNCATED_IMAGES = True

                try:
                    with Image.open(input_path) as img:
                        # 检查GIF整体尺寸安全性
                        if not MirrorProcessor._check_image_size(img):
                            return (
                                None,
                                f"GIF尺寸过大，可能存在安全风险: {img.width}x{img.height}像素",
                            )

                        MAX_FRAMES = config.max_gif_frames if config else 200
                        MAX_TOTAL_PIXELS = config.max_total_pixels if config else 4000 * 4000

                        # 根据配置的质量计算调色板颜色数 (quality 1-100 映射到 64-255 色)
                        quality = config.output_quality if config else 85
                        palette_colors = max(64, min(255, int(64 + (255 - 64) * quality / 100)))

                        def make_frames():
                            """逐帧镜像并产出 (RGBA 帧, 时长ms)，保持内存占用与帧数无关。"""
                            frame_count = 0
                            target_size = None
                            for frame in ImageSequence.Iterator(img):
                                frame_count += 1

                                if frame_count > MAX_FRAMES:
                                    logger.error(
                                        f"GIF帧数过多，可能存在解压炸弹风险: {frame_count}帧"
                                    )
                                    raise ValueError(
                                        f"GIF帧数过多（{frame_count} > {MAX_FRAMES}），可能存在安全风险"
                                    )

                                # 检查总像素数（帧数 × 单帧像素）
                                total_pixels = frame_count * frame.width * frame.height
                                if total_pixels > MAX_TOTAL_PIXELS:
                                    logger.error(
                                        f"GIF总像素数过多，可能存在解压炸弹风险: {total_pixels}像素"
                                    )
                                    raise ValueError(
                                        f"GIF总像素数过多（{total_pixels / 10000 / 10000:.1f}亿像素），可能存在安全风险"
                                    )

                                duration = frame.info.get("duration", 100)

                                # 复制帧并转换为 RGBA（确保帧数据独立，避免迭代器问题）
                                frame_copy = frame.copy()
                                if frame_copy.mode != "RGBA":
                                    frame_copy = frame_copy.convert("RGBA")

                                # 应用图像变换
                                mirrored_frame = MirrorProcessor._apply_mirror(frame_copy, mode)

                                # 应用压缩（如果启用）
                                if config and config.enable_compression:
                                    mirrored_frame = MirrorProcessor._compress_image(
                                        mirrored_frame, config
                                    )

                                # 统一所有帧的尺寸（以第一帧为基准）
                                if target_size is None:
                                    target_size = mirrored_frame.size
                                elif mirrored_frame.size != target_size:
                                    mirrored_frame = mirrored_frame.resize(
                                        target_size, Image.Resampling.LANCZOS
                                    )

                                yield mirrored_frame, int(duration)

                            if frame_count > 100:
                                logger.warning(
                                    f"处理大型GIF: {frame_count}帧，可能需要较长时间"
                                )

                        # 优先流式写出：逐帧写盘，内存占用与帧数无关
                        if has_low_level_api():
                            try:
                                with open(output_path, "wb") as fp:
                                    count = write_gif_streaming(
                                        fp,
                                        make_frames(),
                                        loop=0,
                                        palette_colors=palette_colors,
                                        reserve_transparency=True,
                                    )
                                if count > 0:
                                    return count, None
                                if count == 0:
                                    return None, "GIF没有帧数据"
                            except ValueError as ve:
                                # 安全校验未通过：清理半成品
                                try:
                                    os.remove(output_path)
                                except OSError:
                                    pass
                                logger.error(f"GIF处理中止: {ve}")
                                return None, str(ve)
                            except Exception as e:
                                logger.warning(
                                    f"流式写出 GIF 失败，回退标准写出: {type(e).__name__}: {e}",
                                    exc_info=True,
                                )

                        # 回退：重新遍历收集帧后用标准写出
                        try:
                            gif_frames = []
                            durations = []
                            for f, d in make_frames():
                                gif_frames.append(
                                    rgba_to_gif_frame(f, palette_colors, True)
                                )
                                durations.append(d)
                        except ValueError as ve:
                            logger.error(f"GIF处理中止: {ve}")
                            return None, str(ve)

                        if not gif_frames:
                            return None, "GIF没有帧数据"

                        gif_frames[0].save(
                            output_path,
                            format="GIF",
                            save_all=True,
                            append_images=gif_frames[1:],
                            duration=durations,
                            loop=0,
                            disposal=2,
                            transparency=GIF_TRANSPARENT_INDEX,
                            optimize=True,
                        )
                        return len(gif_frames), None

                finally:
                    # 恢复原始设置
                    ImageFile.LOAD_TRUNCATED_IMAGES = original_load_truncated

            result = await loop.run_in_executor(None, process_gif_in_thread)

            if result[0] is None:
                return False, result[1]

            return True, "GIF处理成功"

        except Exception as e:
            return False, f"GIF处理失败: {str(e)}"

    @staticmethod
    def _apply_mirror(image: Image.Image, mode: str) -> Image.Image:
        """
        应用图像变换

        Args:
            image: PIL图像对象
            mode: 对称模式

        Returns:
            Image.Image: 变换后的图像
        """
        if mode == "invert":
            return MirrorProcessor._apply_invert(image)

        width, height = image.size
        # 创建新图像
        result = Image.new(image.mode, (width, height))

        if mode == "left_to_right":
            # 左半边对称到右边（处理奇数宽度）
            half_width = (width + 1) // 2
            other_half = width - half_width
            left_half = image.crop((0, 0, half_width, height))
            right_half = left_half.transpose(Image.FLIP_LEFT_RIGHT)
            result.paste(left_half, (0, 0))
            # 只粘贴右侧需要的列数（防止越界）
            if other_half > 0:
                right_piece = right_half.crop(
                    (right_half.width - other_half, 0, right_half.width, height)
                )
                result.paste(right_piece, (half_width, 0))

        elif mode == "right_to_left":
            # 右半边对称到左边（处理奇数宽度）
            half_width = (width + 1) // 2
            other_half = width - half_width
            # 右侧包含上取整的一半（确保中间像素被正确处理）
            right_half = image.crop((other_half, 0, width, height))
            left_half = right_half.transpose(Image.FLIP_LEFT_RIGHT)
            # 只粘贴左侧需要的列数
            if other_half > 0:
                left_piece = left_half.crop((0, 0, other_half, height))
                result.paste(left_piece, (0, 0))
            result.paste(right_half, (other_half, 0))

        elif mode == "top_to_bottom":
            # 上半边对称到下面（处理奇数高度）
            half_height = (height + 1) // 2
            other_half = height - half_height
            top_half = image.crop((0, 0, width, half_height))
            bottom_half = top_half.transpose(Image.FLIP_TOP_BOTTOM)
            result.paste(top_half, (0, 0))
            if other_half > 0:
                bottom_piece = bottom_half.crop(
                    (0, bottom_half.height - other_half, width, bottom_half.height)
                )
                result.paste(bottom_piece, (0, half_height))

        elif mode == "bottom_to_top":
            # 下半边对称到上面（处理奇数高度）
            half_height = (height + 1) // 2
            other_half = height - half_height
            bottom_half = image.crop((0, other_half, width, height))
            top_half = bottom_half.transpose(Image.FLIP_TOP_BOTTOM)
            if other_half > 0:
                top_piece = top_half.crop((0, 0, width, other_half))
                result.paste(top_piece, (0, 0))
            result.paste(bottom_half, (0, other_half))

        else:
            # 默认不处理
            return image.copy()

        return result

    @staticmethod
    def _apply_invert(image: Image.Image) -> Image.Image:
        """反转图像颜色，保留透明通道。"""
        if image.mode == "RGBA":
            red, green, blue, alpha = image.split()
            rgb = Image.merge("RGB", (red, green, blue))
            inverted = ImageOps.invert(rgb)
            inverted.putalpha(alpha)
            return inverted

        if image.mode == "LA":
            luminance, alpha = image.split()
            inverted = ImageOps.invert(luminance)
            return Image.merge("LA", (inverted, alpha))

        if image.mode == "L":
            return ImageOps.invert(image)

        if image.mode != "RGB":
            image = image.convert("RGB")

        return ImageOps.invert(image)

    @staticmethod
    def get_mode_description(mode: str) -> str:
        """
        获取对称模式的描述

        Args:
            mode: 对称模式

        Returns:
            str: 模式描述
        """
        descriptions = {
            "left_to_right": "左半边图像对称到右边",
            "right_to_left": "右半边图像对称到左边",
            "top_to_bottom": "上半边图像对称到下面",
            "bottom_to_top": "下半边图像对称到上面",
            "invert": "反转图像颜色",
        }
        return descriptions.get(mode, "未知对称模式")
