# Cloudflare NRT 优选 · Armbian / N1 / x86 Linux

支持 ARM64 Armbian 和 x86 Linux 的 Cloudflare 东京 NRT IP 优选工具。优选 IP 并推送 Telegram，可选在两轮延迟比较达标或确认旧 IP ping 故障后自动更新 Cloudflare DNS。

基于 [huangmlj/cf-optimizer](https://github.com/huangmlj/cf-optimizer) 的流程改写，使用官方 [XIU2/CloudflareSpeedTest](https://github.com/XIU2/CloudflareSpeedTest) 测 TCP 延迟，curl 验证地区和下载速度。第三方测速核心在安装时下载，不包含在仓库内。

## 默认行为

- 默认每 **3 天、北京时间 03:00** 运行一次；关机时错过的任务不补跑。
- 同一周期先运行官方 IPv4 优选，再运行亚太地区反代优选，优先 HKG/NRT/SIN/KIX，各自通过 TG 推送。共用一个定时器并顺序执行，官方扫描失败后仍尝试反代优选。可用 `CF_PROXY_AUTO_ENABLED=0` 单独关闭自动反代优选。
- 六小时健康检查定时任务**默认关闭**。需要时可配置 `CF_HEALTH_ENABLED=1` 后重新安装启用，在北京时间 00:00、06:00、12:00、18:00 检查；也可手动运行 `cf-optimizer --health`。
- 覆盖 Cloudflare 官方全部 IPv4 网段，先逐 `/24` 采样发现 NRT，再完整细扫最多 8 个候选 `/24`（2048 IP）。不是逐个扫描全部约 152 万个地址。
- 地区验证必须返回 `colo=NRT`，下载响应 `CF-RAY` 也必须属于 NRT。
- **测速直连**：独立系统用户临时查询主路由表，绕过本机 Mihomo TUN；不停止代理、不修改其配置。
- **TG 走代理**：明确使用本机 Mihomo 的 `http://127.0.0.1:7890`，可配置。
- 最多复测 20 个下载候选，找到 5 个达标 IP 后结束。默认每个约 5 秒、速度 ≥5 MB/s、TCP 延迟 ≤250ms、TCP 丢包率 ≤0.1。
- 下载接口返回 **429 时立即停止请求**，保留已有 IP，并尝试推送失败原因，不把失败或旧结果当成本次结果。
- DNS 自动更新默认关闭。启用后，正常优选要求同一个新 IP 连续两轮比旧最快 IP 的延迟低至少 **10%**；故障切换则要求旧 IP 连续两轮 ping 不通、新 IP 两轮 ping 和 TCP 均正常。

## eMMC 与临时文件

正常运行时只允许写入 `/run` 内存文件系统，程序和配置所在系统盘被该服务挂载为只读。

- 不保存程序日志；服务输出丢弃。
- 禁用该服务的 swap、崩溃转储和 Python 字节码写入。
- 临时 CSV、响应头、状态和报告都在 RAM，结束即删除。
- 推送前把报告读入进程内存并删除文件，重试结束后也不保留本地副本。
- IPv4 只留下 `/run/cf-optimizer/best-ips.txt`；手动 IPv6 只留下独立的 `best-ips-v6.txt`。各自成功运行时覆盖，重启后清空。
- 手动反代优选独立使用 `/run/cf-optimizer/best-proxy-ips.json`，包含 IP、端口、机房和本轮检查结果；不覆盖官方 IPv4/IPv6 列表，重启后清空。
- 两轮比较结果只在进程内存中；三天周期按配置中的固定日期计算，不写运行日期或历史状态文件。

程序、配置和定时任务仅安装/修改时写系统盘。操作系统自身可能记录少量服务启停信息；本项目不会关闭整台设备的系统日志。

## 安装

支持使用 systemd 的 Armbian、Debian 和 Ubuntu。安装器按 `uname -m` 自动下载对应的官方 Linux 测速核心：

| 架构 | 系统识别值 | 下载版本 |
| --- | --- | --- |
| ARM64 | `aarch64`、`arm64` | `cfst_linux_arm64` |
| x86 64 位 | `x86_64`、`amd64` | `cfst_linux_amd64` |
| x86 32 位 | `i386`、`i486`、`i586`、`i686`、`x86` | `cfst_linux_386` |

安装和升级时会校验已有测速文件的 ELF 架构；架构不匹配时重新下载，并在替换前验证下载文件。配置和定时运行方式在各架构上相同。这里的 x86 支持面向 Linux，不包含 Windows 或不使用 systemd 的 OpenWrt。

已在 N1、Armbian 25.08 / Ubuntu 24.04、Linux 6.1 上验证基本运行、直连路由、代理 TG 推送与清理；x86 分支提供安装器架构选择和 ELF 校验测试，尚未进行 x86 整机实测。需要 Python3、curl、iproute2、iputils-ping、util-linux、systemd；安装器会检查依赖，不自动升级系统。

```bash
git clone https://github.com/hbsx/hbsx-cf-optimizer-armbian.git
cd hbsx-cf-optimizer-armbian
sudo bash install.sh
sudo nano /opt/cf-optimizer/env.sh
```

安装会启用三天周期的定时任务，升级时保留已有 `env.sh` 和凭据，并补齐缺少的新配置项。

`CFST_SCHEDULE_DAYS=3` 表示每三天运行；安装器首次写入 `CFST_SCHEDULE_ANCHOR` 为当天北京时间日期。例如起始日为 2026-10-22，则运行日期为 10-22、10-25、10-28、10-31、11-03，均为凌晨 3 点。安装时若已过凌晨 3 点，等待下一个周期。升级不会重置已有起始日。定时器每天凌晨只检查日期，非运行日直接退出，不扫描或推送。重启和跨月不会改变周期。

安装器默认关闭 `cf-optimizer-health.timer`，升级也会遵循 `CF_HEALTH_ENABLED` 配置。显式启用后才每 6 小时检查 DNS 旧 IP。健康检查与完整优选共用运行锁，完整优选期间会跳过，不会互相清理运行数据。临时数据仍只允许写 `/run`，不保存日志或历史结果。

填写自己的 Telegram 配置，勿上传真实配置：

```bash
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
TELEGRAM_PROXY_URL="http://127.0.0.1:7890"
```

Mihomo 的本地代理端口应允许此用户连接。如果默认网关本身也有代理，需另外确保测速出站为直连。本工具只保证绕过**本机**的 TUN。

## 两轮比较与故障切换

在 `/opt/cf-optimizer/env.sh` 填写自己的域名和凭据，凭据只保存在设备上：

```bash
CF_DNS_ENABLED=1
CF_DNS_NAME="your-host.example.com"
CLOUDFLARE_ZONE_ID="your-zone-id"
CLOUDFLARE_API_TOKEN="your-zone-scoped-token"
CF_DNS_API_PROXY_URL=""
```

Token 只需目标 Zone 的 DNS 编辑权限，Zone ID 在 Cloudflare 域名概览页获取。API 默认直连；如本地 DNS 返回代理假 IP 或 API 直连受限，可单独把 `CF_DNS_API_PROXY_URL` 设为 `http://127.0.0.1:7890`。它只影响 Cloudflare 管理请求，TCP 比较始终直接连接数字 IP，TG 继续使用独立的代理配置。

每次完整优选成功后，从 Cloudflare API 读取**精确域名**当前的 A 记录内容，不使用公共解析结果，以免受到缓存、橙云或 Mihomo 假 IP 干扰：

1. 对域名旧 IP 和本轮通过 NRT、速度门槛的新候选，各做 5 次直连 TCP 连接，取中位延迟，并各发一次真实 ICMP ping。ping 最多等待 2 秒，输出直接丢弃。旧 IP 有多个时，正常延迟比较以其中最快值为基准。
2. 等 30 秒，以同样方法再测一轮。**正常优选**要求同一个新 IP 每轮都满足 `新延迟 ≤ 当轮旧最快延迟 × 0.90`。**故障切换**要求同一个旧 IP 两轮 ping 都无响应，且同一个新 IP 两轮 ping 均成功、所有 TCP 测量均成功；故障切换不要求快 10%。
3. 保留现有 A 记录数量，用 [Cloudflare 批量修改接口](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/batch/) 只修改记录内容，保留 TTL、备注等属性。正常优选需足够的新 IP 整组替换；故障切换优先，只替换确认失败的记录，其他记录保留。两种情况都从符合对应条件的候选中选择两轮平均延迟最低的一组，候选不足则本轮不提交更新。
4. 写入前重新读取并核对记录，若比较期间发生变化则跳过；写入后再次读取确认。请求或读回失败不会自动重试写入，TG 会明确显示结果未确认。

只处理已有、DNS-only（灰云）的 A 记录，不新增或删除记录，不修改 AAAA/CNAME。ping 仅失败一轮或第二轮恢复时，不触发故障切换；ping 缺失、权限错误或进程异常也不算“IP 不通”。新旧 IP 全部 ping 不通时，不触发故障切换，避免本地网络异常引起误改。旧 TCP 测量不完整且未确认两轮 ping 故障、没有 A 记录、橙云或 API 失败时，保留解析。Cloudflare 批量请求在数据库中事务执行，但全球 DNS 传播并非同时完成。`--smoke` 和 `--check` 都不更新 DNS。

TG 报告包含两轮延迟、旧 IP 的 ping 结果，以及“延迟优化 / 故障切换 / 未更新 / 结果未确认”状态。报告发送后删除，不保存比较历史。

10% 是本项目为减少频繁切换采用的策略值。旧 IP 延迟为 60 ms 时，新 IP 两轮都需要不高于 54 ms；仅快约 1 ms 不会触发更新。延迟会受排队等因素影响（参见 [IETF 延迟变化说明](https://www.rfc-editor.org/rfc/rfc5481.html)），两轮各 5 次取中位数可以减少偶然波动的影响，但不能保证整天或不同运营商下的表现。

完整优选中的两轮发生在同一次扫描结束后，间隔 30 秒，不是跨两次三天周期确认。故障切换按真实 ICMP ping 的结果触发；某 IP 禁止 ICMP 回包时，也会被视为 ping 不通。

每 6 小时的独立健康检查遵循以下流程：旧 IP 首次 ping 不通后等 30 秒复查；确认两次均不通，才读取 RAM 中的备用 IP，并重新验证 NRT 地区、TCP 延迟和下载速度。备用合格后再次执行两轮 ping/TCP 故障复核，只允许故障切换，不对健康记录做 10% 延迟优化。若复核时旧 IP 恢复，则保留解析。

重启会清空 RAM 备用列表。确认故障但没有足够的合格备用 IP 时，会提前启动一轮完整优选，完成后按原有规则决定更新，不改变三天周期。测速接口限流或检查执行异常时会停止并报告，不反复启动扫描。健康时不推送；确认故障后的处理结果或检查错误会通过 TG 发送，随后删除本地报告。故障发现可能等待接近 6 小时，备用验证、完整扫描和 DNS 缓存会增加恢复时间。

## 使用

```bash
# 检查配置和直连路由
sudo cf-optimizer --check
# 按周期在后台运行（非运行日会跳过）
sudo systemctl start --no-block cf-optimizer
# 忽略周期，立即在前台完整优选、比较并推送
sudo cf-optimizer --force
# 手动从 IPv6 /48 网段池采样、优选并推送（不修改 DNS）
sudo cf-optimizer --ipv6
# 手动执行一次轻量健康检查
sudo systemctl start --no-block cf-optimizer-health
# 查看运行状态（systemd 内存状态，不创建日志文件）
systemctl show cf-optimizer -p ActiveState -p Result -p StatusText
# 运行期间的当前阶段，结束后清理
cat /run/cf-optimizer/status.json
# 当前 IP（最多 5 个，每行一个）
sudo cat /run/cf-optimizer/best-ips.txt
# 查看下次执行时间
systemctl list-timers cf-optimizer.timer
systemctl list-timers cf-optimizer-health.timer
# 检查今天是否为运行日：退出码 0=是，1=否，2=配置错误
sudo cf-optimizer --schedule-check
# 停止一轮扫描
sudo systemctl stop cf-optimizer
# 关闭定时
sudo systemctl disable --now cf-optimizer.timer
# 关闭每 6 小时健康检查
sudo systemctl disable --now cf-optimizer-health.timer
```

`cf-optimizer --force` 可在前台显示过程并绕过日期限制；定时器列表显示的是下次日期检查时间，实际扫描日期由三天周期决定。`--smoke` 优先检查最新优选列表，首次运行回退到少量已知 NRT IP，有一个通过下载验证即结束，不替换正式结果或更新 DNS。并发运行会被拒绝；退出和停止服务会清理临时路由与数据。

## 扫描与测速说明

### 第三方反代优选（亚太地区）

```bash
sudo cf-optimizer --proxy-check
# 是否到达自动反代优选日期：0=到期，1=未到期或已关闭，2=配置错误
sudo cf-optimizer --proxy-schedule-check
# 前台运行，结束后通过现有 TG 配置推送
sudo cf-optimizer --proxy
# 立即后台运行反代优选，忽略周期
sudo systemctl start --no-block cf-optimizer-proxy
systemctl show cf-optimizer-proxy -p ActiveState -p Result -p StatusText
sudo cat /run/cf-optimizer/best-proxy-ips.json
```

从 [skfoa/tg-proxy-fetcher 的公开 CSV](https://raw.githubusercontent.com/skfoa/tg-proxy-fetcher/main/data/scan_ips.csv) 获取候选，筛选亚太地区、TLS 开启、上游失败计数为零的地址。除腾讯、阿里、华为、AWS、Oracle 等常见厂商外，也覆盖 ByteVirt、LucidaCloud、Leaseweb、VMISS、Zenlayer 等；其他具有有效 ASN 和厂商名称的来源也参与公开初筛。排除 CF 官方地址，避免混作第三方反代。默认公开候选上限 10000 条，每地区公开检查最多 40 条、全局最多 240 条；按 443、8443、其他 TLS 端口轮流分配名额，每组同一厂商最多占 3 个公开检查名额。其他端口组兼顾不同实际端口，保留本机候选优先级。上游标签仅用于初筛；地区以本轮验证结果为准。仅请求列表中的具体 IP 和端口，不扫描厂商整段。

同时加载本机 `proxy-candidates.csv`，优先香港 HKG、东京 NRT、新加坡 SIN 和大阪 KIX，其他亚太机房也参与。允许范围依据 [Cloudflare 状态页](https://www.cloudflarestatus.com/)的 Asia/Oceania 分组及[网络机房列表](https://www.cloudflare.com/network/)，在代码中固定，不根据入口 IP 的注册地猜测。自带候选在过去的 N1 测试中可用，不保证以后一直有效。安装器保留已有候选文件，方便维护自己的列表；新安装才复制默认文件。

单次公开粗筛达标的新地址保存在 `proxy-public-candidates.csv`，后续可参与公开检查，未完成实际节点验证前不会参与 DNS 更新。此文件不会扩充私有节点验证允许列表；安装升级会保留本机已有版本。粗筛结果不等同于正式三轮复测达标。

仅保留 TLS 入口。80、8080、8880、2052、2082、2086、2095 等常见明文 HTTP 端口被固定排除，即使公开来源标记 `tls=true` 也不会导入。此规则同时覆盖单 IP、网段采样和缓存 DNS 候选。443、8443、2053、2083、2087、2096、9443 以及其他实际通过 TLS 验证的自定义端口仍可参与；连接时必须验证目标主机证书，不会因 TLS 失败退回明文 HTTP。

`proxy-cidrs.csv` 保存第三方 `/24` 网段、端口、已知机房及来源；同一个网段的不同端口分别保留。每轮每段默认采样 16 个地址（`CF_PROXY_CIDR_SAMPLES_PER_RANGE`，允许 1–64），按北京时间三天周期轮换，每 16 个周期覆盖该段的 254 个主机地址。不保存轮换历史，不在每次运行时写配置文件。网段标签仅用于分配初筛名额，相邻 IP 必须重新通过公开 CF 检查并确认实际机房在亚太，不能直接作为有效节点或 DNS 更新候选。网段池不会扩充私有节点验证允许列表。默认不遍历全部网段，以控制 N1 的运行时间；公开检查全局 240 条、实际节点验证全局 30 条的上限继续生效。

所有 TCP、TLS、WebSocket 和下载测试都绑定配置中的物理网卡，绕过本机 Mihomo；候选池获取和 TG 推送使用配置中的代理。先做有数量上限的 TCP/公开 CF 检查，再按地区选择候选；下载逐条进行，默认三轮、每轮 4 MiB，所有轮次完整成功、地区一致且不低于 `CF_PROXY_MIN_MBPS=8` 才入选。HTTP 429 会停止本轮，不把旧结果当成新结果。

真实节点验证需要在设备上配置 `CF_PROXY_VERIFY_NODE=1` 以及原有 `CFST_IPV6_NODE_HOST`、`CFST_IPV6_NODE_PATH`（这里也用于 IPv4）。**只会向本机候选 CSV 中明确列出的 IP:端口发送这些私有连接信息**，每轮最多 30 个地址；远程 CSV 不能自动扩充允许范围。默认关闭真实节点验证，报告会明确标记“仅公开 CF 测试”。启用后，未获本机允许的新地址只在“待确认”一栏出现，不冒充已验证节点。往本机候选 CSV 添加一行，代表允许向该地址发送节点域名和 WS 路径；不会发送 UUID、密码或 Cloudflare/TG Token。SNI 与 WS Host 必须相同。握手验证不包含完整代理业务认证。

本模式保留并报告每条地址自己的端口，不新增 Telegram 接收器，也不改变三天一次的官方 IPv4 定时任务。第三方反代不能仅通过修改 A 记录切换端口，原有 `/set`、`/use4`、`/use6`、`/rescue` 的官方网段限制保持原样。

可选的反代 DNS 自动更新：在私有 `env.sh` 配置独立的 `CF_PROXY_DNS_NAME`、客户端实际端口 `CF_PROXY_DNS_PORT=443`，并设置 `CF_PROXY_DNS_AUTO_ENABLED=1`（仓库默认 0）。每次手动或定时反代优选成功后，只从本轮结果选相同端口、下载测速最快且在本机允许列表内的候选，再做两轮直连公开 CF 与真实节点握手验证，通过后更新这个独立域名。该规则直接选本轮最快候选，不沿用官方 IPv4 更新的 10% 比较条件；同 IP 不重复写入。无合格候选、验证失败或复测期间 DNS 变化时不覆盖，写入结果不确定时不自动重试。DNS 更新结果附在同一份 TG 报告开头，报告随后清理。原 `CF_DNS_NAME` 和健康检查频率不变。

自动反代优选默认启用：`CF_PROXY_AUTO_ENABLED=1`。它与官方优选共用 `CFST_SCHEDULE_DAYS`（默认 3）、`CFST_SCHEDULE_ANCHOR` 和北京时间 03:00 定时器，不另建一个同时抢带宽的定时器。到期日按顺序运行两轮，各自结束后推送报告；非到期日两轮都直接跳过。自动运行状态查看 `cf-optimizer.service`，手动后台运行状态查看 `cf-optimizer-proxy.service`。`CF_PROXY_AUTO_ENABLED=0` 只关闭自动反代优选，手动 `--proxy` 仍可使用。没有新增磁盘运行日期、日志或历史文件；错过的周期不补跑。

下载候选按机房和端口组分别保留，`CF_PROXY_DOWNLOAD_PER_COLO=5` 表示每个机房的每个端口组最多复测 5 条；实际节点验证仍受每轮全局 30 条上限约束。443、8443、其他端口的合格结果都会进入 RAM 快照和 TG 报告。自动 DNS 更新仍使用 `CF_PROXY_DNS_PORT`；手动 `/fd443` 和 `/fd8443` 分别选择对应端口的第一名，不能用 DNS 修改客户端端口。

候选 CSV 下载内容、探测数据和待推送报告只在 RAM；报告推送后删除，只有最新成功完成的反代结果 JSON 保留在 `/run`，下一次完成时覆盖。没有应用日志、磁盘测速文件或运行历史。

### 手动 IPv6 优选

`sudo cf-optimizer --ipv6` 从 `ipv6-cidrs.txt` 的候选网段内采样。默认池为：

```text
2606:4700:ff00::/48
2606:4700:52::/48
2606:4700:57::/48
2606:4700:5a::/48
```

这些网段在 2026-11-01 的线路测试中命中过 NRT，不代表全段或以后始终是 NRT。每轮每段采样 64 个地址（`CFST_IPV6_SAMPLES_PER_CIDR`，允许 4–256），从已验证后缀和随机地址中产生候选，不遍历整段。按 `/48` 轮换验证和下载，避免一个网段用完所有测速预算；样本未命中不代表整段没有可用地址。

IPv6 候选必须通过直连 TCP、实时 `colo=NRT`、节点端口和下载验证。默认额外测试 2096 端口（`CFST_IPV6_NODE_PORT`）；在设备的 `env.sh` 同时填写 `CFST_IPV6_NODE_HOST` 和 `CFST_IPV6_NODE_PATH`，还会使用该 Host 作为 TLS SNI，验证真实 WebSocket 升级及 NRT 响应。仅适用于 SNI 与 WS Host 相同的节点；不要将业务路径或凭据提交到仓库。未配置这两项时只验证节点 TCP 端口，不代表业务认证或完整代理流量已测试。

这是独立手动入口，不切换三天一次的 IPv4 定时任务，也不修改任何 A / AAAA 记录。通过原有 TG 代理发送报告后删除报告。IPv6 结果单独保留在 `/run/cf-optimizer/best-ips-v6.txt`，不覆盖 IPv4 列表，重启后清空；候选网段文件只在安装或手动修改时写系统盘，运行采样不修改它。

### Telegram 私聊切换 IPv4 / IPv6

#### 接入已有 FlClouds 机器人

保留原机器人的上传、下载及其他功能，DNS 菜单提供以下指令：

| 指令 | 功能 |
| --- | --- |
| `/dns` | 同时查看 CF、FD 443、FD 8443 三个域名当前的 A/AAAA IP |
| `/usecf` | 使用 N1 最近 CF IPv4 优选第一名 |
| `/fd443 [IP或IP:443]` | 更新 tj；不带 IP 使用 FD 443 第一名，带 IP 手动指定 |
| `/fd8443 [IP或IP:8443]` | 更新 nj；不带 IP 使用 FD 8443 第一名，带 IP 手动指定 |
| `/fdcheck IP[:443或8443]` | 只检测端口，不改 DNS；不带端口分别检查 443/8443 |
| `/use6` | 使用 N1 最近 CF IPv6 优选第一名 |
| `/fdscan` | 手动启动 N1 的 FD 优选，完成后通过原机器人推送 |

**优选、下载测速和实际节点验证只在 N1 执行。甲骨文不参加优选或下载测速；手动 FD 更新时先做轻量公开 CF/TLS 端口检查，再提供备用 Cloudflare DNS API 线路。**

默认由 N1 处理。N1 离线、8 秒内未领取任务，或明确报告 DNS 请求在写入前失败时，自动使用甲骨文。N1 的节点验证失败、扫描锁占用或结果缺失时，不绕过验证。任务已领取但更新结果不明时，先查询当前 DNS，不重复提交未知结果的更新。任务最长 120 秒，N1 在写入前检查有效期。

N1 worker 每约 45–60 秒把已经测出的 CF4、CF6、FD 443、FD 8443 第一名及原始结果时间同步到甲骨文进程内存，最多四个候选，不同步节点域名、WS 路径、凭据、完整候选池或测速报告。结果最多有效 96 小时，拒绝异常未来时间。CF 地址受官方网段限制；两个 FD 端口分别使用独立的甲骨文允许列表，快照不能扩大授权。没有缓存就拒绝切换，不伪造第一名，不在甲骨文重新测速。甲骨文重启清空缓存，待 N1 在线重新同步。

`fd443`、`fd8443`、`fdscan` 也支持不带 `/` 的写法；`/usefd` 继续兼容 443 更新。两个 FD 域名必须彼此独立，且不同于原 CF 域名。`CF_PROXY_DNS_443_NAME` 可回退到原 `CF_PROXY_DNS_NAME`；`CF_PROXY_DNS_8443_NAME` 须单独配置。只有原机器人接收 TG 消息。

手动指定示例：`/fd8443 82.110.234.167:8443`，或 `/fd443 39.105.24.90`。仅接受公网 IPv4，端口须与命令一致。自定义地址不依赖第一名缓存、不加入长期候选池或允许列表。N1 在线时，两轮公开 CF/TLS 检查均通过且亚太机房一致才更换；不会发送原节点 Host、WS 路径或凭据，不验证实际节点或下载速度。公开检查失败不会绕到备用线路强行更新。N1 不可用时，甲骨文也必须先对指定 IP:端口完成两轮公开 CF/TLS 检查，再写入对应 DNS；回复注明甲骨文观测机房、未测速和未验证节点，不能代表家中线路。两边都在指定端口失败时探测另一个端口并提示命令，不会自动改端口或域名；遇到限流停止探测。`/fdcheck IP` 可只检查两个端口，不修改 DNS。沿用原运行锁、写前核对和写后读回机制；临时数据均在内存，不新增定时检测或日志。之后的自动优选仍按原规则运行，可能替换手动指定的地址。

`/fdscan` 仅提交一个空的 RAM 请求文件。root 所有的 `cf-optimizer-fd-request.path/service` 只允许启动固定的 `cf-optimizer-proxy.service`，不读取用户命令或参数，不新增常驻机器人。worker 保留 `NoNewPrivileges` 且不增加 capabilities；运行目录使用专用 cfoptimizer 用户组供固定派发器删除请求。结果沿用原来的代理 TG 推送与清理流程。N1 离线时甲骨文不能启动或执行测速。

备用回复注明线路及 N1 结果时间。备用更新不保证家庭宽带此刻仍可用。DNS 无法修改客户端端口，FD 端口须保持一致。

旧 `/use4`、`/usetj`、`/rescue`、`/rescuetj` 隐藏兼容，不再强制选择线路。裸 IP 和 `/set IP` 仍指定 CF 地址；`/settj IPv4` 仍指定已授权 FD 地址，均自动选择线路。按地区、名次的旧 `/usetj` 仅在 N1 在线时支持，不显示在简化菜单中。

完整配置与测试见 [FlClouds 接入说明](integrations/flclouds/README.md)。

不带 IP 的 `/fd443`、`/fd8443` 在 N1 按近期同端口结果的速度排名逐个验证。首选不通或两轮节点验证失败时，会继续检查下一条；只有两轮都通过才更新。最多检查 8 条，并限制检测时间，期间共用一次运行锁。失败地址只在 N1 worker 进程内存中暂时跳过 15 分钟，新一轮扫描或 worker 重启后清除，不删除静态候选池、不写失败日志。N1 同步给甲骨文的第一名也会排除暂时失败的地址。限流、DNS 变化、API 故障、指令过期、写入结果不明时停止，不继续换其他 IP。手动明确指定 IP 时不自动换成另一个地址；全部失败时提示 `/fdscan` 重新优选。

#### 使用专用机器人的独立接收器

可选的 `cf-optimizer-bot.service` 复用推送机器人。直接在机器人私聊中发送一个 IP，或发送 `/set IP`：IPv4 切为一条灰云 A 记录，IPv6 切为一条灰云 AAAA 记录，移除**同一个精确域名**的其他 A / AAAA。已有目标类型时保留其中一条的 ID、TTL 和备注；跨类型新建时沿用已有 TTL、备注和标签。存在 CNAME、橙云或异常记录时拒绝操作；TXT 等非地址记录不处理。

**已有接收程序的机器人不可再启动这个独立监听器。** 如果 Token 已用于 OpenClaw、下载工具或其他 Bot 程序，必须把 DNS 处理入口接入原程序的消息分发，或使用专用机器人；两个程序不能分别长轮询同一个 Token。纯发送通知可以共用 Token。发送使用说明不会注册、覆盖或删除已有的命令菜单。

在设备的 `env.sh` 设置以下项目，然后启动服务：

```bash
CF_TG_CONTROL_ENABLED=1
CF_TG_ALLOWED_USER_ID="你的个人数字 User ID"
CF_TG_DIRECT_INTERFACE="eth0"
# 还需原有 TELEGRAM_*、CLOUDFLARE_*、CF_DNS_NAME，
# 以及真实节点的 CFST_IPV6_NODE_HOST / PATH / PORT（同时用于 IPv4）。
sudo systemctl enable --now cf-optimizer-bot.service
```

只接受匹配 User ID 和正数 Chat ID 的个人私聊新消息；不接受群聊、转发、机器人消息和历史命令。User ID 留空时使用个人 Chat ID。`/dns` 查询当前解析；`/use4`、`/use6` 使用 RAM 中对应优选列表的第一名，再进行实时验证；`/help` 查看说明。RAM 列表重启清空时需要先优选，或直接发送 IP。机器人不提供执行任意系统命令的入口。

每次切换都先确认是 Cloudflare 官方范围内的数字地址，再绑定配置的物理网卡，直连做两轮 NRT 和真实节点 TLS / WebSocket 验证。验证失败不修改 DNS。手动指定不套用原自动优选的“快 10%”规则。与完整扫描共用只读 flock；扫描期间收到更新请求会提示稍后重发。写前核对 DNS 是否发生变化，使用 Cloudflare 批量事务完成切换，再读回核对；写入结果不确定时不会自动重试，请使用 `/dns` 查询。

TG 长轮询和回复均走配置的代理，不需开放入站端口。Webhook 已存在时停止，不会删除已有 Webhook。服务以普通用户运行，无额外 Linux capabilities；不保存命令、日志、游标、报告或历史结果，重启时丢弃旧命令。安装的 tmpfiles 配置仅在 RAM 中创建可读的共享锁，程序不写该锁内容。旧 Linux 内核如不允许普通用户绑定网卡，会拒绝验证并保留 DNS。

此功能保留三天一次的 IPv4 优选；六小时健康定时器默认关闭。手动或显式启用健康检查时，域名只剩 AAAA 会安静跳过；定时 IPv4 优选不会创建 A 记录覆盖 IPv6。切回 A 后恢复原有 IPv4 自动更新规则。DNS 缓存可能延迟客户端看到更新。升级会安装此服务，默认关闭，只有显式启用时才开始监听。

`CFST_TARGET_CIDRS="auto"` 从 Cloudflare 官方拉取所有 IPv4 段；失败时使用安装时附带的官方列表。官方列表当前为 15 段、1,524,736 地址，对应约 5956 个 `/24` 采样点。

发现后按延迟优先选择最多 8 个 NRT `/24` 细扫。默认保留 `172.64.229.0/24` 为已知种子，**不会限制全段发现范围**。验证和下载按 `/24` 轮换，防止一个不可下载的网段占满所有候选。任播路由会变，某 IP 不保证一直属于 NRT。

公共测速地址存在大小限制与限流，因此默认使用 25MB 文件连续限时下载。速度按实际字节数/实际耗时计算，包含 TLS 和连接开销，按 1024² 字节/秒展示。下载会消耗网络流量；它验证的是测速站点，具体业务 Host/SNI 仍需单独验证。可配置自己的 HTTPS 测速文件，避免公共接口的限流。

首轮全段发现实测探测阶段需要数分钟；总时长受运营商路由、丢包和下载接口影响，服务最长约 40 分钟自动停止。

## 检查代码

```bash
python3 -m unittest discover -s tests -v
for file in install.sh linux/cf-optimizer linux/*.sh; do bash -n "$file" || exit; done
```

MIT License；上游项目及外部测速核心遵循各自许可证。仓库只包含空白配置模板，无设备地址、密码、Bot Token 或 Chat ID。
