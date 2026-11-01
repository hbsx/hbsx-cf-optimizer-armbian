# FlClouds 同机器人 DNS 接入

将 `cfDnsBridge.ts` 放进 FlClouds 的 `backend/src/services/`，在项目根应用 `existing-bot.patch`。补丁只添加消息分流、原中文菜单后的四条命令、帮助文字和专用 HTTPS 路由，不替换下载或上传处理。补丁基于部署时的 FlClouds 源码；其他版本应根据补丁逐项接入并检查上下文。

启动时同时为指定管理员注册私聊完整菜单（原指令加 DNS 指令），包含默认语言与中文，避免旧的语言菜单或客户端缓存遮住新增命令；不更改其他用户的私聊菜单。菜单注册只发送设置请求，不启动额外的 Telegram 接收器。

FlClouds 后端环境变量（Docker Compose 的 backend.environment 也要传入）：

```text
CF_DNS_BRIDGE_ENABLED=1
CF_DNS_BRIDGE_KEY=独立生成的64位小写十六进制密钥
CF_DNS_OWNER_ID=个人Telegram数字ID
```

用 `python3 -c 'import secrets; print(secrets.token_hex(32))'` 生成密钥，两端相同。不要提交真实密钥、Bot Token 或设备配置。复用原 HTTPS 入口，将 `/api/cf-dns-bridge` 交给后端；不要放宽其他路由的身份验证。

N1 `/opt/cf-optimizer/env.sh`：

```bash
CF_DNS_BRIDGE_ENABLED=1
CF_DNS_BRIDGE_URL="https://你的FlClouds API域名/api/cf-dns-bridge"
CF_DNS_BRIDGE_KEY="与后端相同的密钥"
CF_TG_CONTROL_ENABLED=0
CF_TG_ALLOWED_USER_ID="同一个个人Telegram数字ID"
CF_TG_DIRECT_INTERFACE="eth0"
```

还需原有的 Cloudflare Zone/API Token、精确 DNS 名称、个人 Chat ID、控制网络代理及真实节点 Host/Path/Port 配置。重新安装优化器后会启用 outbound worker；手动检查连接：

```bash
sudo runuser -u cfoptimizer -- bash /opt/cf-optimizer/cf_dns_bridge.sh --check
```

DNS 更新在 N1 绑定物理网卡直连验证两次 NRT 与真实节点 TLS/WS，成功后以单个 Cloudflare 批量事务切换 A/AAAA。只允许指定域名与官方 Cloudflare 数字 IP。原机器人的指定管理员私聊可用 `/dns` 查询，或发送 IP 切换；`/use4`、`/use6` 使用内存中的优选第一名。完整扫描占用锁时拒绝更新，稍后重发。

接口仅支持鉴权后的取任务、回传结果和连接状态，没有 HTTP 创建任务或任意命令入口。单任务内存队列 120 秒过期，任务只投递一次，结果可幂等回传；结果不确定时用 `/dns` 查询，不自动重试 DNS 写入。FlClouds 重启会丢弃待处理任务。

测试：把 `cfDnsBridge.test.ts` 放在同一 services 目录，在有开发依赖的 FlClouds 后端执行 `node --import tsx src/services/cfDnsBridge.test.ts`。N1 worker 测试包含在项目的 Python unittest 中。

三天一次的 IPv4 优选、手动 IPv6 优选及健康检查关闭状态均不受此模块影响。N1 不保存指令、报告、任务或结果历史。
