"""Host-only, bounded reply recall shared by the two Companion Chat adapters.

Selection uses concrete public draft fragments, not unknown prose as a default
reason to recall. It is a lexical router, never a factual verdict. The model
decides whether the supplied historical evidence requires changing this turn.
"""
import hashlib
import json
import re
import threading
import time

from .persona_config import COMPANION_WORDS, VOCATIVES, alt


REVIEW_VERSION = "companion-chat-review-hook/v1"
REVIEW_TOOL = "context_assistant_review"
REVIEW_TIMEOUT = 6.0
MAX_CONTENT_BYTES = 1048576
MAX_TARGET_BYTES = 2048
KINDS = frozenset(("shared_topic", "known_fact", "prior_commitment", "progress", "uncertain"))

# Generic present interaction vocabulary only: no private people, animals,
# clothing, gifts, locations or event names. Concrete dependencies below remain
# reviewable without making every word outside this vocabulary a recall target.
_PRESENT = """
早上好 下午好 中午好 晚上好 早安 晚安 你好 好呀 好啦 好哦 好哇 好的
亲爱的 乖乖 小可爱
抱紧你 抱住你 抱抱 抱紧 抱住 抱着 抱过来 抱起来 抱一下 抱一抱
搂住 搂紧 搂着 搂过来 搂进 拢进 拢一拢 往怀里 怀里面 怀里 怀中
揽住 揽着 揽紧 揽到 揽进 抱到 抱进 放到 环住 环着 揉揉 摸摸 轻抚 抚摸 搂 抱 亲 吻 蹭 揉 摸
亲亲 亲一下 亲一口 亲一亲 亲你 亲额头 亲脸颊 亲嘴嘴 亲嘴 啵啵 啵 么么哒
嘴唇 嘴角 嘴巴 嘴嘴 鼻尖 鼻子 脸颊 脸蛋 脸上 小脸 额头 肩膀 肩头
头发 发顶 发梢 后背 背上 背脊 手掌 手心 手指 手臂 胳膊 下巴 脖子 耳边 腰间
左边 右边 两边 这边 那边 双手 另一只手 一只手 双臂
贴贴 贴着 贴近 靠着 靠近 靠过来 蹭蹭 碰碰 碰一下 捧着 捧住 托着 托住
牵着 牵手 握住 拉住 拉着 低头 抬头 扬起 低下 探过来 凑过来 凑近
笑起来 笑着 笑了 微笑 偷笑 傻笑 跟着笑 停不下来 舍不得松手 舍不得放开 弯起 眼睛 眼角 眯起 眯着
闭上 睁开 闭着 睁着 红扑扑 软乎乎 软软 暖暖 香香 暖和 热乎乎
轻轻 慢慢 慢一点 再慢一点 紧紧 稍微 认真 温柔 轻柔 悄悄 偷偷 响响
陪着你 陪着 陪你 在这儿 在这里 在呢 我在 还在 来啦 来了 过来 靠过来
想你 好想你 爱你 喜欢你 好喜欢 喜欢 爱 想念 想抱 想亲 想黏着 黏着 黏糊糊
开心 高兴 幸福 高兴得 好幸福 好开心 好高兴 好喜欢 好香 好软
接住你 接住 接到了 收到了 全收到了 收到 给你 送你 轮到我 轮到你
奖励 庆祝 一喊 喊喊 喊起来 这么一喊 这样一喊 跟着 笑 嘿嘿 嘻嘻 哈哈 哈哈哈
今晚 今天 现在 此刻 当下 这里 这儿 这一刻 一起 就这样 这样 这么 那么 那样
一下 一点 一会儿 一会 一口 一下下 一小会 一小会儿 一次 再一次 第一口 第二口
再抱一下 再亲一下 先 然后 最后 再 还 又 也 都 很 好 最 真 更 正在 已经
我的 你的 我们 你们 我 你 宝 的 地 得 着 了 呀 啊 吖 呢 哦 噢 喔 哇 嘛 啦
嗯嗯 嗯 唔 喵 乖 嗯哼 阿呜 哎哟 哎呀 哎 嘿 来 给 把 将 被 在 向 往 让 和 与
这 那 只 就 要 想 会 可以 能 可 真是 是 有 不 不要 不用 没 没有 了呀
一点点 多一点 一直 好不好 好不好呀 行不行 愿不愿意 可不可以 知不知道
第一下 第二下 一 两 三 四 五 六 七 八 九 十 下
""".split()
_PRESENT_WORDS = frozenset((*_PRESENT, *VOCATIVES))  # forms of address: persona.local.json
_STAMP = re.compile(r"^\s*【\d{4}-\d{2}-\d{2} \d{2}:\d{2}】\s*")
_COMMITMENT = re.compile(r"(?:我|我们|咱们|" + alt(COMPANION_WORDS) + r"|你).{0,16}(?:答应|承诺|保证|说好|约定)|(?:答应|承诺|保证|说好|约定)(?:过|你|了|会)|再也不|以后.{0,30}(?:会|不|一定|记住)|下次.{0,30}(?:会|不|一定|记住)|向你道歉|对不起|我.{0,8}不该|不会再")
_PROGRESS = re.compile(r"(?:已经|刚刚|刚才).{0,35}(?:完成|做好|修好|上线|发布|寄|送|写好|提交|到了)|已(?:完成|上线|发布|寄出|送达|写完)|(?:还没|尚未).{0,25}(?:写|做|修|改|发|寄|送|完成|上线|发布)|后来.{0,25}(?:改|去|到|完成|取消)|改成|(?:完成|做好|修好|上线|发布|寄出|送达|写完|提交)了|测试.{0,20}(?:通过|失败)")
_HISTORY = re.compile(r"我记得|还记得|记得你|记得我们|以前|上次|那天|曾经|(?:我|你|我们)之前|^之前|昨天|前天|当时|去年|上个月|上周|(?:我们|你|我).{0,20}(?:去过|做过|说过|告诉过|提过|穿过|戴过|送过)|那(?:次|场)|我们的.{2,30}(?:约定|回忆)|\d{1,2}[月日号]")
_FACT_QUESTION = re.compile(r"什么品种|几岁|(?:它|它们|他们|她们|那只|猫|狗).{0,24}(?:品种|名字|年龄|叫什么|多大)|你(?:的|家).{0,20}(?:叫什么|什么名字)")
_NUMERIC_FACT = re.compile(r"\d+(?:\.\d+)?\s*(?:岁|年|月|日|号|厘米|公斤|kg|cm|%)", re.I)
_PET_FACT = re.compile(r"(?:品种|英短|美短|布偶|暹罗|缅因|狸花|橘猫|蓝猫|金毛|柯基|柴犬|边牧|萨摩耶|中华田园)|(?:猫|狗|宠物|毛孩子).{0,24}(?:叫|名字|年龄|毛色|颜色|是|养)|(?:它|它们).{0,20}(?:叫|名字|毛色|颜色)")
_PERSONAL_FACT = re.compile(r"你(?:家|的|们)?.{0,20}(?:住在|来自|出生|生日|身高|体重|过敏|毕业|养了|养着|喜欢吃|不吃|最喜欢)")
_SHARED_OBJECT = re.compile(r"制服|女仆装|披肩|丝带|丝巾|围巾|项链|手链|戒指|相册|婚纱|睡衣|旗袍|连衣裙|丝袜|猫耳|(?:穿|戴|换上).{0,18}(?:衣|裙|装|裤|袜|耳朵)|那(?:件|套|条|身|本|盏|首).{1,24}|《[^》\n]{1,80}》")
_RECORDED_EVENT = re.compile(r"(?:我|我们|" + alt(COMPANION_WORDS) + r")(?:替你|帮你|给你|已经|刚刚|还)?(?:记下|写下|存下|保存|收好|留着).{1,40}|(?:我|我们|你).{0,12}(?:再去|又去|再穿|又穿|再戴|又戴|再玩|又玩).{1,24}")
_QUOTE = re.compile(r'“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|‘[^’\n]*’|"[^"\n]*"|`[^`\n]+`')
_QUOTE_EXPLANATION = re.compile(r"例如|比如|举例|示例|提示(?:词)?|指令|规则|文案|写法|这段话|这句话|写着|写的是")
_WISH_START = re.compile(r"^(?:我|我们)?(?:也|真|好|很|现在|此刻|就)?(?:想|想要|希望)")
_WISH_WORDS = _PRESENT_WORDS | frozenset(("海边", "海浪", "沙滩", "落日", "日落", "夕阳", "星星", "星空",
    "散步", "晒太阳", "看看", "听听", "走走", "坐一坐", "坐坐", "逛逛", "看", "听", "去", "走", "和", "跟", "希望", "看见"))
