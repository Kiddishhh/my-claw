"""正式服务与验收脚本共用事件注册，避免订阅与处理器不匹配。"""


def ignore_chat_entered(event) -> None:
    """打开机器人单聊只确认事件，不执行 Agent，不主动发送欢迎消息。"""
    return None


def build_event_handler(on_message):
    # 延迟导入，确保调用入口已设置 SDK 所属事件循环。
    import lark_oapi as lark
    return (lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(ignore_chat_entered)
            .build())
