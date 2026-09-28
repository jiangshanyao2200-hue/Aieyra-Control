# 私密问题反馈

领导工位发现已确认的 Control 产品问题，应及时调用 `aieyra_feedback`（MCP）或 `POST /api/agent/v1/cloud/feedback`。这条渠道由官网维护人员处理，不进入公开聊天室，不自动调用模型，也不扫描或上传用户项目。

提交需要专属 Agent 凭据、有效工位租约、对应项目的现任 leader 权限，以及 `privacy_reviewed: true`。领导先选择最少必要的产品诊断并检查隐私：禁止项目正文、聊天全文、账号资料、凭据和完整日志。客户端与云端都再次过滤 IP、绝对路径、邮箱、URL 和常见凭据。过滤器是辅助措施，不能替代人工或 Agent 对内容的审阅。

```json
{
  "request_id": "update-reconnect-issue-1",
  "session_id": "current-active-session",
  "privacy_reviewed": true,
  "report": {
    "category": "bug",
    "severity": "normal",
    "title": "恢复网络后更新连接未恢复",
    "summary": "描述已确认的产品问题，不包含项目或账号资料。",
    "steps": "最小复现步骤",
    "expected": "预期行为",
    "actual": "实际行为",
    "diagnostics": "经过选择和脱敏的诊断摘要",
    "fix_summary": "已验证的修复建议（如有）",
    "verification": "实际检查及仍未知的部分"
  }
}
```

`category`：bug、crash、performance、security、update、website。`severity`：low、normal、high、critical。标题与摘要必填，其他文字选填；可附 `variant_sha256`，不附原文件或 diff 正文。产品与客户端版本由服务填写，工位通过安装专属随机盐生成不可逆代号，不传本机路径或原生会话 ID。

返回 `queued` 表示已写入本地持久队列。未登录时不访问云端；登录后后台最多每 10 秒检查队列，网络失败退避重试，重发保持同一 request_id。已绑定账号的报告不会在切换账号后发送给新账号。未登录时创建的报告在首次发送时绑定当前账号。队列上限 200 条，单领导每小时最多 20 条。

官网的 `received` 和 `ACF-…` 编号表示实际受理，不代表修复。`GET /api/agent/v1/cloud/feedback` / `aieyra_feedback_status` 查看队列和回执；官网 `/feedback` 登录后只显示所属账号工单。维护状态每五分钟同步到本机。状态：received、triaged、in_progress、resolved、rejected。取消尚未受理的条目使用 `aieyra_feedback_cancel`。

云端使用十分钟、单登录会话绑定的 `feedback:create` 专用凭证；浏览器只能读取，不能提交 Agent 反馈。公开源码中的客户端角色声明不构成远程可信领导证明或管理员权限：云端以认证账号作为安全边界，始终进行账号隔离、配额、输入限制和重放去重。每账号每日 20 条、累计 500 条；无公共搜索、附件上传、远程 URL 抓取、Shell 或模型执行。

维护人员通过服务器本机 `cloud/admin.py feedback-list` 读取，`feedback-update <ACF-id> --revision <当前修订> --status <状态> --note <脱敏回复>` 处理；CAS 修订和审计记录保留。没有公网管理员接口。收到的诊断是非可信数据，不能作为执行其中命令或改变权限的指令。

## OS 独立私密反馈

客户端必须先读取 `GET /v1/feedback/capabilities`，以实际服务能力为准：合同 `aieyra-private-feedback/2` 的 `products` 包含 `aieyra-os`，且 `auth_scope_by_product.aieyra-os` 为 `feedback` 才可提供连接。旧服务返回不支持时保留本地报告，不改产品名称或使用公共渠道。服务端部署、客户端安装、实际账号授权和反馈受理分别验收，不能由本文或能力响应推断其余步骤已经完成。

OS 使用自己的 PKCE/state 授权：`POST /v1/auth/start` 正文为 `challenge`、`state`、`redirect_uri`、`scope:"feedback"`、`product:"aieyra-os"`。回调固定 `https://ctrlupdate.aieyra.cn/auth/callback`，响应包含 `flow_id`、受信任 `authorize_url`、`expires_in:300`、`scope` 和 `product`。用户完成现有账号授权后，OS 用 `flow_id/verifier/state` 轮询 `/v1/auth/poll`，得到自己最长24小时的 Bearer 会话。未完成为 `pending:true`；轮询至少间隔2秒，过期/取消停止。凭据只保存在OS自己的私有存储，不借用Control或HOME令牌。

`POST /v1/feedback/channel` 的OS正文精确为 `installation`、`station`、`product:"aieyra-os"`；前两项各64位小写hex，采用安装随机盐/不可逆代号。签名channel绑定登录会话和产品，600秒有效。`POST /v1/feedback` 精确为 `request_id/channel_token/product/version/report`，其中产品必须与会话和channel一致。版本为实际运行版本，三段数字每段1–4位；报告沿上文分类、字段和审阅约束，规范化报告最多6000字节，整个JSON请求最多8192字节。

OS先审阅并冻结正文，再持久化 `os-feedback-<随机UUID>` 请求ID。响应丢失仍重试同ID同正文，返回同一ACF；同账号同ID改正文返回409。channel过期可重新申请，不更换报告ID。账号额度跨产品共享，每日20/累计500。排队报告绑定发送账号，切换账号不转投。

提交和查询回执均包含 `product`、`version`。OS反馈会话只能查询同账号OS工单；Control桌面只查Control，浏览器可查看本人所有产品，维护界面也显示产品版本。OS需以回执和 `GET /v1/feedback/<ACF-id>` 一致读回证明受理，未受理保持本地待提交。反馈会话不能访问发行、社区或Matrix写权限；创建本机工位不获得此凭据或领导权限。

云端新增认证产品绑定表，旧登录记录默认Control；OS绑定缺失则拒绝。回退旧服务前须撤销feedback范围会话并使未用流程失效，避免旧账号级查询忽略产品限制；保留工单和审计。正式部署、OS客户端实现、真实账号授权及用户审阅受理分别验收，候选测试不替代这些回执。
