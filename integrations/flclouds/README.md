# FlClouds 同机器人 DNS 接入

保留原机器人的上传、下载及其他功能，DNS 菜单提供六项：

| 指令 | 功能 |
| --- | --- |
| `/dns` | 同时查看 CF、FD 443、FD 8443 三个域名当前的 A/AAAA IP |
| `/usecf` | 使用 N1 最近 CF IPv4 优选第一名 |
| `/fd443 [IP或IP:443]` | 不带 IP 使用 FD 443 第一名；带 IP 手动更新 tj 对应域名 |
| `/fd8443 [IP或IP:8443]` | 不带 IP 使用 FD 8443 第一名；带 IP 手动更新 nj 对应域名 |
| `/use6` | 使用 N1 最近 CF IPv6 优选第一名 |
| `/fdscan` | 手动启动 N1 FD 优选，完成后走原机器人推送 |

**所有测速、地区筛选和节点验证只在 N1 执行。甲骨文不测速，只提供备用 Cloudflare DNS API 线路。**

默认由 N1 处理。N1 离线、8 秒内未领取任务，或明确报告 DNS 请求在写入前失败时，自动使用甲骨文。N1 的节点验证失败、扫描锁占用或结果缺失时，不绕过验证。任务已领取但更新结果不明时，先查询当前 DNS，不重复提交未知结果的更新。任务最长 120 秒，N1 在写入前检查有效期。

N1 worker 每约 45–60 秒把已经测出的 CF4、CF6、FD 443、FD 8443 第一名及原始结果时间同步到甲骨文进程内存，最多四个候选，不同步节点域名、WS 路径、凭据、完整候选池或测速报告。结果最多有效 96 小时，拒绝异常未来时间。CF 地址受官方网段限制；两个 FD 端口分别使用独立的甲骨文允许列表，快照不能扩大授权。没有缓存就拒绝切换，不伪造第一名，不在甲骨文重新测速。甲骨文重启清空缓存，待 N1 在线重新同步。

备用回复注明线路及 N1 结果时间。备用更新不保证家庭宽带此刻仍可用。DNS 无法修改客户端端口，FD 端口须保持一致。

`fd443`、`fd8443`、`fdscan` 支持不带斜杠；`/usefd` 仍兼容 443 更新。手动优选只由 N1 执行，离线时不会交给甲骨文测速或伪造启动成功。启动结果不明时不重复提交。

自定义示例：`/fd8443 82.110.234.167:8443`，或 `/fd443 39.105.24.90`。只接受公网 IPv4；带端口时必须与命令一致。443、8443 分别绑定既有域名，DNS 不会保存端口。裸 IP 仍按 CF 处理，不能用于指定 FD。

**自定义更新不要求进入优选池，也不扩大任何长期允许列表。** N1 在线时，在同一运行锁内完成两轮公开 CF/TLS 检查，要求亚太机房且两轮机房一致，再更换 DNS；不向新地址发送原节点 Host、WS 路径或凭据，不验证实际节点或下载速度，回复明确标注。公开验证失败时保留 DNS，不绕到甲骨文强行更新。甲骨文备用仅将本条管理员明确指定的公网 IPv4 写入对应 DNS，不进行测速或节点验证，并在回复中说明。自定义地址不写入候选池、不污染第一名缓存，不保留指令历史；后续自动优选仍可能按原规则替换它。

旧 `/use4`、`/usetj`、`/rescue`、`/rescuetj` 隐藏兼容，不再强制选择线路。裸 IP 和 `/set IP` 仍指定 CF 地址；`/settj IPv4` 仍指定已授权 FD 地址，均自动选择线路。按地区、名次的旧 `/usetj` 仅在 N1 在线时支持，不显示在简化菜单中。

## 接入配置

