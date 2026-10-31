"""Host-side query obligations; no fact inference and no name-specific rules."""
import copy
import re

from .persona_config import VOCATIVES, alt

_WORD = re.compile(r'[A-Za-z][A-Za-z0-9_.-]*|[\u3400-\u9fff]+')
_DENIAL = re.compile(r'不记得|(?:一点)?印象(?:都)?没有|没(?:有)?(?:一点)?印象|没有.{0,24}(?:这个人|这个工具)|没听过|做不到|不能使用|用不了|没有用过|没找回|没找到')
_STOP = re.compile(r'^(?:我|我们|你|现在|脑子里|一点|这个|那个|确实|完全|真的|暂时|还|也|对|关于|是|的|了|过)+|(?:一点|完全|真的|暂时|也|还|我|这个人|这个工具|的事|的事情)$')

def executable(query):
    return isinstance(query,str) and bool(_WORD.search(query))

def clean(value):
    value=value.strip(' \n\t，,。！!？?：:；;“”\"\'`<>')
    for _ in range(3):value=_STOP.sub('',value).strip()
    return value if 1 < len(value) <= 64 and executable(value) else ''

def denial_targets(draft):
    found=[]
    for sentence in re.findall(r'[^。！？!?\n]+[。！？!?]?',draft):
        if not _DENIAL.search(sentence):continue
        names=[]
        for pattern in (
            r'没有\s*([^，,。！？!?]{1,40}?)\s*(?:这个人|这个工具)',
            r'(?:不记得|没听过|没有用过|没找回|没找到)\s*[“\"「]?([^，,。！？!?”\"」]{2,40})',
            r'([^，,。！？!?]{2,40}?)(?:我)?(?:一点|完全|真的|暂时|也|还)*(?:没(?:有)?印象|印象(?:都)?没有)',
            r'(?:我)?(?:做不到|不能使用|用不了)\s*([^，,。！？!?]{2,40})',
        ):
            names.extend(clean(m[1]) for m in re.finditer(pattern,sentence))
        names=[v for v in dict.fromkeys(names) if v and not _DENIAL.search(v)]
        for name in names:
            found.append({'text':sentence,'kind':'history_dependency','current_evidence_ids':[],'query':name})
    return found[:4]

# Reverse recall: a draft that admits it guessed or has not looked at her words
# gets one bounded host retrieval for the guessed subject (rules only, no model).
_GUESS = re.compile(r'猜的|是猜|瞎猜|没去翻|还没翻|没翻原话|去(?:原话|原文|档案|记录)?里?翻|翻不到|没翻到|手边没有|没核实|没去查|还没查')
_NOT_GUESS = re.compile(r'不(?:用|必)再?去翻|不用再翻')
_CLAUSE = re.compile(r'[，,；;：:、\s]+')
_FILLER = sorted("""原话里 原话 原文 档案 聊天记录 记录 下一轮 下一回 下回 这一轮 这回 这次 这轮 回来 到底 是什么
 什么 怎么 自己 直接 可是 但是 不过 所以 因为 现在 刚才 一下 一遍 一只 一个 不用 写上了 写上 写的 告诉
 先招 一处 不然 每次 当时 转述 这是 说好 这首 那首 这家 那家 叫 我们 你们 他们 再来 求你 问你 就 先 再 还 都 也 又 可 但 得 给 把 被 让 我 你 他 她 它 的 了 着
 吗 呢 吧 啊 呀 是 在 有 没 不 去 来 里 写 说 问 求 招 认 那 这 能 会 要 想""".split(), key=len, reverse=True)
_FILLER_RE = re.compile('|'.join(map(re.escape, _FILLER)))
_TERM = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{1,31}|[\u3400-\u9fff]{2,8}')
GUESS_LIMIT = 2
# What is being guessed about a subject, normalized to the words the Archive
# answers with ("花色" alone also matches card suits).
_ATTRS = (('颜色', r'颜色|花色|毛色'), ('长什么样', r'长什么样|样子|长相'), ('名字', r'名字|叫什么'),
          ('品种', r'品种'), ('生日', r'生日'), ('歌词', r'歌词'))
ATTR_NAMES = frozenset(name for name, _ in _ATTRS)
_ATTR = re.compile('|'.join('(?P<a%d>%s)' % (i, pattern) for i, (_, pattern) in enumerate(_ATTRS)))


def _normal(term):
    match = _ATTR.fullmatch(term)
    return _ATTRS[int(match.lastgroup[1:])][0] if match else term


