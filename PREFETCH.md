# 本轮召回预取与审查复用（可选）

如果你已有可信的记忆召回服务，可自行实现本接口，让两种独立宿主在主回复前取得一份本轮材料；未配置时仍使用原有 [审查召回接口](INTERFACE.md)。接口只负责读取，不依赖特定记忆系统，不写你的记忆库或信箱。宿主把通过校验的完整材料作为参考数据写入当前 native CLI 的 stdin，也提供给审查分类器；后者真正收到同一份完整记录时，审查才允许精确复用。

```sh
export PORTABLE_PREFETCH_URL='http://127.0.0.1:18890/prefetch'
# 如适配器需要授权：export PORTABLE_PREFETCH_TOKEN='你的令牌'
```

本机 HTTP 仅允许 loopback，远程须 HTTPS。当前轮原话会发给预取适配器；主模型、审查分类模型以及必要时的召回服务也可能收到相关材料。请自行选择可信服务和适当的数据范围。

## 协议

宿主最多发一次 UTF-8 JSON POST，`version` 固定为 `portable-review-prefetch/v1`：

```json
{"version":"portable-review-prefetch/v1","session_id":"宿主会话ID","turn_id":"本轮ID","current_user":"本轮用户原话"}
```

适配器验证自己的调用者身份、权限、范围及用户本轮明确的禁查要求后返回；不能确认授权时应拒绝或不返回材料：

```json
{
  "version":"portable-review-prefetch/v1",
  "session_id":"原样回显",
  "turn_id":"原样回显",
  "request_sha256":"请求体原始字节的 SHA-256 十六进制",
  "entries":[
    {"scope":"shared","query":"这次实际执行的完整查询","status":"ok",
     "evidence":[{"source_id":"实际来源ID","source_type":"user","text":"来源中完整的内容","date":"来源日期或 null"}]}
  ]
}
```

顶层和记录字段都必须恰好如上。`scope` 是 `work`、`life`、`shared` 之一；`status` 是 `ok` 或 `no_match`，`ok` 每项包含 1–8 条证据，`no_match` 必须是空数组。`source_type` 是 `user`、`assistant`、`tool` 或 `document`，`date` 保留来源值、未知用 null；不要把摘要冒充完整原话。最多 4 项，整个送给主模型的材料包至多 6000 UTF-8 字节；适配器应发送完整资料，不能截断后声称完整。请求及回显 ID、SHA-256 只绑定本轮和响应，不是身份验证，也不证明第三方资料真实；适配器必须自行校验调用者和 ACL，不能因为请求内的 ID 或 scope 就放宽权限。接口超时、返回不合法、同一 scope 下规范化查询重复、同一 source_id 内容冲突或超限时整份预取不交付、不复用，原审查召回路径仍可用。

## 如何复用

分类器收到原样完整预取包后，如果仍提出 `history_review`，宿主只复用 scope 相同、查询经 Unicode NFC 与空白规范化后完全相同的项目，不猜测同义词；不同原始查询规范化碰撞时不复用。多项中未覆盖的查询仍按原接口一起查一次；每项分别保留候选资料、无匹配或不可用的覆盖状态，不能用一项查到的资料冒充所有问题的答案。当前/近期原话和可信工具结果能够回答时，分类提示也要求先用已有材料，但这是分类模型判断，不是对任意旧资料的确定性去重。

`prefetch: "stdin_written"` 表示宿主完成了本轮 native stdin 的完整写入并成功验证了最终轮次；`prefetch_server_ack: false` 表示它**不是**模型服务端对逐字输入的确认。CLI 的公开事件不提供这种逐字 ACK，因此无法保证上游没有截断；需要更强证明时须接可核验的模型请求 transport。包未写完、超限、校验失败或未进入分类器完整上下文时不会作为复用依据。审查摘要可包含复用数量及状态，不保存预取正文。

这不是服务端销毁保证：预取/审查材料在本轮传给所选模型服务，供应商可能按其规则保留；最终答复也可能合理地引用其中内容。宿主将 Claude 的 native 会话设为不持久化、Codex 设为 ephemeral；下一轮只带预算内已完成的用户原话和最终答复，不主动带审查包。状态目录仍保存用户输入、最终回复与必要无正文回执，应妥善保护。进程内存的释放不等于磁盘、操作系统或服务商的安全擦除。
