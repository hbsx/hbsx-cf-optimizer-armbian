# FlClouds 同机器人 DNS 接入

保留原机器人的上传、下载及其他功能，DNS 菜单提供七项：

| 指令 | 功能 |
| --- | --- |
| `/dns` | 同时查看 CF、FD 443、FD 8443 三个域名当前的 A/AAAA IP |
| `/usecf` | 使用 N1 最近 CF IPv4 优选第一名 |
| `/fd443 [IP或IP:443]` | 不带 IP 使用 FD 443 第一名；带 IP 手动更新 tj 对应域名 |
| `/fd8443 [IP或IP:8443]` | 不带 IP 使用 FD 8443 第一名；带 IP 手动更新 nj 对应域名 |
| `/fdcheck` / `/fdcheck IP:端口` | 无参数读取 tj/nj 当前解析并检测实际 IP:443 / IP:8443；带参数检查指定公网 IPv4 的 TLS 端口，不改 DNS |
| `/use6` | 使用 N1 最近 CF IPv6 优选第一名 |
| `/fdscan` | 手动启动 N1 FD 优选，完成后走原机器人推送 |

**优选、下载测速和实际节点验证只在 N1 执行。甲骨文不参加优选或下载测速；手动 FD 更新时会执行轻量公开 CF/TLS 端口检查，再提供备用 Cloudflare DNS API 线路。**

默认由 N1 处理。N1 离线、8 秒内未领取任务，或明确报告 DNS 请求在写入前失败时，自动使用甲骨文。N1 的节点验证失败、扫描锁占用或结果缺失时，不绕过验证。任务已领取但更新结果不明时，先查询当前 DNS，不重复提交未知结果的更新。任务最长 120 秒，N1 在写入前检查有效期。

N1 worker 每约 45–60 秒把已经测出的 CF4、CF6、FD 443、FD 8443 第一名及原始结果时间同步到甲骨文进程内存，最多四个候选，不同步节点域名、WS 路径、凭据、完整候选池或测速报告。结果最多有效 96 小时，拒绝异常未来时间。CF 地址受官方网段限制；两个 FD 端口分别使用独立的甲骨文允许列表，快照不能扩大授权。没有缓存就拒绝切换，不伪造第一名，不在甲骨文重新测速。甲骨文重启清空缓存，待 N1 在线重新同步。

备用回复注明线路及 N1 结果时间。备用更新不保证家庭宽带此刻仍可用。DNS 无法修改客户端端口，FD 端口须保持一致。

`fd443`、`fd8443`、`fdscan` 支持不带斜杠；`/usefd` 仍兼容 443 更新。手动优选只由 N1 执行，离线时不会交给甲骨文测速或伪造启动成功。启动结果不明时不重复提交。

N1 收到不带 IP 的 `/fd443`、`/fd8443` 时，按近期同端口测速排名逐个验证，首选失败后继续检查下一条，选用第一个两轮公开 CF 和实际节点检查均通过的地址。最多检查 8 条并限制总时间；整个过程持有同一运行锁，候选之间也核对 DNS，只有一次最终更新。仅端点验证失败允许继续；限流、配置/API 错误、DNS 变化、时间不足和写入结果不明均停止，禁止重放写入。失败地址仅在 N1 worker 进程内存中跳过 15 分钟，新扫描或重启后重置；不改静态候选池、不生成失败历史。同步给甲骨文的同端口第一名也会跳过这些地址。明确指定 IP 或地区/名次时不会擅自换成别的 IP，全部候选失败则提示 `/fdscan` 刷新。

自定义示例：`/fd8443 82.110.234.167:8443`，或 `/fd443 39.105.24.90`。只接受公网 IPv4；带端口时必须与命令一致。443、8443 分别绑定既有域名，DNS 不会保存端口。裸 IP 仍按 CF 处理，不能用于指定 FD。