def _subject(clause):
    rest = _FILLER_RE.sub(' ', _GUESS.sub(' ', clause))
    return list(dict.fromkeys(_normal(t) for t in _TERM.findall(rest)))[:3]


def _enrich(terms, draft, user):
    """Add the attribute the draft/her message asks about right after the subject."""
    subject = next((t for t in terms if t not in ATTR_NAMES), None)
    if subject is None:
        return terms
    found = []
    for text in (draft, user):
        for at in [m.start() for m in re.finditer(re.escape(subject), text)]:
            match = _ATTR.search(text, at + len(subject), at + len(subject) + 10)
            if match:
                found.append(_ATTRS[int(match.lastgroup[1:])][0])
    return list(dict.fromkeys([*terms, *found]))[:3]


def guess_targets(draft, user=''):
    """Sentences that admit a guess / unread original, with a short subject query.

    The subject comes from the admitting clause itself, else from the nearest
    earlier clause of the same sentence that is not itself an admission and has
    words (any later fallback only when one of its terms also appears in her
    message or elsewhere in the draft). Anchor = the whole sentence (exact draft text).
    """
    found = []
    admits = lambda c: bool(_GUESS.search(c)) and not _NOT_GUESS.search(c)
    for sentence in re.findall(r'[^。！？!?\n]+[。！？!?]?', draft):
        if not admits(sentence):
            continue
        clauses = [c for c in _CLAUSE.split(sentence) if c.strip()]
        for i, clause in enumerate(clauses):
            if not admits(clause):
                continue
            terms = _subject(clause)
            strong, nearest = bool(terms), True
            for j in range(i - 1, -1, -1) if not terms else ():
                if admits(clauses[j]):
                    continue
                terms = _subject(clauses[j])
                if not terms:
                    continue
                # The claim usually sits right before its admission; a farther
                # one counts only when its words recur (her message / draft).
                if nearest or any(t in user or draft.count(t) >= 2 for t in terms):
                    break
                nearest, terms = False, []
            if terms:
                found.append({'text': sentence.strip(), 'kind': 'history_dependency',
                              'current_evidence_ids': [], 'query': ' '.join(_enrich(terms, draft, user)), 'strong': strong})
    seen, ranked = set(), []
    # Subjects she named this turn first, then the admitting clause's own words.
    for item in sorted(found, key=lambda x: (not any(t in user for t in x['query'].split()), not x['strong'])):
        if item['query'] in seen or item['text'] in {r['text'] for r in ranked}:
            continue
        seen.add(item['query'])
        ranked.append({k: item[k] for k in ('text', 'kind', 'current_evidence_ids', 'query')})
    return ranked[:GUESS_LIMIT]


# Fact questions: a draft that ASKS her a stable attribute of a named subject
# ("<name>……她是什么品种呀？") gets one bounded host retrieval, so a known answer
# replaces the question. Rules only, no model call. The query is the bare subject:
# Brain's review memory lane expands whole cards only for a literal name (12 KB);
# a "<pet name> 品种" query got 6 KB, two story lines filled it and the pet's card
# stayed unexpanded. The attribute ranks the host's own evidence.
FACT_LIMIT = 2
FACT_RANK_WORDS = {'品种': ('品种', '长这样'), '名字': ('名字', '叫'), '生日': ('生日',),
                   '颜色': ('颜色', '毛色', '花色', '长这样'), '长什么样': ('长这样', '样子', '长相'), '年龄': ('岁', '年龄')}
# Interrogative attribute forms, normalized to the words the Archive/memory answer with.
_Q_ATTRS = (('品种', r'(?:什么|啥|哪种|哪个)品种|品种(?:是|叫)?(?:什么|啥|哪)'),
            ('名字', r'叫(?:什么|啥)|(?:什么|啥)名字|名字(?:是|叫)?(?:什么|啥)'),
            ('生日', r'生日(?:是|在)?(?:哪|什么时候|啥时候|几)|(?:哪天|哪一天|什么时候|啥时候|几号|几月)(?:过)?生日'),
            ('颜色', r'(?:什么|啥)(?:颜色|毛色|花色)|(?:颜色|毛色|花色)(?:是)?(?:什么|啥)'),
            ('长什么样', r'长(?:什么|啥)样|(?:什么|啥)样子'),
            ('年龄', r'几岁|多少岁|多大了|多大啦|年龄(?:是)?多(?:大|少)'))
