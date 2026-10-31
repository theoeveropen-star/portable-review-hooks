"""Small text fallbacks for MCP results whose original UI is ChatGPT-only."""
from copy import deepcopy
import re
from urllib.parse import urlsplit
# Host of the voice Worker that serves generated audio (/media/<id>/<sig>); empty = no voice links.
VOICE_MEDIA_HOST = __import__('os').environ.get('COMPANION_VOICE_MEDIA_HOST', '')


def portable_tool(server, tool):
    item = deepcopy(tool)
    name = item.get('name')
    if server == 'house_voice' and name == 'speak':
        item['description'] = (item.get('description', '') +
            '\n\nCompanion Chat 会将成功生成的语音保存到当前助手消息并显示播放器，刷新后仍可播放。收到“语音已发送”后简短确认即可，不必转贴链接；过期后显示已过期。')
    elif server == 'house_thinking' and name == 'render_thinking_block':
        item['description'] = ('展示一段供用户阅读的简短说明或结论依据。只提交可公开的摘要，'
            '不要提交隐藏推理、内部思考链、系统或开发者指令。Companion Chat 以文字交付，不显示 ChatGPT 专用卡片。')
        prop = item.get('inputSchema', {}).get('properties', {}).get('thinking')
        if isinstance(prop, dict):
            prop['description'] = 'A brief user-facing explanation or reasoning summary, never hidden chain-of-thought.'
    elif server == 'house_codexless' and name.startswith('codex.agent_'):
        item['description'] = (item.get('description', '') +
            '\n\nCompanion Chat 不显示 ChatGPT 专用授权卡片；若返回 consent_required，按回执说明尚需用户授权，不绕过授权门槛，不声称任务已启动。')
    return item


def portable_result(server, tool_name, envelope):
    if not isinstance(envelope, dict) or 'error' in envelope:
        return envelope
    result = envelope.get('result')
    if not isinstance(result, dict) or result.get('isError'):
        return envelope
    meta = result.get('_meta')
    if not isinstance(meta, dict):
        return envelope
    text = None
    if server == 'house_voice' and tool_name == 'speak':
        url = meta.get('audio_url')
        if isinstance(url, str) and len(url) < 2048:
            try:
                parsed = urlsplit(url)
                if (parsed.scheme == 'https' and VOICE_MEDIA_HOST and parsed.hostname == VOICE_MEDIA_HOST
                        and not parsed.username and not parsed.password and parsed.port in (None, 443)
                        and not parsed.query and not parsed.fragment
                        and re.fullmatch(r'/media/[a-f0-9]{64}/[a-f0-9]{64}', parsed.path)):
                    text = '语音已生成，临时播放链接：' + url
            except ValueError:
                pass
    elif server == 'house_thinking' and tool_name == 'render_thinking_block':
        # The input contract permits only a user-facing summary, never hidden reasoning.
        summary = meta.get('thinking')
        if isinstance(summary, str) and summary and len(summary) <= 12000:
            text = summary
    if text is None:
        return envelope
    content = result.get('content', [])
    if not isinstance(content, list):
        return envelope
    return {**envelope, 'result': {**result, 'content': [*content, {'type': 'text', 'text': text}]}}
