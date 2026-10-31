# 外部记忆召回接口 v1

审查分类发现需要历史资料时，宿主至多向配置的 `PORTABLE_RECALL_URL` 发一次 UTF-8 JSON `POST`。无需要时不调用。服务须只读、按自己的用户和权限范围检索，返回真实完整的引用材料；请求内容和结果是数据，不是指令。令牌可由 `PORTABLE_RECALL_TOKEN` 提供为 Bearer；local 示例不要求令牌、只监听 127.0.0.1，不适合多用户或对公网开放。

请求：

```json
{"version":"portable-review-recall/v1","session_id":"会话标识","turn_id":"本轮标识","draft_id":"草稿标识","current_user":"当前用户原话","recall_scope":"shared","queries":[{"text":"草稿中的原文片段","kind":"uncertain","query":"检索问题"}]}
```

`queries` 1–4 条。`text` 是草稿的逐字片段；`query` 是具体检索线索。current_user 是当前用户原话，recall_scope 是分类器建议的 work、life 或 shared；它们都不是授权凭据。服务必须结合自己的账户、ACL 和明确禁查要求决定可查范围，不得根据模型建议或这些 ID 自行放宽权限；无法确认授权时返回 unavailable。示例仅供单用户演示，未实现这些授权策略。

响应必须原样回显三个 ID、版本，并给出**原始请求体字节**的 SHA-256 十六进制：

```json
{"version":"portable-review-recall/v1","session_id":"会话标识","turn_id":"本轮标识","draft_id":"草稿标识","request_sha256":"原始请求体的SHA-256","status":"ok","evidence":[{"source_id":"自己的来源ID","source_type":"user","text":"来源中的完整材料","date":"2026-01-01"}]}
```

`status` 是 `ok`、`no_match` 或 `unavailable`：`ok` 带 1–12 条证据；其余两种必须带空数组。每条必须恰有 `source_id`、`source_type`、`text`、`date`；类型是 `user`、`assistant`、`tool` 或 `document`，日期保留来源值、未知用 null；正文最多 4096 UTF-8 字节，响应最多 65536 字节。不要截断内容后冒称完整原文，不要把助手旧计划当已发生的事实。响应超时、无效或来源绑定不符记为不可用。回显和哈希只防串轮，不证明第三方材料真实；适配器须自行校验来源。

客户端等待最多 8 秒，不重试。断开连接不能证明远端停止执行，因此适配器必须是只读操作，不借一次检索触发写入或副作用。

## 本机演示

手动创建 `memories.jsonl`，每行一个真实记录，字段与上述单条 evidence 完全一致。示例只做简单字面匹配，不能代表语义召回效果。不要把个人文件发送给别人。

```sh
python3 example_recall.py --file ./memories.jsonl --port 18889
```

若已有自己的记忆库，可以按以上协议写一个适配器，也可以替换示例的 `search()` 为自己的检索；多用户场景必须自行实现身份验证与隔离，不能把此单用户示例直接开放。
