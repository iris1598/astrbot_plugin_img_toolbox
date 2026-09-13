"""流式 GIF 写出工具。

Pillow 的 ``Image.save(save_all=True)`` 会先把所有帧缓存进内存再写，帧数多时
内存占用和耗时都会显著上升。这里直接复用 Pillow 的底层帧写入原语，逐帧写出，
使内存占用与帧数无关，并复用同一全局调色板以避免重复写局部色表。

若当前 Pillow 版本缺少所需底层接口，``write_gif_streaming`` 返回 -1（且不写入
任何内容），由调用方回退到标准写出方式。
"""

from typing import Iterable, Optional, Tuple

from PIL import Image, GifImagePlugin

GIF_TRANSPARENT_INDEX = 255

_GIF_RAWMODE = {"1", "L", "P"}


def has_low_level_api() -> bool:
    """当前 Pillow 是否提供流式写出所需的底层接口。"""
    return hasattr(GifImagePlugin, "_get_global_header") and hasattr(
        GifImagePlugin, "_write_frame_data"
    )


def rgba_to_gif_frame(
    image: Image.Image,
    palette_colors: int = 256,
    reserve_transparency: bool = True,
) -> Image.Image:
    """将 RGBA 帧量化为 GIF 帧，必要时保留独立透明索引。

    透明区域的写入使用 C 层的 ``paste(mask)``，避免逐像素 Python 循环。
    """
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    alpha = image.getchannel("A")
    has_alpha = alpha.getextrema()[0] < 255
    colors = min(palette_colors, 255) if reserve_transparency else palette_colors
    p_frame = image.convert("RGB").quantize(colors=colors)

    if not reserve_transparency:
        return p_frame

    palette = p_frame.getpalette() or []
    if len(palette) < 768:
        palette.extend([0] * (768 - len(palette)))
    transparent_index = GIF_TRANSPARENT_INDEX
    palette[transparent_index * 3: transparent_index * 3 + 3] = [0, 0, 0]
    p_frame.putpalette(palette)

    if has_alpha:
        mask = alpha.point(lambda a: 255 if a < 128 else 0)
        p_frame.paste(transparent_index, (0, 0), mask)

    p_frame.info["transparency"] = transparent_index
    return p_frame


def _normalize_frame(
    frame: Image.Image,
    palette_colors: int,
    reserve_transparency: bool,
) -> Tuple[Image.Image, Optional[int]]:
    """把任意输入帧规范化为可写出的 GIF 帧，并返回其透明索引。"""
    if frame.mode == "P":
        return frame, frame.info.get("transparency")
    p_frame = rgba_to_gif_frame(frame, palette_colors, reserve_transparency)
    return p_frame, p_frame.info.get("transparency")


def _frame_params(duration_ms: int, transparency: Optional[int], include_color_table: bool) -> dict:
    params = {
        "duration": int(duration_ms),
        "disposal": 2,
        "include_color_table": include_color_table,
    }
    if transparency is not None:
        params["transparency"] = transparency
    return params


def write_gif_streaming(
    fp,
    frames: Iterable[Tuple[Image.Image, int]],
    loop: int = 0,
    palette_colors: int = 256,
    reserve_transparency: bool = True,
) -> int:
    """逐帧写出 GIF。

    Args:
        fp: 可写的二进制文件对象（如 BytesIO）。
        frames: 可迭代对象，逐项产出 ``(帧, 时长毫秒)``。帧可为 P 模式（直接写出）
            或 RGB/RGBA/L/LA（量化为 P 后写出）。
        loop: 循环次数，0 表示无限循环。
        palette_colors: 量化颜色数（P 模式输入不受影响）。
        reserve_transparency: 量化时是否为透明索引预留一个调色板位。

    Returns:
        写入的帧数；底层接口不可用时返回 -1（此时未写入任何内容）。
    """
    if not has_low_level_api():
        return -1

    count = 0
    global_palette: Optional[bytes] = None

    # 只保留上一帧（held），与下一帧比较：完全相同则合并时长，避免重复写整帧
    held: Optional[Image.Image] = None
    held_transparency: Optional[int] = None
    held_palette: Optional[bytes] = None
    held_bytes: Optional[bytes] = None
    held_duration = 0

    def flush(frame, transparency, palette_bytes, duration):
        nonlocal count, global_palette
        if count == 0:
            info = {"loop": loop, "duration": int(duration)}
            if transparency is not None:
                info["transparency"] = transparency
            for chunk in GifImagePlugin._get_global_header(frame, info):
                fp.write(chunk)
            global_palette = palette_bytes
            include_color_table = False
        else:
            include_color_table = palette_bytes != global_palette
        GifImagePlugin._write_frame_data(
            fp,
            frame,
            (0, 0),
            _frame_params(duration, transparency, include_color_table),
        )
        count += 1

    for frame, duration_ms in frames:
        p_frame, transparency = _normalize_frame(frame, palette_colors, reserve_transparency)
        palette_bytes = bytes(p_frame.getpalette() or [])
        frame_bytes = p_frame.tobytes()

        if held is not None and (
            frame_bytes == held_bytes
            and palette_bytes == held_palette
            and transparency == held_transparency
        ):
            held_duration += int(duration_ms)
            continue

        if held is not None:
            flush(held, held_transparency, held_palette, held_duration)

        held = p_frame
        held_transparency = transparency
        held_palette = palette_bytes
        held_bytes = frame_bytes
        held_duration = int(duration_ms)

    if held is not None:
        flush(held, held_transparency, held_palette, held_duration)

    if count == 0:
        return 0 if held is None else count

    fp.write(b";")
    return count