将 `cfDnsBridge.ts`、`cfDnsUnified.ts`、`cfDnsRescue.ts`、`cfProxyRescue.ts`、`cfFdCommand.ts` 放入 FlClouds 的 `backend/src/services/`，应用 `existing-bot.patch`。其他版本须检查补丁上下文；已经接入的版本更新模块和帮助文字，不重复应用补丁。

FlClouds 私有环境和 Compose backend.environment 传入：

```text
CF_DNS_BRIDGE_ENABLED=1
CF_DNS_BRIDGE_KEY=两端相同的独立64位小写十六进制密钥
CF_DNS_OWNER_ID=个人Telegram数字ID
CF_DNS_RESCUE_ENABLED=1
CF_DNS_RESCUE_NAME=官方优选精确域名
CF_DNS_RESCUE_ZONE_ID=CloudflareZoneID
CF_DNS_RESCUE_TOKEN=该Zone的DNS编辑令牌
CF_PROXY_RESCUE_ENABLED=1
CF_PROXY_RESCUE_NAME=独立反代精确域名
CF_PROXY_RESCUE_PORT=443
CF_PROXY_RESCUE_IPS=明确授权的同端口IPv4列表（逗号分隔）
CF_PROXY_RESCUE_8443_NAME=独立的8443反代精确域名
CF_PROXY_RESCUE_8443_IPS=明确授权的8443端口IPv4列表（逗号分隔）
```

三个域名须彼此不同。仅修改合法灰云 A/AAAA，保留 TTL、备注等属性；遇到 CNAME、橙云或期间 DNS 变化时拒绝覆盖。目标还没有地址记录时可创建灰云 A。单次批量写入后读回确认，不重试结果不明的更新。不要提交真实令牌或设备配置。

N1 私有 `env.sh` 保留原测速、节点验证及 DNS 配置，并设置：

```text
CF_DNS_BRIDGE_ENABLED=1
CF_DNS_BRIDGE_URL=https://你的FlCloudsAPI域名/api/cf-dns-bridge
CF_DNS_BRIDGE_KEY=与后端相同的共享密钥
CF_TG_CONTROL_ENABLED=0
CF_TG_ALLOWED_USER_ID=同一个个人Telegram数字ID
CF_TG_DIRECT_INTERFACE=eth0
CF_PROXY_DNS_NAME=独立反代精确域名
CF_PROXY_DNS_PORT=443
CF_PROXY_DNS_443_NAME=443对应域名（可省略并回退原CF_PROXY_DNS_NAME）
CF_PROXY_DNS_8443_NAME=独立的8443对应域名
```

只有原机器人接收 TG 消息，不新增接收器。只接受指定管理员认证后的新私聊消息，不处理转发、群聊、旧消息或向导中的裸 IP/裸命令。默认语言与中文菜单均保留原功能，并提供上述六条 DNS/优选指令。

升级 N1 安装器会安装固定动作的 `cf-optimizer-fd-request.path/service`。worker 只在 `/run/cf-optimizer-bridge` 创建空请求，派发器删除后启动唯一指定的 FD 服务；不读取命令文本，不赋予 worker root 权限或额外 capabilities。请求、状态和报告均在 RAM，沿用既有运行锁、直连测速和代理 TG 推送。

HTTP 接口以共享密钥认证，只支持取任务、结果回传、状态查询和候选 RAM 同步，不允许通过 HTTP 创建 DNS 更新或任意命令。队列、缓存和去重仅在内存，N1 不保存命令、日志或结果历史。测速直连，控制网络沿用代理；原三天一次扫描等定时任务不变。

## 测试

在有开发依赖的后端执行：

```sh
node --import tsx src/services/cfDnsBridge.test.ts
node --import tsx src/services/cfDnsUnified.test.ts
node --import tsx src/services/cfDnsRescue.test.ts
node --import tsx src/services/cfProxyRescue.test.ts
node --import tsx src/services/cfFdPorts.test.ts
```

N1 worker 测试在 `python -m unittest discover -s tests` 中，使用模拟客户端，不修改实际 DNS。
