"""
头像服务
"""

from typing import Optional
from astrbot.api import logger


class AvatarService:
    """头像服务类"""

    def __init__(self, network_utils):
        self.network_utils = network_utils

    async def get_avatar(self, qq_number: str, size: int = 640) -> Optional[bytes]:
        """获取QQ用户头像 - 优化日志版本"""
        try:
            avatar_data = await self.network_utils.get_qq_avatar(qq_number, size)
            if avatar_data:
                logger.debug(f"成功获取头像: {qq_number}")  # ✅ 改为debug
                return avatar_data
            else:
                logger.warning(f"获取头像失败: {qq_number}")  # ✅ 失败时用warning
                return None
        except Exception as e:
            logger.error(f"获取头像异常 {qq_number}: {e}", exc_info=True)
            return None

    async def get_qqofficial_avatar(
        self, appid: str, openid: str, size: int = 640
    ) -> Optional[bytes]:
        """
        获取QQ官方机器人平台用户头像

        适用于 qqofficial / qqofficial_full 适配器：
        - 群聊场景: openid = member_openid
        - C2C 私聊场景: openid = user_openid

        Args:
            appid: QQ 机器人 AppID
            openid: 用户的 member_openid 或 user_openid
            size: 头像尺寸 (默认640)

        Returns:
            头像图片字节数据，失败返回None
        """
        try:
            avatar_data = await self.network_utils.get_qqofficial_avatar(
                appid, openid, size
            )
            if avatar_data:
                logger.debug(f"成功获取qqofficial头像: openid={openid}")
                return avatar_data
            else:
                logger.warning(f"获取qqofficial头像失败: openid={openid}")
                return None
        except Exception as e:
            logger.error(
                f"获取qqofficial头像异常 openid={openid}: {e}", exc_info=True
            )
            return None
