# Codex 独立审查宿主（候选）

这是一个单独启动的命令行宿主，不是原生 Codex TUI 的通用插件。它使用 Codex CLI 0.159.2 的公开 JSON 事件及 Stop/PreToolUse/PostToolUse Hook，复用本包的审查和冰箱贴逻辑，不依赖特定记忆系统，也不包含信箱或记忆写入。它把子进程的全部公开输出留在宿主内，绑定本轮输入、native session、Stop 草稿、终稿事件，确认后才向外返回完整 JSON。遇到具体问题最多续答一次；KEEP 选择由宿主恢复原稿。审查不可用会标记不可用并保留绑定原稿，不当作通过。Codex 原生 JSON 流本身会先输出原稿，不能把这个宿主删掉后声称发送前拦截仍成立。

## 接线

先按主 README 配置审查模型和自己的 [召回接口](INTERFACE.md)。准备 Linux（内核和 Python 支持 pidfd_open / pidfd_send_signal）、Python 3.10+、Git、已登录并支持模型的 Codex CLI 0.159.2，以及可用的 `auth.json`。没有移动或写入原始记忆库；召回的身份、权限与数据准确性由使用者自己的适配器负责。复制示例并编辑其中模型、`codex_auth_file` 和可选只读目录；不需要文件读取时，删除 `read_root` 这一行（同时保持 JSON 合法）。只选愿意给 Codex 读取、也愿意发给审查模型的目录。

```sh
cp config.codex.example.json config.codex.json
# 编辑 config.codex.json 中的模型、凭据文件及可选只读目录
export PORTABLE_REVIEW_MODEL_BASE_URL='https://你的模型服务地址'
export PORTABLE_REVIEW_MODEL_NAME='你的审查模型名'
export PORTABLE_REVIEW_MODEL_API_KEY='你的审查模型密钥'
export PORTABLE_RECALL_URL='http://127.0.0.1:18889/recall'
# 如有需要再设置 PORTABLE_RECALL_TOKEN
install -d -m 700 "$HOME/.local/state/portable-review-codex"
printf '%s' '在这里写本轮消息' > message.txt
python3 codex_review_host.py --state "$HOME/.local/state/portable-review-codex" \
  run --config ./config.codex.json --session my-conversation --prompt-file ./message.txt
```

stdout 的 `content` 是完整最终回复，`review` 含审查状态；报错写 stderr，失败轮不自动重放。状态目录要求属于当前用户且无组/其他人权限，并须位于 Git 工作树外。它含私人聊天和回执，不要分享、备份给不可信的人或用旧状态覆盖新对话。宿主每轮使用独立临时 Codex home 和空工作目录；Codex 运行期间该 home 可能含审查文本、SQLite 和 WAL，必须按私人资料保护。正常结束后删除整轮私有目录；宿主异常死亡时看门进程按 PID 和启动代次停止已登记的子进程组并清理，下一次启动也会恢复有归属标记的未完成清理。如果宿主与看门进程同时异常退出，资料可能保留到下次启动；清理中断也由标记支持恢复。这不是对磁盘、备份或操作系统缓存的安全擦除保证。没有归属标记的旧目录不会被自动删除。

认证使用私有符号链接指向原 `auth.json`；删除整轮目录只删除链接，不删除其目标。Codex 自己的登录刷新仍可能按其实现改动凭据；凭据和状态应由使用者保护。只支持文件认证配置，其他认证存储方式尚未验证。看门进程记录登记进程组中观察到的成员与启动代次，通过 Linux pidfd 停止已证明的组成员，确认内核中整组不存在后才删除；组长退出后若无法证明组身份或有成员未退出，保留目录并报错，后续启动再试。不能保证停止自行脱离该组的外部进程。

纸条与 Claude 宿主共用数据结构，但可以选不同状态目录：

```sh
python3 codex_review_host.py --state "$HOME/.local/state/portable-review-codex" notes
python3 codex_review_host.py --state "$HOME/.local/state/portable-review-codex" \
  add-note --trigger '触发词' --note-file ./note.txt
```

`edit-note --id ID --version 版本 --note-file 文件` 可修改。纸条命中后的行为与本包既有规则相同。

## 只读工具及限制

配置 `read_root` 后，模型可在审查前使用专用 MCP 的 `read`、`list`、`search`，分别读取一个 UTF-8 普通文件、列出一级目录、对单个文件作字面搜索。路径只能相对该根目录，逐段拒绝符号链接和 `..`；文件及搜索结果上限 4096 字节、每目录最多 100 名称、本轮最多 24 次。文件读期间发生变化时拒绝；超限、越界和读取失败返回明确 `READ_REFUSED`。根目录以外的文件、二进制文件、递归/语义搜索都不支持。这是受控工具的单用户示例，不是完整文件沙箱或多用户授权系统。

只读 MCP 在宿主内再次检查本轮登记和审查封条；PostToolUse 将匹配的实际结果作为本轮工具来源传给审查，未匹配结果使轮次失败。其他受 Codex PreToolUse 覆盖的工具调用默认拒绝，审查开始后也拒绝只读 MCP。Codex 官方说明部分工具路径不经过 hook，且 hook 超时、错误等可能放行，因此不要把 Hook 描述成绝对封条。宿主关闭 web search，使用 read-only 模式，并让专用只读 MCP 的只读标记适用 `writes` 审批策略；其他工具的安全仍依赖实际 Codex 和运行环境，未宣称覆盖所有外部路径或阻止全部副作用。`--dangerously-bypass-hook-trust` 只用于此独立宿主生成的临时 hook；请勿将它套在不受信任的项目或全局 hook 上。系统/组织强制配置可能影响或拒绝运行，不应绕过。

每轮只带最多最近两轮已完成消息、当前用户消息及本轮受控工具结果；不会读取完整历史。审查模型收到草稿和上下文，召回服务在必要时收到查询，请使用可信服务并遵循 [接口](INTERFACE.md) 的身份和来源要求。

## 本机验证及尚未覆盖

本机以真实 Codex CLI 0.159.2 和独立账户链路生成回复，审查服务、召回与测试文件均为合成数据：直接通过；历史召回后改稿；KEEP 还原；审查 503 保留原稿并标不可用；召回无匹配；纸条命中后续答；超时失败后可继续下一轮。真实 Codex 读取文件及越界软链接拒绝、工具结果进入审查、审查期间只读入口拒绝和失败时不输出草稿均已测。宿主 SIGKILL 后，本轮链接/路由清除、同代 Codex 终止、原认证文件保留。

尚未验证群友账号、审查供应商和真实记忆库的完整链路，也未覆盖所有 Codex 版本、操作系统、认证方式、原生 TUI 或不受 Hook 覆盖的工具。新版本应先在隔离环境验证；不要用于多用户或未经授权的数据。出现失败检查状态并新发一轮，不自动重放先前请求。

可选的本轮预取与精确复用见 [PREFETCH.md](PREFETCH.md)；宿主 stdin 写入不等于模型服务端逐字确认。