_Q_ATTR = re.compile('|'.join('(?P<q%d>%s)' % (i, pattern) for i, (_, pattern) in enumerate(_Q_ATTRS)))
# The same attribute already said as a statement elsewhere in the draft.
_STATED = {'品种': r'品种|是[^，,。！？!?]{0,6}(?:猫|狗|犬)', '名字': r'名字|名叫', '生日': r'生日',
           '颜色': r'颜色|毛色|花色|[^，,。！？!?]{0,4}色', '长什么样': r'长得|样子|长相',
           '年龄': r'[0-9一二两三四五六七八九十]+岁|年龄'}
_ASKS = re.compile(r'(?:[？?]|(?:吗|呀|呢|啊|嘛|来着)[\s~～…]*[。！!]?)\s*$')
_PRONOUN = re.compile(r'(?<![其吉])(?:她|他|它)')
_LEAD = re.compile(r'^(?:那么|那|所以|对了|话说|诶|哎|欸|这么说)+')
_TAIL = re.compile(r'(?:的|到底|究竟|又|都|就|现在|今年)*是?$')
_ZERO = re.compile(r'^(?:到底|究竟)?是?$')
# Reduplicated pet-style names (豆豆, 球球); the doubled char must not be a common
# adverb/verb/adjective/kinship/onomatopoeia doubling (刚刚, 看看, 粉粉, 宝宝, 哈哈).
_DOUBLE = re.compile(r'([一-鿿])\1')
_DOUBLE_STOP = frozenset('刚常慢好天偷轻悄渐往默稍乖人个谢看试想说等听走抱亲摸蹭哈嘿呜嘻呵嗯啊哇喵汪咕统仅明久处年时种样偏白满多大小高深静早紧狠稳乱滚溜乎呼噜吧啦嘟哒叨唠嗷呱叽喳刷哗砰咚滴点片层步声句字次回真实清楚干净甜暖软香胖瘦红蓝绿黑黄粉圆扁尖长短厚薄亮暗快冷热凉温湿闷脆酥滑宝哥弟妹姐爸妈爷奶姥舅叔婶伯公婆姨星微略恰匆纷屡每各平淡远近齐整双对家户日夜月分秒条件位只张本朵颗粒块根歇聊玩逛跑跳笑哭拍敲揉捏戳挠舔咬吃喝睡躺坐站')
_LATIN = re.compile(r'(?<![A-Za-z0-9_])[A-Z][a-z]{2,15}(?![A-Za-z0-9_])')
_LATIN_STOP = frozenset(('Companion', 'Claude', 'Codex', 'Android', 'Brain', 'Chat', 'Work', 'App', 'Fable', 'Opus', 'Sonnet'))
_QUOTED = re.compile(r'[「“]([^」”\s]{1,12})[」”]')
# The two people themselves (persona.local.json) and parents are never a looked-up subject.
_OWNER_WORDS = (*VOCATIVES, '爸爸', '妈妈')
_OWNER = frozenset(_OWNER_WORDS)
_GENERIC = re.compile(r'今天|昨天|明天|今年|去年|现在|刚才|后来|以前|上次|这次|那次|平时|最近|外面|天气|^(?:' + alt(_OWNER_WORDS) + r')|^老[一二三四五六七八九十大幺]|^[这那哪你我他她它咱您每某一两几各本该此其]|[猫狗犬鸟鱼兔鼠龟马牛羊猪虫花草树歌曲书剧片店车人们家城国市种只位个首件条款]$|一般|通常|大多|平常|大概|应该|可能')
_INTRO = re.compile(r'叫做?|名叫|名字是|取名|起名|新(?:养|捡|来|认识)|刚(?:养|捡|认识|领)|捡了|认识了?|一只|一个|一位|有只|有个|朋友|同事|邻居|同学|她家|他家')


def _names(text):
    """Strong named terms with positions: quoted, capitalized Latin, reduplicated."""
    found = [(m.start(1), m[1]) for m in _QUOTED.finditer(text)]
    found += [(m.start(), m[0]) for m in _LATIN.finditer(text) if m[0] not in _LATIN_STOP]
    found += [(m.start(), m[0]) for m in _DOUBLE.finditer(text) if m[1] not in _DOUBLE_STOP]
    return [term for _, term in sorted(found) if term not in _OWNER]


def _possessor(prefix):
    """The clause's own topic right before the asked attribute (豆豆是什么颜色呀)."""
    rest = _TAIL.sub('', _LEAD.sub('', prefix))
    if re.fullmatch(r'[一-鿿]{2,4}', rest) and rest not in _OWNER and not _GENERIC.search(rest):
        return rest
    return None


def _introduced(subject, user):
    """Her current message brings this subject in for the first time."""
    at = user.find(subject)
    while at >= 0:
        if _INTRO.search(user[max(0, at - 12):at]):
            return True
        at = user.find(subject, at + len(subject))
    return False