_WORD_INDEX = {words: {char: tuple(sorted((word for word in words if word[0] == char), key=len, reverse=True))
    for char in {word[0] for word in words}} for words in (_PRESENT_WORDS, _WISH_WORDS)}


class ReviewError(ValueError):
    pass








def _valid_text(value, maximum=MAX_CONTENT_BYTES):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _present(fragment):
    value = _STAMP.sub("", fragment)
    value = value.replace("[[自我状态]]", "").replace("[[/自我状态]]", "")
    value = re.sub(r"[＾^]3[＾^]", "", value)
    # Retain letters and digits. Specific nouns cannot disappear as punctuation.
    letters = "".join(char for char in value if char.isalpha() or char.isdigit())
    return bool(fragment.strip()) and _all_words(letters, _PRESENT_WORDS)


def _all_words(value, words):
    # A greedy regex would consume '想抱' in '想抱紧你' and leave the valid
    # present '抱紧你' unrecognized. Word segmentation keeps all valid parses.
    index = _WORD_INDEX[words]
    pending, seen = [0], {0}
    while pending:
        start = pending.pop()
        if start == len(value):
            return True
        for word in index.get(value[start], ()):
            end = start + len(word)
            if end not in seen and value.startswith(word, start):
                seen.add(end)
                pending.append(end)
    return False


