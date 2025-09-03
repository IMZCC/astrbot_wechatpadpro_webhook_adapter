import asyncio
import base64
import json
import os
import traceback
import time
from typing import Optional
import threading

import aiohttp
import anyio
from astrbot import logger
from astrbot.api.message_components import Plain, Image, At, Record
from astrbot.api.platform import Platform, PlatformMetadata
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astrbot_message import (
    AstrBotMessage,
    MessageMember,
    MessageType,
)
from astrbot.core.platform.register import register_platform_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.platform.astr_message_event import MessageSesion

from .wechatpadpro_message_event import WeChatPadProWebhookMessageEvent
from .webhook_server import run_webhook_server, set_message_queue, stop_webhook_server, set_wx_id

try:
    from .xml_data_parser import GeweDataParser
except ImportError as e:
    logger.warning(
        f"警告: 可能未安装 defusedxml 依赖库，将导致无法解析微信的 表情包、引用 类型的消息: {str(e)}"
    )


@register_platform_adapter("wechatpadpro_webhook", "WeChatPadPro 消息平台适配器", default_config_tmpl={
    "wx_id": "机器人的wx_id",
    "wechatpadpro_address": "http://192.168.10.221:8066",
    "webhook_listen_port": 9909,
    "webhook_secret_key": "aaaa",
   
})
class WeChatPadProWebhookAdapter(Platform):
    def __init__(
        self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue
    ) -> None:
        super().__init__(event_queue)
        self._shutdown_event = None
        self.wxnewpass = None
        self.config = platform_config
        self.settings = platform_settings
        self.unique_session = platform_settings.get("unique_session", False)

        self.metadata = PlatformMetadata(
            name="wechatpadpro_webhook",
            description="WeChatPadPro 消息平台适配器",
            id=self.config.get("id", "wechatpadpro_webhook"),
        )

        # 保存配置信息
        self.wechatpadpro_address = self.config.get("wechatpadpro_address")
        self.webhook_listen_port = self.config.get("webhook_listen_port")
        
        self.base_url = self.wechatpadpro_address
        self.wxid = self.config.get("wx_id")
        
        self.message_handler_task = None

        # 添加图片消息缓存，用于引用消息处理
        self.cached_images = {}
        """缓存图片消息。key是NewMsgId (对应引用消息的svrid)，value是图片的base64数据"""
        # 设置缓存大小限制，避免内存占用过大
        self.max_image_cache = 50

        # 添加文本消息缓存，用于引用消息处理
        self.cached_texts = {}
        """缓存文本消息。key是NewMsgId (对应引用消息的svrid)，value是消息文本内容"""
        # 设置文本缓存大小限制
        self.max_text_cache = 100

        # 创建用于接收 webhook 消息的队列
        self.webhook_queue = asyncio.Queue()

    async def handle_webhook_messages(self):
        """
        处理从 Webhook 接收到的消息。
        """
        while True:
            try:
                message_data = await self.webhook_queue.get()
                logger.debug(f"收到 Webhook 消息: {message_data}")
                
                if message_data.get("Wxid") is not None \
                    and message_data.get("Data") and len(message_data.get("Data")) > 0:
                    abm = await self.convert_message(message_data.get("Data"))
                    if abm:
                        # 创建 WeChatPadProMessageEvent 实例
                        message_event = WeChatPadProWebhookMessageEvent(
                            message_str=abm.message_str,
                            message_obj=abm,
                            platform_meta=self.meta(),
                            session_id=abm.session_id,
                            # 传递适配器实例，以便在事件中调用 send 方法
                            adapter=self,
                        )
                        # 提交事件到事件队列
                        self.commit_event(message_event)
                else:
                    logger.warning(f"收到非有效的 Webhook 消息: {message_data}")
                    
            except Exception as e:
                logger.error(f"处理 Webhook 消息时发生错误: {e}")
                logger.error(traceback.format_exc())

    async def run(self) -> None:
        """
        启动平台适配器的运行实例。
        """
        logger.info("WeChatPadPro 适配器正在启动...")

        isLoginIn = await self.check_online_status()

        # 检查在线状态
        if isLoginIn:
            logger.info("WeChatPadPro 设备已在线")
        else:
            logger.warning("未在线，请登录后配置此适配器。")
            await self.terminate()
            return

        # 启动 Webhook 服务器
        set_wx_id(self.wxid)
        set_message_queue(self.webhook_queue)
        self.webhook_server_thread = threading.Thread(
            target=run_webhook_server,
            args=('0.0.0.0', self.webhook_listen_port),
            daemon=True
        )
        self.webhook_server_thread.start()
        logger.info(f"WeChatPadPro Webhook 服务器已启动在端口 {self.webhook_listen_port}")

        # 启动消息处理任务
        self.message_handler_task = asyncio.create_task(self.handle_webhook_messages())

        self._shutdown_event = asyncio.Event()
        await self._shutdown_event.wait()
        logger.info("WeChatPadPro 适配器已停止。")

    async def terminate(self):
        """
        终止一个平台的运行实例。
        """
        logger.info("终止 WeChatPadPro 适配器。")
        try:
            if self.message_handler_task and not self.message_handler_task.done():
                self.message_handler_task.cancel()
            if self._shutdown_event:
                self._shutdown_event.set()
            stop_webhook_server()
        except Exception as e:
            logger.error(f"终止 WeChatPadPro 适配器时出错: {e}")

    async def check_online_status(self):
        """
        检查 WeChatPadPro 设备是否在线。
        """
        if not self.wxid:
            return False
        url = f"{self.base_url}/Login/GetCacheInfo?wxid={self.wxid}"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url) as response:
                    response_data = await response.json()
                    # 根据提供的在线接口返回示例，成功状态码是 200，loginState 为 1 表示在线
                    if response.status == 200 and response_data.get("Code") == 1:
                        bot_name = response_data.get("Data", {}).get("NickName")
                        if bot_name:
                            logger.info(f"WeChatPadPro 设备当前在线，昵称: {bot_name}")
                            return True
                        else:
                            logger.warning("WeChatPadPro 设备当前在线，但未获取到昵称。")
                            return True
                    else:
                        logger.error(f"检查在线状态失败: {response.status}, {response_data}")
                        return False

            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return False
            except Exception as e:
                logger.error(f"检查在线状态时发生错误: {e}")
                logger.error(traceback.format_exc())
                return False

    async def convert_message(self, raw_message: dict) -> AstrBotMessage | None:
        """
        将 WeChatPadPro 原始消息转换为 AstrBotMessage。
        """
        if not raw_message or "messages" not in raw_message or not raw_message.get("messages"):
            logger.warning("消息内容为空")
            return None
            
        raw_message = raw_message.get("messages")
        logger.debug(f"转换 WeChatPadPro 消息前: {raw_message}")
        raw_message = raw_message[0]
        logger.debug(f"转换 WeChatPadPro 消息后: {raw_message}")
        abm = AstrBotMessage()
        abm.raw_message = raw_message
        abm.message_id = str(raw_message.get("msgId"))
        abm.timestamp = raw_message.get("createTime")
        abm.self_id = self.wxid

        if int(time.time()) - abm.timestamp > 180:
            logger.warning(
                f"忽略 3 分钟前的旧消息：消息时间戳 {abm.timestamp} 超过当前时间 {int(time.time())}。"
            )
            return None
        
        from_user = raw_message.get("fromUser", '')
        to_user = raw_message.get("toUser", '')
        content = raw_message.get('text', '')
        push_content = raw_message.get('pushContent', '')
        msg_type = raw_message.get("msgType")
        
        abm.message_str = ""
        abm.message = []

        # 如果是机器人自己发送的消息、回显消息或系统消息，忽略
        if from_user == self.wxid:
            logger.info("忽略来自自己的消息。")
            return None

        if from_user in ["weixin", "newsapp", "newsapp_wechat"]:
            logger.info("忽略来自微信团队的消息。")
            return None

        # 先判断群聊/私聊并设置基本属性
        if await self._process_chat_type(abm, raw_message, from_user, to_user, content, push_content):
            # 再根据消息类型处理消息内容
            await self._process_message_content(abm, raw_message, msg_type, content)

            return abm
        return None

    async def _process_chat_type(
        self,
        abm: AstrBotMessage,
        raw_message: dict,
        from_user: str,
        to_user: str,
        content: str,
        push_content: str,
    ):
        """
        判断消息是群聊还是私聊，并设置 AstrBotMessage 的基本属性。
        """
        if from_user == "weixin":
            return False
        at_me = False
        if "@chatroom" in from_user:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = from_user

            parts = content.split(":\n", 1)
            sender_wxid = parts[0] if len(parts) == 2 else ""
            abm.sender = MessageMember(user_id=sender_wxid, nickname="")

            # 获取群聊发送者的nickname
            if sender_wxid:
                accurate_nickname = await self._get_group_member_nickname(
                    abm.group_id, sender_wxid
                )
                if accurate_nickname:
                    abm.sender.nickname = accurate_nickname

            # 对于群聊，session_id 可以是群聊 ID 或发送者 ID + 群聊 ID (如果 unique_session 为 True)
            if self.unique_session:
                abm.session_id = f"{from_user}#{abm.sender.user_id}"
            else:
                abm.session_id = from_user

            msg_source = raw_message.get("text", "")
            if self.wxid in msg_source:
                at_me = True
            if "在群聊中@了你" in raw_message.get("pushContent", ""):
                at_me = True
            if at_me:
                abm.message.insert(0, At(qq=abm.self_id, name=""))
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.group_id = ""
            nick_name = raw_message.get("fromNick", "")
            abm.sender = MessageMember(user_id=from_user, nickname=nick_name)
            abm.session_id = from_user
        return True


    async def _get_group_member_nickname(
        self, group_id: str, member_wxid: str
    ) -> Optional[str]:
        """
        通过接口获取群成员的昵称。
        """
        # 检查必要的配置是否存在
        if not hasattr(self, 'auth_key') or not self.auth_key:
            logger.warning("缺少 auth_key 配置，无法获取群成员昵称")
            return None
            
        url = f"{self.base_url}/group/GetChatroomMemberDetail"
        payload = {
            "ChatRoomName": group_id,
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    response_data = await response.json()
                    if response.status == 200 and response_data.get("Code") == 200:
                        # 从返回数据中查找对应成员的昵称
                        member_list = (
                            response_data.get("Data", {})
                            .get("memberData", {})
                            .get("chatroomMemberList", [])
                        )
                        for member in member_list:
                            if member.get("userMame") == member_wxid:
                                return member.get("nickMame")
                        logger.warning(
                            f"在群 {group_id} 中未找到成员 {member_wxid} 的昵称"
                        )
                    else:
                        logger.error(
                            f"获取群成员详情失败: {response.status}, {response_data}"
                        )
                    return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"获取群成员详情时发生错误: {e}")
                logger.error(traceback.format_exc())
                return None

    async def _download_raw_image(
        self, from_user_name: str, to_user_name: str, msg_id: int
    ):
        """下载原始图片。"""
        # 检查必要的配置是否存在
        if not hasattr(self, 'auth_key') or not self.auth_key:
            logger.warning("缺少 auth_key 配置，无法下载图片")
            return None
            
        url = f"{self.base_url}/message/GetMsgBigImg"
        payload = {
            "CompressType": 0,
            "FromUser": from_user_name,
            "MsgId": msg_id,
            "Section": {"DataLen": 61440, "StartPos": 0},
            "ToUser": to_user_name,
            "TotalLen": 0,
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.error(f"下载图片失败: {response.status}")
                        return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"下载图片时发生错误: {e}")
                logger.error(traceback.format_exc())
                return None

    async def download_voice(
        self, to_user_name: str, new_msg_id: str, bufid: str, length: int
    ):
        """下载原始音频。"""
        # 检查必要的配置是否存在
        if not hasattr(self, 'auth_key') or not self.auth_key:
            logger.warning("缺少 auth_key 配置，无法下载语音")
            return None
            
        url = f"{self.base_url}/message/GetMsgVoice"
        payload = {
            "Bufid": bufid,
            "ToUser": to_user_name,
            "NewMsgId": new_msg_id,
            "Length": length,
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.error(f"下载音频失败: {response.status}")
                    return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"下载音频时发生错误: {e}")
                logger.error(traceback.format_exc())
                return None

    async def _process_message_content(
        self, abm: AstrBotMessage, raw_message: dict, msg_type: int, content: str
    ):
        """
        根据消息类型处理消息内容，填充 AstrBotMessage 的 message 列表。
        """
        try:
            if msg_type == 1:  # 文本消息
                abm.message_str = content
                if abm.type == MessageType.GROUP_MESSAGE:
                    parts = content.split(":\n", 1)
                    if len(parts) == 2:
                        message_content = parts[1]
                        abm.message_str = message_content

                        # 检查是否@了机器人，参考 gewechat 的实现方式
                        # 微信大部分客户端在@用户昵称后面，紧接着是一个\u2005字符（四分之一空格）
                        at_me = False

                        # 检查 msg_source 中是否包含机器人的 wxid
                        # wechatpadpro 的格式: <atuserlist>wxid</atuserlist>
                        # gewechat 的格式: <atuserlist><![CDATA[wxid]]></atuserlist>
                        msg_source = raw_message.get("msgSource", "")
                        if (
                            f"<atuserlist>{abm.self_id}</atuserlist>" in msg_source
                            or f"<atuserlist>{abm.self_id}," in msg_source
                            or f",{abm.self_id}</atuserlist>" in msg_source
                        ):
                            at_me = True

                        # 也检查 push_content 中是否有@提示
                        push_content = raw_message.get("text", "")
                        if "在群聊中@了你" in push_content:
                            at_me = True

                        if at_me:
                            # 被@了，在消息开头插入At组件（参考gewechat的做法）
                            bot_nickname = await self._get_group_member_nickname(
                                abm.group_id, abm.self_id
                            )
                            abm.message.insert(
                                0, At(qq=abm.self_id, name=bot_nickname or abm.self_id)
                            )

                            # 只有当消息内容不仅仅是@时才添加Plain组件
                            if "\u2005" in message_content:
                                # 检查@之后是否还有其他内容
                                parts = message_content.split("\u2005")
                                if len(parts) > 1 and any(
                                    part.strip() for part in parts[1:]
                                ):
                                    abm.message.append(Plain(message_content))
                            else:
                                # 检查是否只包含@机器人
                                is_pure_at = False
                                if (
                                    bot_nickname
                                    and message_content.strip() == f"@{bot_nickname}"
                                ):
                                    is_pure_at = True
                                if not is_pure_at:
                                    abm.message.append(Plain(message_content))
                        else:
                            # 没有@机器人，作为普通文本处理
                            abm.message.append(Plain(message_content))
                    else:
                        abm.message.append(Plain(abm.message_str))
                else:  # 私聊消息
                    abm.message.append(Plain(abm.message_str))

                # 缓存文本消息，以便引用消息可以查找
                try:
                    # 获取msg_id作为缓存的key
                    new_msg_id = raw_message.get("newMsgId")
                    if new_msg_id:
                        # 限制缓存大小
                        if (
                            len(self.cached_texts) >= self.max_text_cache
                            and self.cached_texts
                        ):
                            # 删除最早的一条缓存
                            oldest_key = next(iter(self.cached_texts))
                            self.cached_texts.pop(oldest_key)

                        logger.debug(f"缓存文本消息，newMsgId={new_msg_id}")
                        self.cached_texts[str(new_msg_id)] = content
                except Exception as e:
                    logger.error(f"缓存文本消息失败: {e}")
            elif msg_type == 3:
                # 图片消息
                from_user_name = raw_message.get("fromUser", '')
                to_user_name = raw_message.get("toUser", '')
                msg_id = raw_message.get("msgId")
                image_resp = await self._download_raw_image(
                    from_user_name, to_user_name, msg_id
                )
                if image_resp and "Data" in image_resp:
                    image_bs64_data = (
                        image_resp.get("Data", {}).get("Data", {}).get("Buffer", None)
                    )
                    if image_bs64_data:
                        abm.message.append(Image.fromBase64(image_bs64_data))
                        # 缓存图片，以便引用消息可以查找
                        try:
                            # 获取msg_id作为缓存的key
                            new_msg_id = raw_message.get("newMsgId")
                            if new_msg_id:
                                # 限制缓存大小
                                if (
                                    len(self.cached_images) >= self.max_image_cache
                                    and self.cached_images
                                ):
                                    # 删除最早的一条缓存
                                    oldest_key = next(iter(self.cached_images))
                                    self.cached_images.pop(oldest_key)

                                logger.debug(f"缓存图片消息，newMsgId={new_msg_id}")
                                self.cached_images[str(new_msg_id)] = image_bs64_data
                        except Exception as e:
                            logger.error(f"缓存图片消息失败: {e}")
            elif msg_type == 47:
                # 视频消息 (注意：表情消息也是 47，需要区分)
                try:
                    data_parser = GeweDataParser(
                        content=content,
                        is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                        raw_message=raw_message,
                    )
                    emoji_message = data_parser.parse_emoji()
                    if emoji_message is not None:
                        abm.message.append(emoji_message)
                except Exception as e:
                    logger.warning(f"解析表情消息失败: {e}")
            elif msg_type == 50:
                logger.warning("收到语音/视频消息，待实现。")
            elif msg_type == 34:
                # 语音消息
                try:
                    bufid = 0
                    to_user_name = raw_message.get("toUser", '')
                    new_msg_id = raw_message.get("newMsgId")
                    data_parser = GeweDataParser(
                        content=content,
                        is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                        raw_message=raw_message,
                    )

                    voicemsg = data_parser._format_to_xml().find("voicemsg")
                    bufid = voicemsg.get("bufid") or "0"
                    length = int(voicemsg.get("length") or 0)
                    voice_resp = await self.download_voice(
                        to_user_name=to_user_name,
                        new_msg_id=new_msg_id,
                        bufid=bufid,
                        length=length,
                    )
                    if voice_resp and "Data" in voice_resp:
                        voice_bs64_data = voice_resp.get("Data", {}).get("Base64", None)
                        if voice_bs64_data:
                            voice_bs64_data = base64.b64decode(voice_bs64_data)
                            temp_dir = os.path.join(get_astrbot_data_path(), "temp")
                            # 确保临时目录存在
                            os.makedirs(temp_dir, exist_ok=True)
                            file_path = os.path.join(
                                temp_dir, f"wechatpadpro_voice_{abm.message_id}.silk"
                            )

                            async with await anyio.open_file(file_path, "wb") as f:
                                await f.write(voice_bs64_data)
                            abm.message.append(Record(file=file_path, url=file_path))
                except Exception as e:
                    logger.error(f"处理语音消息时出错: {e}")
                    logger.error(traceback.format_exc())
            elif msg_type == 49:
                try:
                    parser = GeweDataParser(
                        content=content,
                        is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                        cached_texts=self.cached_texts,
                        cached_images=self.cached_images,
                        raw_message=raw_message,
                        downloader=self._download_raw_image,
                    )
                    components = await parser.parse_mutil_49()
                    if components:
                        abm.message.extend(components)
                        abm.message_str = "\n".join(
                            c.text for c in components if isinstance(c, Plain)
                        )
                except Exception as e:
                    logger.warning(f"msg_type 49 处理失败: {e}")
                    abm.message.append(Plain("[XML 消息处理失败]"))
                    abm.message_str = "[XML 消息处理失败]"
            else:
                logger.warning(f"收到未处理的消息类型: {msg_type}。")
        except Exception as e:
            logger.error(f"处理消息内容时发生错误: {e}")
            logger.error(traceback.format_exc())

    def meta(self) -> PlatformMetadata:
        """
        得到一个平台的元数据。
        """
        return self.metadata

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ):
        dummy_message_obj = AstrBotMessage()
        dummy_message_obj.session_id = session.session_id
        # 根据 session_id 判断消息类型
        if "@chatroom" in session.session_id:
            dummy_message_obj.type = MessageType.GROUP_MESSAGE
            if "#" in session.session_id:
                dummy_message_obj.group_id = session.session_id.split("#")[0]
            else:
                dummy_message_obj.group_id = session.session_id
            dummy_message_obj.sender = MessageMember(user_id="", nickname="")
        else:
            dummy_message_obj.type = MessageType.FRIEND_MESSAGE
            dummy_message_obj.group_id = ""
            dummy_message_obj.sender = MessageMember(user_id="", nickname="")
        sending_event = WeChatPadProWebhookMessageEvent(
            message_str="",
            message_obj=dummy_message_obj,
            platform_meta=self.meta(),
            session_id=session.session_id,
            adapter=self,
        )
        # 调用实例方法 send
        await sending_event.send(message_chain)

    async def get_contact_list(self):
        """
        获取联系人列表。
        """
        # 检查必要的配置是否存在
        if not hasattr(self, 'auth_key') or not self.auth_key:
            logger.warning("缺少 auth_key 配置，无法获取联系人列表")
            return None
            
        url = f"{self.base_url}/friend/GetContactList"
        params = {"key": self.auth_key}
        payload = {"CurrentChatRoomContactSeq": 0, "CurrentWxcontactSeq": 0}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status != 200:
                        logger.error(f"获取联系人列表失败: {response.status}")
                        return None
                    result = await response.json()
                    if result.get("Code") == 200 and result.get("Data"):
                        contact_list = (
                            result.get("Data", {})
                            .get("ContactList", {})
                            .get("contactUsernameList", [])
                        )
                        return contact_list
                    else:
                        logger.error(f"获取联系人列表失败: {result}")
                        return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"获取联系人列表时发生错误: {e}")
                logger.error(traceback.format_exc())
                return None

    async def get_contact_details_list(
        self, room_wx_id_list: list[str] = None, user_names: list[str] = None
    ) -> Optional[dict]:
        """
        获取联系人详情列表。
        """
        # 检查必要的配置是否存在
        if not hasattr(self, 'auth_key') or not self.auth_key:
            logger.warning("缺少 auth_key 配置，无法获取联系人详情列表")
            return None
            
        if room_wx_id_list is None:
            room_wx_id_list = []
        if user_names is None:
            user_names = []
        url = f"{self.base_url}/friend/GetContactDetailsList"
        params = {"key": self.auth_key}
        payload = {"RoomWxIDList": room_wx_id_list, "UserNames": user_names}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status != 200:
                        logger.error(f"获取联系人详情列表失败: {response.status}")
                        return None
                    result = await response.json()
                    if result.get("Code") == 200 and result.get("Data"):
                        contact_list = result.get("Data", {}).get("contactList", {})
                        return contact_list
                    else:
                        logger.error(f"获取联系人详情列表失败: {result}")
                        return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"获取联系人详情列表时发生错误: {e}")
                logger.error(traceback.format_exc())
                return None