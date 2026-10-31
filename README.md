# pre-send-review · 发送前审查

给 AI 伴侣 / 聊天助手用的「发送前审查」：回复写完先不发，由另一个模型挑问题，宿主自己去查证据，助手最多改一次，再把终稿交出去。专门拦这几种错：编造往事、反问用户早就说过的事、明明有记录却说「不记得」、没有回执却说「已经做了」。

- 为什么这样设计、一轮怎么走：[docs/设计思路.md](docs/设计思路.md)
- 接线检查表、写死的规矩、限制：[使用说明与注意事项.pdf](使用说明与注意事项.pdf)
- 召回接口：[INTERFACE.md](INTERFACE.md)；可选的本轮预取与复用：[PREFETCH.md](PREFETCH.md)；Codex 版：[CODEX-README.md](CODEX-README.md)

> 这是我们自己家里在用的审查抽出来的独立版本，在隔离环境里用合成数据测过，没有在别人的真实账号、记忆库和模型上整链验收过。先在你自己的隔离环境里接线试一遍，再用于真实聊天。

## 下载

两种方式任选一种：

- `git clone` 这个仓库，然后按下面「安装」走。
- 在 Releases 下载 `Portable-Review-Hooks-V5-20261031.zip`，解压后先运行 `python3 -B verify_manifest.py` 校验（应显示 Verified 60 files）。

## 它是什么

这个包提供 Claude Code 和 Codex 两种命令行宿主。Codex 版使用方式和限制见 [Codex 说明](CODEX-README.md)。以下是 Claude Code 版。它在公开草稿完成后通过 Stop Hook 做一次语义审查；有具体问题时最多要求一次续答。审查期间工具封条生效，KEEP 选择会还原原稿，终稿经过公开事件绑定校验。需要历史材料时，向你自己的召回适配器发查询；不依赖特定记忆系统，也不带信箱。

**第一版范围**：两版都是独立命令行宿主，不是把一个 settings.json 丢进任意现有聊天前端即可使用的插件。它使用 Claude Code 2.1.284 的公开事件和 Hook 格式；其他版本需重新验证。模型只开放 Read、Glob、Grep；每次运行新建 native session，带入预算内最近两轮已完成原话。它不会读取你的完整聊天历史，不提供自动 slim、GUI、MCP、附件上传或记忆写入。新应用若要内嵌，需要实现本包宿主等价的事件绑定、封条及状态机，不能只拷 Stop 脚本。

## 安装

准备 Python 3.10+、Claude Code 2.1.284、自己的 Claude 登录、一个支持 JSON 输出的 OpenAI 兼容审查模型服务，以及实现 [召回接口](INTERFACE.md) 的服务。分类模型独立于 Claude 订阅账号，会按其服务商规则计费。审查模型的服务地址须为公网 HTTPS、443 端口；支持 `chat/completions` JSON 模式。安装器不会安装 Claude、接触现有数据库或覆盖已有目录。

```sh
python3 install.py --prefix "$HOME/portable-review"
cd "$HOME/portable-review"
cp config.example.json config.json
```

`config.json` 里 `claude_command` 应指向自己的 Claude 命令。设置以下宿主进程环境变量（不要把密钥写入共享文件）：

```sh
export PORTABLE_REVIEW_MODEL_BASE_URL='https://你的模型服务地址'
export PORTABLE_REVIEW_MODEL_NAME='你的审查模型名'
export PORTABLE_REVIEW_MODEL_API_KEY='你的审查模型密钥'
export PORTABLE_RECALL_URL='http://127.0.0.1:18889/recall'
# 如模型服务支持关闭思考，可选：export PORTABLE_REVIEW_THINKING=disabled
# 如召回服务需要 Bearer token，再设置 PORTABLE_RECALL_TOKEN
```

召回 URL 必须实际指向你自己的适配器；公网地址须 HTTPS，本机可用 loopback HTTP。审查把当前用户输入、完整草稿及预算内最近上下文发给所选模型服务；需要历史时也会把查询发给召回适配器。请只使用你信任的服务。密钥环境变量会从 Claude 子进程移除。

本包另附 `example_recall.py`：单用户、只监听本机的 JSONL 字面查找示例，说明如何实现接口；它不是通用记忆库，也不是语义召回。可先建立自己的记录文件、运行示例，再把 `search()` 换成自己记忆系统的检索。详见接口说明。示例不附任何私人记忆。

## 使用

把用户输入写入 UTF-8 文本，然后在已配置好上述环境变量的 shell 中运行：

```sh
python3 review_host.py --state ./state run --config ./config.json \
  --session my-conversation --prompt-file ./message.txt --cwd ./readable-project
```

stdout 在终稿通过绑定检查后返回 JSON，其中 `content` 是完整回复，`review` 是审查结果。分类或召回不可用不等于通过：会保留绑定原稿并标记不可用。无匹配与不可用分开；无匹配不代表事实从未发生。整轮超时会结束本轮进程，失败轮次不自动重放。`state/` 含原文和回执，是私人数据；保护它，别打包分享。

可选冰箱贴从空白开始：

```sh
python3 review_host.py --state ./state notes
python3 review_host.py --state ./state add-note --trigger '示例触发词' --note-file ./note.txt
python3 review_host.py --state ./state edit-note --id RULE_ID --version 1 --note-file ./note.txt
```

纸条正文原样保存（按规则清理首尾空白）；编辑需提供当前版本，冲突拒绝。最多两条命中纸条可以随唯一一次续答送达。

## 已验证和边界

隔离测试中真实 Claude Code 跑过五轮，模型审查端使用合成替身；独立审查模型用合成草稿做过真实调用。这还不是接收者真实账号、模型供应商与记忆库的整链验收。请先在自己的隔离环境接线验证，再用于真实聊天。审查不是事实正确的保证，召回来源的授权、准确性和作用范围由接收者的适配器负责。保留旧安装，回退时切换程序入口；不要拿旧状态库覆盖新消息。

可选的本轮预取、精确复用和隐私边界见 [PREFETCH.md](PREFETCH.md)。Claude 子进程使用不持久化 native 会话；审查材料仍会发送给所选模型服务。