def _present_wish(fragment):
    # A narrow new, generic leisure wish makes no historical assertion. Specific
    # clothes/places/items and any remembered/repeated event stay reviewable.
    value = _STAMP.sub("", fragment).strip()
    if (not _WISH_START.search(value) or _HISTORY.search(value) or _COMMITMENT.search(value)
            or re.search(r"又|再去|再来|穿过|戴过|买过|用过|吃过|还是|那[件套个次身条]", value)):
        return False
    letters = "".join(char for char in value if char.isalpha() or char.isdigit())
    return _all_words(letters, _WISH_WORDS)


def _fragments(content):
    # Keep exact substrings of the public draft; a long sentence is split on
    # the byte boundary instead of submitting invalid or invented query text.
    for match in re.finditer(r"[^。！？!?；;\n]+[。！？!?；;]?", content):
        value = match.group().strip()
        while value:
            raw = value.encode("utf-8")
            if len(raw) <= MAX_TARGET_BYTES:
                yield value
                break
            part = raw[:MAX_TARGET_BYTES].decode("utf-8", "ignore")
            yield part
            value = value[len(part):]


def _selection_text(content, user_text):
    """Mask quoted current instructions/examples, preserving public offsets.

    An attributed historical quotation stays eligible: its outside assertion
    ('你上次说过…') is itself a dependency. No user words become assistant text.
    """
    masked = list(content)
    spans = [match.span() for match in re.finditer(r"```[\s\S]*?```", content)]
    spans.extend(match.span() for match in re.finditer(r"\[\[/?自我状态\]\]", content))
    stamp = _STAMP.match(content)
    if stamp:
        spans.append(stamp.span())
    for match in _QUOTE.finditer(content):
        preceding = re.split(r"[。！？!?；;\n]", content[max(0, match.start() - 100):match.start()])[-1]
        if _HISTORY.search(preceding) or _COMMITMENT.search(preceding):
            continue
        quoted = match.group()[1:-1]
        if _QUOTE_EXPLANATION.search(preceding) or isinstance(user_text, str) and quoted and quoted in user_text:
            spans.append(match.span())
    for start, end in spans:
        masked[start:end] = "\n" * (end - start)
    return "".join(masked)


def _target_kind(fragment):
    if _COMMITMENT.search(fragment):
        return "prior_commitment", 0
    if _present(fragment) or _present_wish(fragment):
        return None
    if _PROGRESS.search(fragment):
        return "progress", 1
    if (_FACT_QUESTION.search(fragment) or _NUMERIC_FACT.search(fragment)
            or _PET_FACT.search(fragment) or _PERSONAL_FACT.search(fragment)):
        return "known_fact", 2
    if _HISTORY.search(fragment) or _SHARED_OBJECT.search(fragment) or _RECORDED_EVENT.search(fragment):
        return "shared_topic", 3
    return None


