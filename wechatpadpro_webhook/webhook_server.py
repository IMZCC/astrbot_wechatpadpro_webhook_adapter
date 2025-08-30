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
PROCESSED_MESSAGES = set()
MAX_QUEUE_SIZE = 1000
WX_ID = None
DEBUG_MODE = False

def set_wx_id(wx_id):
    global WX_ID
    WX_ID = wx_id


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
    global message_queue, PROCESSED_MESSAGES, DEBUG_MODE
    
    if request.method == 'HEAD':
        # 健康检查，直接返回200
        return '', 200
        
    # 解析消息
    try:
        data = request.get_json(force=True, silent=True) or {}
        
        # 调试信息
        if DEBUG_MODE:
            logging.info(f"🔍 原始数据: {data}")
            logging.info(f"🔍 Headers: {dict(request.headers)}")

        # 兼容大小写的字段获取
        wxid = data.get('Wxid') or data.get('wxid')

        # 调试模式下不强制验证
        if DEBUG_MODE:
            logging.warning("⚠️ 调试模式: 自动通过验证")
            wxid = WX_ID

        # 验证逻辑
        if not wxid:
            logging.warning("⚠️ 缺少wxid字段")
            return jsonify({"status": "rejected", "reason": "missing wxid"}), 400

        if WX_ID and wxid != WX_ID:
            logging.warning(f"⚠️ 拒绝非目标消息 (来自: {wxid})")
            return jsonify({"status": "rejected", "reason": "invalid sender"}), 403
        
        # 异步处理消息
        def put_message():
            try:
                asyncio.run(message_queue.put(data))
            except Exception as e:
                logger.error(f"添加消息到队列时出错: {e}")
                
        threading.Thread(target=put_message, daemon=True).start()
        
        return jsonify({
            "status": "accepted",
            "received_wxid": wxid,
            "timestamp": datetime.now().timestamp()
        }), 200
    except json.JSONDecodeError:
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400
    except queue.Full:
        return jsonify({"status": "error", "message": "Server busy"}), 503
    except Exception as e:
        logger.error(f"处理Webhook请求时发生未预期的错误: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

server = None
def run_webhook_server(host='0.0.0.0', port=8000):
    """运行 Webhook 服务器"""
    global server
    try:
        server = make_server(host, port, app, threaded=True)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        logger.info(f"🚀 Webhook server is running on {host}:{port}...")
    except Exception as e:
        logger.error(f"启动Webhook服务器失败: {e}")

def stop_webhook_server():
    """停止 Webhook 服务器"""
    global message_queue, server
    try:
        if message_queue:
            # 清空队列
            while not message_queue.empty():
                try:
                    message_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        if server:
            server.shutdown()
            server = None
        logger.info("Webhook server stopped.")
    except Exception as e:
        logger.error(f"Error stopping webhook server: {e}")

if __name__ == '__main__':
    run_webhook_server()