def fact_question_targets(draft, user=''):
    """Questions asking her a stable attribute (品种/名字/生日/颜色/长什么样/年龄) of a named subject.

    Subject = a named term in the asking clause before the attribute, else, when
    that clause uses 她/他/它 (or drops the subject), the nearest named term in the
    earlier clauses of the same sentence, then the previous sentence.
    """
    sentences = re.findall(r'[^。！？!?\n]+[。！？!?]?', draft)
    found = []
    for index, sentence in enumerate(sentences):
        if not _ASKS.search(sentence):
            continue
        clauses = [c for c in _CLAUSE.split(sentence) if c.strip()]
        for i, clause in enumerate(clauses):
            match = _Q_ATTR.search(clause)
            if not match:
                continue
            attr = _Q_ATTRS[int(match.lastgroup[1:])][0]
            prefix = clause[:match.start()]
            names = _names(prefix)
            subject = names[-1] if names else _possessor(prefix)
            if subject is None and (_PRONOUN.search(clause) or _ZERO.match(_LEAD.sub('', prefix))):
                before = [c for c in _CLAUSE.split(sentences[index - 1]) if c.strip()] if index else []
                earlier = before + clauses[:i]
                for previous in reversed(earlier):
                    names = _names(previous)
                    if names:
                        subject = names[-1]
                        break
                else:
                    # Nothing named in the draft before it: she named it just now.
                    names = _names(user)
                    subject = names[-1] if names else None
            if subject is None or _introduced(subject, user):
                continue
            stated = re.compile(re.escape(subject) + r'[^。！？!?\n]{0,12}?(?:' + _STATED[attr] + ')')
            if any(stated.search(s) for j, s in enumerate(sentences) if j != index and not _ASKS.search(s)):
                continue
            found.append({'text': sentence.strip(), 'kind': 'history_dependency', 'current_evidence_ids': [],
                          'query': subject, 'attr': attr})
            break
    ranked, seen = [], set()
    for item in found:
        if item['query'] in seen or item['text'] in {r['text'] for r in ranked}:
            continue
        seen.add(item['query'])
        ranked.append(item)
    return ranked[:FACT_LIMIT]


def apply_guard(result, draft, user=''):
    """Keep original provider result diagnostics; require retrieval on named denials, admitted guesses and fact questions."""
    denials=denial_targets(draft)
    guesses=[g for g in guess_targets(draft, user) if not any(g['query']==d['query'] or g['text']==d['text'] for d in denials)]
    facts=[f for f in fact_question_targets(draft, user) if not any(f['query']==o['query'] or f['text']==o['text'] for o in denials+guesses)]
    obligations=denials+guesses+[{k: f[k] for k in ('text','kind','current_evidence_ids','query')} for f in facts]
    if result.get('status')!='ok' and not obligations:return result
    result=copy.deepcopy(result)
    plan=result.get('plan') or {'route':'pass','issues':[]}
    issues=copy.deepcopy(plan.get('issues',[]))
    for issue in issues:
        if issue.get('kind') in ('history_dependency','history_request') and not executable(issue.get('query')):
            candidates=denial_targets(issue['text'])
            fallback=candidates[0]['query'] if candidates else ' '.join(_WORD.findall(issue['text']))[:128]
            if not executable(fallback):
                result.update(status='unavailable',error='history_retrieval_failed_empty_query',plan=None,work=None)
                return result
            issue['query']=fallback
    added=[]
    for obligation in reversed(obligations):
        # A sentence the provider already sends to history keeps its own query
        # (the contract refuses a duplicate anchor+kind).
        if not any((i.get('query')==obligation['query'] or i.get('text')==obligation['text']) and i.get('kind') in ('history_dependency','history_request') for i in issues):
            issues.insert(0,obligation);added.append(obligation['query'])
    issues=issues[:4]
    plan['issues']=issues
    plan['route']='history_review' if any(i.get('kind') in ('history_dependency','history_request') for i in issues) else ('current_repair' if issues else 'pass')
    provider_status=result.get('status')
    result.update(status='ok',plan=plan)
    kept={i.get('query') for i in issues}
    result['name_guard']={'required_queries':[x['query'] for x in denials],'provider_status':provider_status,
                          'guess_queries':[x['query'] for x in guesses if x['query'] in added and x['query'] in kept],
                          'fact_queries':[x['query'] for x in facts if x['query'] in added and x['query'] in kept],
                          'fact_attrs':[x['attr'] for x in facts if x['query'] in added and x['query'] in kept]}
    return result