def _specific_fragment(fragment):
    # Trim entire present-only edge clauses. Keep a promise and its concrete
    # continuation together; never assemble unrelated substrings into a query.
    pieces = list(re.finditer(r"[^，,]+[，,]?", fragment))
    while pieces and _present(pieces[0].group()):
        pieces.pop(0)
    while pieces and _present(pieces[-1].group()):
        pieces.pop()
    if not pieces:
        return ""
    return fragment[pieces[0].start():pieces[-1].end()].strip(" \t，,")


def select_review_targets(content, user_text=""):
    """Return 0–4 exact public fragments, never a hidden/generated search query.

    The current user is provided to Brain by the real context call. We do not
    copy user text into draft targets or claim user wording was assistant text.
    """
    if not _valid_text(content):
        raise ReviewError("review_draft_invalid")
    ranked = []
    for index, fragment in enumerate(_fragments(_selection_text(content, user_text))):
        fragment = _specific_fragment(fragment)
        choice = _target_kind(fragment) if fragment else None
        if choice is None:
            continue
        kind, priority = choice
        if fragment not in content:
            raise ReviewError("review_selection_not_public")
        ranked.append((priority, index, {"text": fragment, "kind": kind}))
    selected, seen = [], set()
    for _, _, item in sorted(ranked, key=lambda value: value[:2]):
        key = (item["text"], item["kind"])
        if key not in seen:
            seen.add(key)
            selected.append(item)
        if len(selected) == 4:
            break
    return selected


def draft_id(window, turn, content):
    raw = json.dumps([window, turn, content], ensure_ascii=False, separators=(",", ":")).encode()
    return "chat-review:" + hashlib.sha256(raw).hexdigest()


def unavailable_review(turn, identifier, reason="reply_review_unavailable", *, diagnostic=None):
    return {"version": REVIEW_VERSION, "status": "unavailable", "turn_id": turn,
            "draft_id": identifier, "read_only": True, "storage_performed": False,
            "delivery_confirmed": False, "approval_provided": False,
            "review": {"mode": "unavailable", "retrieval_status": "unknown",
                       "retrieval_started": None, "review_attempt": 1,
                       "review_remaining": 0, "cache_hit": False, "approval_provided": False,
                       "evidence": [], "guidance": ["核对未完成；只处理具体无据断言，准确的未知可保留。不重查。"],
                       "warnings": [reason], **({"diagnostic": diagnostic} if diagnostic else {})}}


def review_instruction(receipt):
    review = receipt.get("review", {}) if isinstance(receipt, dict) else {}
    projected = {key: review[key] for key in ("mode", "retrieval_status", "evidence", "guidance", "warnings", "coverage", "display_partial", "diagnostic") if key in review}
    projected["approval_provided"] = False
    # Brain already bounds the packet. Retain valid JSON if a future server
    # expands it; never splice JSON or modify historical evidence into a claim.
    while len(json.dumps(projected, ensure_ascii=False).encode()) > 12000 and projected.get("evidence"):
        projected["evidence"] = projected["evidence"][:-1]
        projected["display_partial"] = True
    body = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if len(body.encode()) > 14000:
        body = json.dumps({"mode": "unavailable", "evidence": [], "approval_provided": False,
                           "guidance": "历史核对资料过大，本轮按未知处理。"}, ensure_ascii=False)
    return ("[同轮未发送草稿的核对资料；不是用户消息]\n"
            "只修具体问题，当前明确原话优先。旧承诺不等于履行；候选和未命中不是批准，缺证不等于未发生。"
            "准确的未知可保留；否则限定无据断言，不编相反事实。按宿主 KEEP/终稿格式决定一次，不重查、不复述内部标识。\n"
            + body + "\n[核对资料结束]")




