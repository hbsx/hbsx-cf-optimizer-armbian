# Cloudflare NRT 优选 · Armbian / N1 / x86 Linux

支持 ARM64 Armbian 和 x86 Linux 的 Cloudflare 东京 NRT IP 优选工具。优选 IP 并推送 Telegram，可选在两轮延迟比较达标或确认旧 IP ping 故障后自动更新 Cloudflare DNS。

基于 [huangmlj/cf-optimizer](https://github.com/huangmlj/cf-optimizer) 的流程改写，使用官方 [XIU2/CloudflareSpeedTest](https://github.com/XIU2/CloudflareSpeedTest) 测 TCP 延迟，curl 验证地区和下载速度。第三方测速核心在安装时下载，不包含在仓库内。

## 默认行为

- 默认每 **3 天、北京时间 03:00** 运行一次；关机时错过的任务不补跑。
- 独立健康检查每天北京时间 **00:00、06:00、12:00、18:00** 运行。旧 IP 正常时每个只 ping 一次，不做 TCP 测速、下载或全网扫描，不生成报告，不推送 TG。
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
- **只留下 `/run/cf-optimizer/best-ips.txt`**，下次成功运行覆盖，重启后清空。
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

安装器同时启用 `cf-optimizer-health.timer`，每 6 小时检查 DNS 旧 IP。两个任务共用运行锁，完整优选期间的健康检查会跳过，不会互相清理运行数据。健康检查的临时数据仍只允许写 `/run`，不保存日志或历史结果。

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
