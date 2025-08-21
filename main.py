
from astrbot.api.star import Context, Star, register

@register("WeChatPadProWebhookAdapterPlugin", "IMZCC", "WeChatPadPro的 Webhook 插件", "1.1.0")
class WeChatPadProWebhookAdapterPlugin(Star):
    def __init__(self, context: Context):
        from .wechatpadpro_webhook.webhook_adapter import WeChatPadProWebhookAdapter