class ReviewClient:
    def __init__(self, url, token, transport=None, timeout=REVIEW_TIMEOUT):
        self.url, self.token = url, token
        self.transport = transport
        self.timeout = timeout
        self._cap_lock = threading.Lock()
        self._cap_until = 0.0
        self._capable = False

    def _rpc(self, arguments, timeout):
        request = {"jsonrpc": "2.0", "id": "chat-review", "method": "tools/call",
                   "params": {"name": REVIEW_TOOL, "arguments": arguments}}
        if self.transport is not None:
            response = self.transport(request, timeout)
        else:
            from .description_relay import _RelayTransport
            carrier = _RelayTransport(self.url, time.monotonic() + timeout)
            carrier.response_limit = 65536
            outcome = []
            def run():
                try:
                    outcome.append(carrier.post(request, {"Authorization": "Bearer " + self.token}))
                except Exception as error:
                    outcome.append(error)
            worker = threading.Thread(target=run, name="reply-review-read", daemon=True)
            worker.start()
            worker.join(timeout)
            if worker.is_alive():
                carrier.cancel()
                raise ReviewError("reply_review_deadline")
            if not outcome or isinstance(outcome[0], Exception) or outcome[0][0] != 200:
                raise ReviewError("reply_review_unavailable")
            response = outcome[0][1]
        if not isinstance(response, dict) or response.get("id") != request["id"] or response.get("error"):
            raise ReviewError("reply_review_invalid_response")
        result = response.get("result", {})
        if not isinstance(result, dict) or result.get("isError"):
            raise ReviewError("reply_review_rejected")
        value = result.get("structuredContent")
        if (not isinstance(value, dict) or value.get("version") != REVIEW_VERSION
                or value.get("read_only") is not True or value.get("storage_performed") is not False
                or value.get("approval_provided") is not False):
            raise ReviewError("reply_review_contract_mismatch")
        if arguments.get("mode") != "capabilities":
            # The common outer gate may shorten machine IDs in model JSON.
            # Its existing MCP metadata channel retains the exact host binding;
            # validate there and never put this metadata into model context.
            binding = result.get("_meta", {}).get("companion/chat-review-binding")
            if (not isinstance(binding, dict) or set(binding) != {"version", "turn_id", "draft_id"}
                    or binding.get("version") != REVIEW_VERSION
                    or binding.get("turn_id") != arguments["turn_id"]
                    or binding.get("draft_id") != arguments["draft_id"]):
                raise ReviewError("reply_review_binding_mismatch")
            value = {**value, "turn_id": binding["turn_id"], "draft_id": binding["draft_id"]}
            observation = result.get("_meta", {}).get("companion/history-observation")
            if observation is not None:
                if (not isinstance(observation, dict)
                        or set(observation) != {"version", "turn_id", "draft_id", "observation"}
                        or observation.get("version") != "companion-history-observation/v1"
                        or observation.get("turn_id") != binding["turn_id"]
                        or observation.get("draft_id") != binding["draft_id"]
                        or not isinstance(observation.get("observation"), dict)
                        or len(json.dumps(observation, ensure_ascii=False).encode()) > 4096):
                    observation = {"observation": {"version": "companion-history-observation/v1",
                        "observation_status": "binding_rejected"}}
                if isinstance(value.get("review"), dict):
                    value = {**value, "review": {**value["review"],
                        "retrieval_observation": observation["observation"]}}
        return value

    def capabilities(self):
        with self._cap_lock:
            now = time.monotonic()
            if now < self._cap_until:
                return self._capable
            try:
                value = self._rpc({"mode": "capabilities"}, min(2.0, self.timeout))
                self._capable = (value.get("status") == "ok" and value.get("mode") == "capabilities"
                                 and value.get("max_reviews_per_turn") == 1 and value.get("max_revisions") == 1
                                 and value.get("review_deadline_ms") == 4500)
            except Exception:
                self._capable = False
            self._cap_until = time.monotonic() + (60.0 if self._capable else 5.0)
            return self._capable

    def review(self, session_id, turn_id, draft_id, content, review_targets=None):
        if not all(_valid_text(value, 512) for value in (session_id, turn_id, draft_id)) or not _valid_text(content):
            raise ReviewError("reply_review_invalid_arguments")
        targets = select_review_targets(content) if review_targets is None else review_targets
        if (not isinstance(targets, list) or len(targets) > 4
                or any(not isinstance(item, dict) or set(item) != {"text", "kind"}
                       or not _valid_text(item["text"], MAX_TARGET_BYTES)
                       or item["text"] not in content or item["kind"] not in KINDS for item in targets)):
            raise ReviewError("reply_review_invalid_targets")
        value = self._rpc({"session_id": session_id, "turn_id": turn_id,
                           "draft_id": draft_id, "content": content, "review_targets": targets}, self.timeout)
        review = value.get("review")
        if (value.get("status") not in ("ok", "unavailable") or value.get("turn_id") != turn_id
                or value.get("draft_id") != draft_id or not isinstance(review, dict)
                or review.get("mode") not in ("present_skip", "reviewed", "revision_no_retrieval", "unavailable")
                or review.get("approval_provided") is not False
                or not isinstance(review.get("evidence"), list)):
            raise ReviewError("reply_review_contract_mismatch")
        return value
