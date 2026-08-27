"""
配置服务
"""

from astrbot.api import logger

from ..config import PluginConfig
from ..constants import _load_version


class ConfigService:
    """配置服务类"""

    PLUGIN_VERSION = _load_version()

    def __init__(self, plugin_instance):
        self.plugin = plugin_instance
        self._config = None  # 延迟加载

    def _load_config(self) -> PluginConfig:
        """简化版本 - 只用标准方式

        直接读取 AstrBot 在实例化插件时传入的 config（AstrBotConfig，WebUI 可调）。
        """
        try:
            config_dict = getattr(self.plugin, "cfg", None)
            if isinstance(config_dict, dict):
                return PluginConfig.load_from_dict(dict(config_dict))
            return PluginConfig()
        except (AttributeError, TypeError, KeyError) as e:
            logger.error(f"配置加载失败，使用默认配置: {e}", exc_info=True)
            return PluginConfig()

    def get_config_summary(self) -> str:
        """获取配置摘要"""
        # 确保配置已加载
        config = self.config_obj  # 使用config_obj属性确保加载

        return (
            f"图像限制={config.image_size_limit_mb}MB, "
            f"GIF限制={config.gif_size_limit_mb}MB, "
            f"频率限制={config.rate_limit_per_minute}次/分钟, "
            f"并发上限={config.max_concurrent_tasks}, "
            f"自动清理={'启用' if config.enable_auto_cleanup else '禁用'}, "
            f"@头像功能={'启用' if config.enable_at_avatar else '禁用'}, "
            f"qqofficial适配={'启用' if config.qqofficial_appid else '自动'}"
        )

    def get_help_text(self) -> str:
        """获取帮助文本"""
        config = self.config

        if config.silent_mode:
            return """🖼️ 图片工具箱 - 镜像效果

可用指令:
• 左对称 / mirror left - 左半边对称到右边
• 右对称 / mirror right - 右半边对称到左边
• 上对称 / mirror top - 上半边对称到下面
• 下对称 / mirror bottom - 下半边对称到上面
• 反色 / invert - 反转图像颜色

使用方法:
1. 回复一条包含图像的消息，然后发送指令
2. 发送指令并@一个用户 (处理该用户头像)
3. 直接发送图像和指令在同一消息中

支持格式: PNG, JPG, GIF, BMP, WebP

示例:
回复图片消息后发送: 左对称
@用户 并发送: 右对称
图片 + 反色"""
        else:
            return f"""🖼️ 图片工具箱 v{self.PLUGIN_VERSION}

【镜像效果】
• 左对称 / 右对称 / 上对称 / 下对称 / 反色
• 可处理图片 / GIF 动画 / @用户头像

【GIF 动图】
• 视频转gif - 视频转为动图 (支持 -start/-end/-dur/-fps/-step/-scale)
• 合成1gif / 合成2gif - 精灵图/雪碧图合成动图
• gif变速 - 倍速或帧率变速 (2x / 30fps)
• 加速 / 减速 - 快捷变速
• gif倒放 / 倒放 - 动图倒放
• gif分解 - 逐帧拆解为静态图
• 多图合成gif - 多张图片合成动图

【静态图】
• 裁剪 - 网格九宫格/宫格裁剪 (如 3x3 边距10)
• 图片转线稿 - 生成线稿素描效果
• 表情包做旧 - 模拟早期互联网传播效果

【其他】
• 图片帮助 / 工具箱帮助 - 本帮助
• 对称帮助 / 镜像帮助 - 镜像命令帮助

使用方法: 回复含图消息并输入指令，或直接 @ 本机器人并输入指令即可。
唤醒方式: 私聊直接发送；群聊请 @ 机器人或以 / 开头。

GitHub: https://github.com/iris1598/astrbot_plugin_img_toolbox"""

    @property
    def config_obj(self) -> PluginConfig:
        """获取配置对象"""
        if self._config is None:
            self._config = self._load_config()
        return self._config

    @property
    def config(self):
        """配置对象别名"""
        return self.config_obj
