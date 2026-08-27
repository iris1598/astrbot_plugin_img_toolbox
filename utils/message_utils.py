"""
消息解析工具模块
"""

from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote, urlparse
import astrbot.api.message_components as Comp
from astrbot.api import logger


class MessageUtils:
    """消息解析工具类"""

    # qqofficial_full 适配器平台名称（与 PlatformMetadata.name 一致）
    QQOFFICIAL_FULL_PLATFORM_NAME = "qq_official_full"
    QQOFFICIAL_FULL_WEBHOOK_PLATFORM_NAME = "qq_official_full_webhook"
    QQOFFICIAL_PLATFORM_NAMES = {
        QQOFFICIAL_FULL_PLATFORM_NAME,
        QQOFFICIAL_FULL_WEBHOOK_PLATFORM_NAME,
        # AstrBot 内置的 qqofficial / qqofficial_webhook 适配器
        "qq_official",
        "qq_official_webhook",
    }

    @staticmethod
    def is_qqofficial_platform(event) -> bool:
        """判断当前事件是否来自 qqofficial 系列适配器

        Args:
            event: AstrMessageEvent

        Returns:
            是否为 qqofficial 系列平台
        """
        try:
            platform_name = event.get_platform_name()
            return platform_name in MessageUtils.QQOFFICIAL_PLATFORM_NAMES
        except Exception:
            return False

    @staticmethod
    def detect_qqofficial_scene(event) -> Optional[str]:
        """
        判断 qqofficial 平台的当前会话场景

        依据 message_obj.type (MessageType)：
        - GROUP_MESSAGE -> "group" (群聊 GroupMessage, 使用 member_openid)
        - FRIEND_MESSAGE -> "c2c" (C2C 私聊, 使用 user_openid)
        - 其他 -> None

        Args:
            event: AstrMessageEvent

        Returns:
            "group" / "c2c" / None
        """
        # AstrBot v3.5+ 起 MessageType 通过 astrbot.api.platform 暴露
        try:
            from astrbot.api.platform import MessageType
        except ImportError:
            try:
                from astrbot.core.platform.message_type import MessageType
            except ImportError:
                return None

        try:
            msg_type = event.get_message_type()
        except Exception:
            return None

        if msg_type == MessageType.GROUP_MESSAGE:
            return "group"
        if msg_type == MessageType.FRIEND_MESSAGE:
            return "c2c"
        return None

    @staticmethod
    def extract_at_qq(event) -> Optional[str]:
        """提取@的QQ号 - 减少日志版本"""
        try:
            messages = event.get_messages()
        except AttributeError:
            messages = event.message_obj.message

        for component in messages:
            if isinstance(component, Comp.At):
                # At组件可能有qq属性
                if hasattr(component, "qq"):
                    qq_value = component.qq
                    if qq_value:
                        logger.debug(f"提取到@QQ号: {qq_value}")  # ✅ debug级别
                        return str(qq_value)

                # 或者检查其他可能的属性名
                for attr_name in ["target", "user_id", "id"]:
                    if hasattr(component, attr_name):
                        attr_value = getattr(component, attr_name)
                        if attr_value:
                            logger.debug(f"提取到@QQ号: {attr_value}")  # ✅ debug级别
                            return str(attr_value)

        return None

    @staticmethod
    def extract_at_openid_qqofficial(event) -> Optional[str]:
        """
        从 qqofficial / qqofficial_full 消息中提取被 @ 的非 bot 用户的 openid

        与 extract_at_qq 语义保持一致：
        - 仅在群聊场景下提取被 @ 用户的 member_openid
        - 私聊场景(C2C)无法 @ 他人，直接返回 None
        - 未 @ 他人时返回 None（不回退到发送者本人）

        qqofficial_full 适配器解析消息时，只会为 bot 自身生成 At 组件
        （见 qqofficial_adapter.py 的 _parse_message_event），普通用户 @ 他人
        不会产生 At 组件，但 mention 信息会保留在 raw_message.mentions 列表中。

        本方法从 raw_message.mentions 中筛选出 is_you != True 且 id != self_id
        的第一个 mention 作为被 @ 的用户。

        Args:
            event: AstrMessageEvent

        Returns:
            被 @ 用户的 openid（字符串），未找到返回 None
        """
        # 与 onebot11 策略一致：仅群聊场景下 @ 他人时才取头像
        scene = MessageUtils.detect_qqofficial_scene(event)
        if scene != "group":
            return None

        try:
            raw_message = getattr(event.message_obj, "raw_message", None)
            if not isinstance(raw_message, dict):
                return None

            mentions = raw_message.get("mentions") or []
            if not mentions:
                return None

            # 获取 bot 自身 id 用于排除
            try:
                bot_self_id = event.get_self_id()
            except Exception:
                bot_self_id = ""
            bot_self_id = str(bot_self_id or "")

            for mention in mentions:
                if not isinstance(mention, dict):
                    continue
                mention_id = str(mention.get("id") or "")
                if not mention_id:
                    continue
                # 跳过 bot 自己
                if mention.get("is_you"):
                    continue
                if mention_id == bot_self_id:
                    continue
                logger.debug(f"提取到qqofficial @ openid: {mention_id}")
                return mention_id
        except Exception as e:
            logger.warning(f"提取qqofficial @ openid 失败: {e}")
        return None

    @staticmethod
    def extract_image_source_groups(event) -> List[List[str]]:
        """提取图片及其按优先级排列的候选来源。"""
        image_source_groups = []
        seen_groups = set()

        def add_image_component(component: Comp.Image) -> None:
            sources = MessageUtils._extract_image_component_sources(component, event)
            if not sources:
                return

            source_group = tuple(sources)
            if source_group not in seen_groups:
                seen_groups.add(source_group)
                image_source_groups.append(sources)

        try:
            messages = event.get_messages()

            if not messages:
                logger.debug("event.get_messages() 返回空")
                return image_source_groups

            logger.debug(f"从get_messages()获取到消息链，长度: {len(messages)}")

            for component in messages:
                if isinstance(component, Comp.Image):
                    add_image_component(component)

                elif isinstance(component, Comp.Reply):
                    if hasattr(component, "chain") and component.chain:
                        for reply_component in component.chain:
                            if isinstance(reply_component, Comp.Image):
                                add_image_component(reply_component)
                                logger.debug("从回复消息提取到图片")

            logger.debug(f"总共找到 {len(image_source_groups)} 组图片来源")
            return image_source_groups

        except (AttributeError, TypeError, KeyError, IndexError, ValueError) as e:
            logger.error(f"提取图像源失败: {type(e).__name__}: {e}", exc_info=True)
            return []


    @staticmethod
    def extract_image_sources(event) -> List[str]:
        """兼容旧调用：返回所有候选来源构成的扁平列表。"""
        return [
            source
            for source_group in MessageUtils.extract_image_source_groups(event)
            for source in source_group
        ]

    @staticmethod
    def _extract_from_image_component(
        component: Comp.Image, event=None
    ) -> Optional[str]:
        """
        从Image组件提取图像URL

        Args:
            component: Image组件

        Returns:
            图像URL或数据
        """
        sources = MessageUtils._extract_image_component_sources(component, event)
        return sources[0] if sources else None

    @staticmethod
    def _extract_image_component_sources(
        component: Comp.Image, event=None
    ) -> List[str]:
        """按可靠性顺序提取一个图片组件的全部可用来源。"""
        sources = []

        def add_source(value) -> None:
            if isinstance(value, str) and value and value not in sources:
                sources.append(MessageUtils._recover_original_gif(event, value))

        # AstrBot 4.26.0+ 会将媒体落到本地，并把路径放入 path/file。
        path = getattr(component, "path", None)
        if isinstance(path, str) and path:
            logger.debug("从Image组件找到path属性")
            add_source(path)

        file_value = getattr(component, "file", None)
        url = getattr(component, "url", None)
        if (
            isinstance(file_value, str)
            and file_value
            and (
                not isinstance(url, str)
                or not url
                or MessageUtils._is_direct_image_source(file_value)
            )
        ):
            logger.debug("从Image组件找到可直接使用的file属性")
            add_source(file_value)

        if isinstance(url, str) and url:
            logger.debug("从Image组件找到url属性")
            add_source(url)

        for attr_name in ["data", "content"]:
            if hasattr(component, attr_name):
                attr_value = getattr(component, attr_name)
                if isinstance(attr_value, str) and attr_value:
                    logger.debug(f"从Image组件找到{attr_name}属性")  # ✅ debug级别
                    add_source(attr_value)

        if not sources:
            logger.debug("Image组件没有找到有效的URL属性")  # ✅ debug级别
        return sources

    @staticmethod
    def _is_direct_image_source(value: str) -> bool:
        """判断 file 字段是否是可直接处理的媒体引用。"""
        if value.startswith(("http://", "https://", "base64://", "file://")):
            return True
        try:
            return MessageUtils._local_reference_to_path(value).is_file()
        except (OSError, ValueError):
            return False

    @staticmethod
    def get_trusted_event_media_paths(event) -> List[str]:
        """获取 AstrBot 为当前事件登记的临时媒体路径。"""
        paths = getattr(event, "_temporary_local_files", None)
        if not isinstance(paths, (list, tuple, set)):
            return []
        return [str(path) for path in paths if isinstance(path, (str, Path))]

    @staticmethod
    def _recover_original_gif(event, source: str) -> str:
        """在 AstrBot 4.26.0 的 JPEG 预处理结果前找回原始 GIF。"""
        if event is None:
            return source

        source_path = MessageUtils._local_reference_to_path(source)
        if (
            source_path.suffix.lower() not in {".jpg", ".jpeg"}
            or not source_path.name.startswith("media_image_")
        ):
            return source

        tracked_paths = MessageUtils.get_trusted_event_media_paths(event)
        try:
            source_resolved = source_path.resolve()
        except OSError:
            return source

        for index, tracked in enumerate(tracked_paths):
            try:
                if (
                    MessageUtils._local_reference_to_path(tracked).resolve()
                    != source_resolved
                    or index == 0
                ):
                    continue
                original_path = MessageUtils._local_reference_to_path(
                    tracked_paths[index - 1]
                ).resolve()
                if not original_path.is_file():
                    return source
                with original_path.open("rb") as file:
                    if file.read(6) in {b"GIF87a", b"GIF89a"}:
                        logger.debug("从 AstrBot 临时文件记录中恢复原始 GIF")
                        return str(original_path)
            except OSError:
                return source

        return source

    @staticmethod
    def _local_reference_to_path(value: str) -> Path:
        """将普通路径或 file URI 规范化为 Path。"""
        if not isinstance(value, str):
            raise ValueError("本地路径必须是字符串")

        parsed = urlparse(value)
        if parsed.scheme.lower() != "file":
            return Path(value)

        netloc = unquote(parsed.netloc or "")
        path = unquote(parsed.path or "")
        if len(netloc) == 2 and netloc[1] == ":":
            return Path(f"{netloc}{path}")
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        if netloc and netloc.lower() != "localhost":
            path = f"//{netloc}{path}"
        return Path(path)

    @staticmethod
    def extract_command_text(event) -> Optional[str]:
        """
        提取纯文本指令

        Args:
            event: 消息事件

        Returns:
            指令文本
        """
        try:
            messages = event.get_messages()
        except AttributeError:
            messages = event.message_obj.message

        for component in messages:
            if isinstance(component, Comp.Plain):
                text = component.text.strip()
                if text:
                    return text

        return None

    @staticmethod
    def has_image_in_message(event) -> bool:
        """
        检查消息中是否包含图像

        Args:
            event: 消息事件

        Returns:
            是否包含图像
        """
        try:
            messages = event.get_messages()
        except AttributeError:
            messages = event.message_obj.message

        for component in messages:
            if isinstance(component, Comp.Image):
                return True
            elif isinstance(component, Comp.Reply):
                # 检查回复中是否有图像
                if hasattr(component, "chain") and component.chain:
                    for reply_component in component.chain:
                        if isinstance(reply_component, Comp.Image):
                            return True

        return False
