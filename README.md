# Cloudflare NRT 优选 · Armbian / N1

面向 ARM64 Armbian 的 Cloudflare 东京 NRT IP 优选工具。只输出 IP 并推送 Telegram，不修改域名 DNS。

基于 [huangmlj/cf-optimizer](https://github.com/huangmlj/cf-optimizer) 的流程改写，使用官方 [XIU2/CloudflareSpeedTest](https://github.com/XIU2/CloudflareSpeedTest) 测 TCP 延迟，curl 验证地区和下载速度。第三方测速核心在安装时下载，不包含在仓库内。

## 默认行为

- 每天北京时间 **03:00** 运行；关机时错过的任务不补跑。
- 覆盖 Cloudflare 官方全部 IPv4 网段，先逐 `/24` 采样发现 NRT，再完整细扫最多 8 个候选 `/24`（2048 IP）。不是逐个扫描全部约 152 万个地址。
- 地区验证必须返回 `colo=NRT`，下载响应 `CF-RAY` 也必须属于 NRT。
- **测速直连**：独立系统用户临时查询主路由表，绕过本机 Mihomo TUN；不停止代理、不修改其配置。
- **TG 走代理**：明确使用本机 Mihomo 的 `http://127.0.0.1:7890`，可配置。
- 最多复测 20 个下载候选，找到 5 个达标 IP 后结束。默认每个约 3 秒、速度 ≥5 MB/s、TCP 延迟 ≤250ms、TCP 丢包率 ≤0.1。
- 下载接口返回 **429 时立即停止请求**，保留已有 IP，并尝试推送失败原因，不把失败或旧结果当成本次结果。

## eMMC 与临时文件

正常运行时只允许写入 `/run` 内存文件系统，程序和配置所在系统盘被该服务挂载为只读。

- 不保存程序日志；服务输出丢弃。
- 禁用该服务的 swap、崩溃转储和 Python 字节码写入。
- 临时 CSV、响应头、状态和报告都在 RAM，结束即删除。
- 推送前把报告读入进程内存并删除文件，重试结束后也不保留本地副本。
- **只留下 `/run/cf-optimizer/best-ips.txt`**，下次成功运行覆盖，重启后清空。

程序、配置和定时任务仅安装/修改时写系统盘。操作系统自身可能记录少量服务启停信息；本项目不会关闭整台设备的系统日志。

## 安装

适用于 ARM64/aarch64，已在 N1、Armbian 25.08 / Ubuntu 24.04、Linux 6.1 上验证基本运行、直连路由、代理 TG 推送与清理。需要 Python3、curl、iproute2、util-linux、systemd；安装器会检查依赖，不自动升级系统。

```bash
git clone https://github.com/hbsx/hbsx-cf-optimizer-armbian.git
cd hbsx-cf-optimizer-armbian
sudo bash install.sh
sudo nano /opt/cf-optimizer/env.sh
```

安装会启用凌晨 3 点的定时任务，升级时保留已有 `env.sh`；新增参数请参照 `linux/env.linux.sh.example` 补充。

填写自己的 Telegram 配置，勿上传真实配置：

```bash
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
TELEGRAM_PROXY_URL="http://127.0.0.1:7890"
```

Mihomo 的本地代理端口应允许此用户连接。如果默认网关本身也有代理，需另外确保测速出站为直连。本工具只保证绕过**本机**的 TUN。

## 使用

```bash
# 检查配置和直连路由
sudo cf-optimizer --check
# 后台运行一轮
sudo systemctl start --no-block cf-optimizer
# 查看运行状态（systemd 内存状态，不创建日志文件）
systemctl show cf-optimizer -p ActiveState -p Result -p StatusText
# 运行期间的当前阶段，结束后清理
cat /run/cf-optimizer/status.json
# 当前 IP（最多 5 个，每行一个）
sudo cat /run/cf-optimizer/best-ips.txt
# 查看下次执行时间
systemctl list-timers cf-optimizer.timer
# 停止一轮扫描
sudo systemctl stop cf-optimizer
# 关闭定时
sudo systemctl disable --now cf-optimizer.timer
```

`cf-optimizer --force` 可在前台显示过程；`--smoke` 只检查少量已知 NRT IP，不替换正式结果。并发运行会被拒绝；退出和停止服务会清理临时路由与数据。

## 扫描与测速说明

`CFST_TARGET_CIDRS="auto"` 从 Cloudflare 官方拉取所有 IPv4 段；失败时使用安装时附带的官方列表。官方列表当前为 15 段、1,524,736 地址，对应约 5956 个 `/24` 采样点。

发现后按延迟优先选择最多 8 个 NRT `/24` 细扫。默认保留 `172.64.229.0/24` 为已知种子，**不会限制全段发现范围**。验证和下载按 `/24` 轮换，防止一个不可下载的网段占满所有候选。任播路由会变，某 IP 不保证一直属于 NRT。

公共测速地址存在大小限制与限流，因此默认使用 25MB 文件连续限时下载。速度按实际字节数/实际耗时计算，包含 TLS 和连接开销，按 1024² 字节/秒展示。下载会消耗网络流量；它验证的是测速站点，具体业务 Host/SNI 仍需单独验证。可配置自己的 HTTPS 测速文件，避免公共接口的限流。

首轮全段发现实测探测阶段需要数分钟；总时长受运营商路由、丢包和下载接口影响，服务最长约 40 分钟自动停止。

## 检查代码

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh linux/cf-optimizer linux/*.sh
```

MIT License；上游项目及外部测速核心遵循各自许可证。仓库只包含空白配置模板，无设备地址、密码、Bot Token 或 Chat ID。
