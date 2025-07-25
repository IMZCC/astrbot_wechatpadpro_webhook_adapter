
from astrbot.api.star import Context, Star, register

@register("WeChatPadProAdapterPlugin", "IMZCC", "WeChatPadPro的 Webhook 插件", "1.0.0")
class WeChatPadProAdapterPlugin(Star):
    def __init__(self, context: Context):
        from .wechatpadpro_webhook.webhook_adapter import WeChatPadProAdapter