**自定义更新不要求进入优选池，也不扩大任何长期允许列表。** N1 在线时，在同一运行锁内完成两轮公开 CF/TLS 检查，要求亚太机房且两轮机房一致，再更换 DNS；不向新地址发送原节点 Host、WS 路径或凭据，不验证实际节点或下载速度，回复明确标注。公开验证失败时保留 DNS，不绕到甲骨文强行更新。甲骨文备用在手动更新前，也须两轮指定 IP:端口的公开 CF/TLS 检查通过，才将本条管理员明确指定的公网 IPv4 写入对应 DNS。甲骨文仅观测当地网络，不按自己的 CF 机房判断家庭落地地区，回复注明不能代表家中线路，且不做下载测速或实际节点验证。自定义地址不写入候选池、不污染第一名缓存，不保留指令历史；后续自动优选仍可能按原规则替换它。

指定端口检测失败时，可额外检查另一个端口一次，并提示正确命令和对应域名；不会自动改用另一个端口或修改另一域名。HTTP 429 会停止，不再探测另一个端口。`/fdcheck` 与更新使用相同公开检查标准，但只返回检查结果，不访问 DNS 写入接口。N1 使用物理网卡直连，甲骨文检查由甲骨文本机发起；均只请求固定的 `speed.cloudflare.com/cdn-cgi/trace`，验证证书及 Host 响应，限制超时和响应大小，不跟随重定向，不退回明文。端口连通不等于实际节点一定可用，也无法识别一个同样可用但并非用户本意的 IP。

无参数 `/fdcheck` 的检测目标来自两个 FD 域名的当前 Cloudflare A 记录，而非第一名缓存；因此重启后没有优选缓存时也能检测现有解析。每个域名最多检测 4 个不同公网 IPv4，两轮公开 CF/TLS 检查，遇到未配置、无 A 记录、IPv6 或查询失败明确报告，禁止探测内网/保留地址。`/fdcheck IP:端口` 可检测 4443、8888、9999、30002 等合法 TLS 端口；仍拒绝 80、8080 等已禁用的明文端口，原 `/fdcheck IP` 保留检查 443/8443 的兼容行为。诊断在时间预算内执行，不更改 DNS/候选池、不发送原节点 Host/WS 路径或凭据、不保存报告。N1 优先，未连接时可由甲骨文执行同样的只读查询与检测，明确注明观测线路。

旧 `/use4`、`/usetj`、`/rescue`、`/rescuetj` 隐藏兼容，不再强制选择线路。裸 IP 和 `/set IP` 仍指定 CF 地址；`/settj IPv4` 仍指定已授权 FD 地址，均自动选择线路。按地区、名次的旧 `/usetj` 仅在 N1 在线时支持，不显示在简化菜单中。

## 接入配置

将 `cfDnsBridge.ts`、`cfDnsUnified.ts`、`cfDnsRescue.ts`、`cfProxyRescue.ts`、`cfFdCommand.ts`、`cfFdCheck.ts` 放入 FlClouds 的 `backend/src/services/`，应用 `existing-bot.patch`。其他版本须检查补丁上下文；已经接入的版本更新模块和帮助文字，不重复应用补丁。

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

两个 FD 端口的独立备用允许列表各最多 256 个 IPv4，与本地正式候选文件的上限一致；这只是名单容量，不扩大每轮最多 30 个实际节点验证的运行预算。只有明确配置的 IP 可接受为同步候选，公开来源和快照不能扩大允许列表。其他 TLS 端口的合格结果保留为 IP:端口，需在客户端手动配置对应端口；现有 tj/nj 自动更新仍分别绑定 443/8443。

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

只有原机器人接收 TG 消息，不新增接收器。只接受指定管理员认证后的新私聊消息，不处理转发、群聊、旧消息或向导中的裸 IP/裸命令。默认语言与中文菜单均保留原功能，并提供上述七条 DNS/优选指令。

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
node --import tsx src/services/cfFdCheck.test.ts
```

N1 worker 测试在 `python -m unittest discover -s tests` 中，使用模拟客户端，不修改实际 DNS。
