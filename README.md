# WeChatPadPro Webhook 适配器插件

这是一个用于 AstrBot 的 WeChatPadPro Webhook 适配器插件，它将 WeChatPadPro 的消息接收方式从 WebSocket 更改为 Webhook。

## 功能特性

- 使用 Webhook 接收 WeChatPadPro 消息，替代原有的 WebSocket 方式
- 保持与原有 WeChatPadPro 适配器相同的功能和接口
- 支持文本、图片、表情、引用消息等多种消息类型
- 支持发送文本、图片、表情、语音等消息

## 安装

1. 将插件文件夹复制到 AstrBot 的 `data/plugins/` 目录下
2. 在 AstrBot 的配置文件中启用该插件

## 配置

在 AstrBot 的配置文件中添加以下配置项：

```yaml
platforms:
  - name: wechatpadpro
    enable: true
    admin_key: "your_admin_key"
    host: "your_wechatpadpro_host"
    port: 8059
    webhook_port: 9909
```

配置项说明：
- `admin_key`: WeChatPadPro 的管理员密钥
- `host`: WeChatPadPro 服务的主机地址
- `port`: WeChatPadPro 服务的端口
- `webhook_port`: Webhook 服务器监听的端口

## 使用

1. 启动 AstrBot
2. 插件会自动启动 Webhook 服务器并监听指定端口
3. 在 WeChatPadPro 中配置 Webhook 地址为 `http://your_astrbot_host:webhook_port/webhook`
4. 扫码登录 WeChatPadPro
5. 开始使用 WeChatPadPro 发送和接收消息

## 注意事项

- 确保 AstrBot 服务器的 Webhook 端口可以从 WeChatPadPro 服务器访问
- Webhook 服务器使用了简单的签名验证，请确保 WeChatPadPro 发送的请求包含正确的签名