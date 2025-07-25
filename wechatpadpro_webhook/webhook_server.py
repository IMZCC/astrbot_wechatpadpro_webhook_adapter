from flask import Flask, request, jsonify
import hmac
import hashlib
import json
import logging
import asyncio
import threading
import queue
from datetime import datetime
from astrbot import logger
from werkzeug.serving import make_server

app = Flask(__name__)

# 消息队列
message_queue = asyncio.Queue()

# 配置
WEBHOOK_SECRET = "wh_sk_2024_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0"
INCLUDE_SELF_MESSAGE = True
PROCESSED_MESSAGES = set()
MAX_QUEUE_SIZE = 1000


def set_message_queue(queue):
    """设置消息队列"""
    global message_queue
    message_queue = queue

def verify_signature(data, timestamp, signature, secret):
    mac = hmac.new(secret.encode('utf-8'), digestmod=hashlib.sha256)
    mac.update(timestamp.encode('utf-8'))
    mac.update(data)
    expected_signature = mac.hexdigest()
    return hmac.compare_digest(expected_signature, signature)

# 自动格式化所有字段
def pretty_format(data, indent=0):
    spacing = '  ' * indent
    if isinstance(data, dict):
        result = ""
        for key, value in data.items():
            result += f"{spacing}- {key}:"
            if isinstance(value, (dict, list)):
                result += "\n" + pretty_format(value, indent + 1)
            else:
                result += f" {value}\n"
        return result
    elif isinstance(data, list):
        result = ""
        for idx, item in enumerate(data):
            result += f"{spacing}- [{idx}]:\n" + pretty_format(item, indent + 1)
        return result
    else:
        return f"{spacing}{data}\n"

# 格式化日志输出
def format_message(data):
    timestamp = data.get("timestamp", None)
    try:
        if isinstance(timestamp, (int, float)):
            time_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        else:
            time_str = str(timestamp)
    except Exception:
        time_str = str(timestamp)

    return (
        f"\n✅ Received webhook message at {time_str}:\n"
        f"{pretty_format(data)}"
    )


# Webhook 接口
@app.route('/webhook', methods=['POST', 'HEAD'])
def webhook():
    global message_queue, PROCESSED_MESSAGES
    
    if request.method == 'HEAD':
        # 健康检查，直接返回200
        return '', 200
    
    raw_data = request.data

    # 获取请求数据
    timestamp = request.headers.get('X-Webhook-Timestamp')
    signature = request.headers.get('X-Webhook-Signature')
    

    logger.info(f"Received {timestamp}, {signature}, {raw_data}")
    
    # 验证签名
    if not verify_signature(raw_data, timestamp, signature, WEBHOOK_SECRET):
        return jsonify({"status": "error", "message": "Invalid signature"}), 401
    
    # 解析消息
    try:
        body = request.get_data(as_text=True)
        message = json.loads(body)
    except json.JSONDecodeError:
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400
    
    # 检查消息ID
    message_id = message.get("msgId")
    if not message_id:
        return jsonify({"status": "error", "message": "Missing message ID"}), 400
    
    # 检查是否重复消息
    if message_id in PROCESSED_MESSAGES:
        return jsonify({"status": "success", "message": "Duplicate message"}), 200
    
    # 添加到已处理集合
    PROCESSED_MESSAGES.add(message_id)
    if len(PROCESSED_MESSAGES) > MAX_QUEUE_SIZE:
        PROCESSED_MESSAGES.pop()
    
    # 将消息加入队列异步处理
    try:
        threading.Thread(target=lambda: asyncio.run(message_queue.put(message))).start()
    except queue.Full:
        return jsonify({"status": "error", "message": "Server busy"}), 503
    
    # 立即返回成功
    return jsonify({"status": "success"}), 200

server = None
def run_webhook_server(host='0.0.0.0', port=8000):
    """运行 Webhook 服务器"""
    server = make_server(host, port, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    logger.info(f"🚀 Webhook server is running on {host}:{port}...")
    
def stop_webhook_server():
    """停止 Webhook 服务器"""
    global message_queue
    try:
        if message_queue:
            message_queue.queue.clear()
        if server:
            server.shutdown()
        logger.info("Webhook server stopped.")
    except Exception as e:
        logger.error(f"Error stopping webhook server: {e}")

if __name__ == '__main__':
    run_webhook_server()