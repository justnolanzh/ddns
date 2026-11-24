# ddns

A single-file DDNS updater for [DNSPod](https://www.dnspod.cn/) — keeps one `A` record pointed at
this host's public IP. Runs as a macOS LaunchDaemon (or any supervisor on Linux).

No third-party dependencies: Python 3.8+ standard library only.

## 特性

- 三个 HTTPS 源探测公网 IP（`ifconfig.me` / `ipify` / `ipinfo`），多数表决，单源告警，全程 10 秒超时
- 写入前先用 `Record.Info` 比对记录值，IP 没变就不调写接口
- 校验 DNSPod 返回的 `status.code`（`Record.Ddns` 失败时 HTTP 仍是 200，只看状态码会"假成功"）
- 连续失败指数退避：60s → 120s → … → 1800s 封顶
- 目标记录写在 `ddns.json`，**每个周期重读**：改记录名、换 record_id、调 TTL 都不需要重启进程
- API Token 放独立文件（`chmod 600`），不硬编码在代码里，也不进版本库
- 日志自轮转（1 MB × 4），带时间戳；launchd 的 stdout/stderr 单独落到 `.err`
- `flock` 保证单实例；`--once` / `--dry-run` 便于现场排查

## 安装（macOS）

### 1. 放好文件

```sh
sudo mkdir -p /opt/ddns
sudo chown "$USER":staff /opt/ddns
cp dnspod.py ddns.json /opt/ddns/
```

### 2. 写入 API Token

DNSPod 控制台 → 账号中心 → 密钥管理 → 创建密钥（**DNSPod Token**）。
控制台会给出两段：密钥 ID 和一个 32 位密钥，合起来的格式是 `ID,Token`：

```sh
printf '%s' '12345,0123456789abcdef0123456789abcdef' > /opt/ddns/.dnspod_token
chmod 600 /opt/ddns/.dnspod_token
```

Token 缺失、或只填了后半段（没有 `ID,`），都会在启动时报错或直接被接口拒绝。

### 3. 填目标记录

`ddns.json` 里的 `domain_id` / `record_id` 可以直接查出来：

```sh
TOKEN=$(cat /opt/ddns/.dnspod_token)
# 列出所有域名，拿 domain_id
curl -s https://dnsapi.cn/Domain.List -d "login_token=$TOKEN&format=json"
# 列出该域名的记录，拿 record_id
curl -s https://dnsapi.cn/Record.List -d "login_token=$TOKEN&format=json&domain_id=<domain_id>"
```

| 字段 | 说明 |
|---|---|
| `domain` | 裸域名，如 `example.com`，仅用于日志显示 |
| `domain_id` | DNSPod 的域名 ID |
| `record_id` | 要维护的那条 A 记录的 ID |
| `sub_domain` | 该记录的主机记录名（`@` 表示裸域名）。必须与 `record_id` 指向同一条记录 |
| `record_line` | 线路，一般 `默认` |
| `ttl` | 记录 TTL，DNSPod 免费版最小 600 |

> `sub_domain` 和 `record_id` 是同一个记录的两个标识。在控制台改名只会改 `sub_domain`，
> `record_id` 不变——如果两者对不上，`Record.Ddns` 会拒绝写入。

### 4. 先手工跑一遍

```sh
cd /opt/ddns
python3 dnspod.py --dry-run    # 只探测和比对，不写
python3 dnspod.py --once       # 真写一次；再跑一次应输出 "no change"
```

### 5. 装成开机自启的 daemon

```sh
sudo install -o root -g wheel -m 644 com.example.dnspod.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/com.example.dnspod.plist
```

`com.example.dnspod.plist` 里要改两处：

- `Label` 必须与 plist 文件名一致（本文档以 `com.example.dnspod` 为例）
- `UserName` 改成跑这个脚本的普通用户——daemon 不需要 root

之后的日常操作：

```sh
# 只改了 dnspod.py 或 ddns.json 里的目标
sudo launchctl kickstart -k system/com.example.dnspod

# 改过 plist 本身（必须 bootout + bootstrap，kickstart 不会重读 plist）
sudo launchctl bootout system/com.example.dnspod
sudo launchctl bootstrap system /Library/LaunchDaemons/com.example.dnspod.plist

# 卸载
sudo launchctl bootout system/com.example.dnspod
```

## 日志与排查

| 路径 | 内容 |
|---|---|
| `/opt/ddns/logs/ddns.log` | 主日志，自轮转 `ddns.log` → `.1` → `.2` → `.3`（1 MB × 4，约四周） |
| `/opt/ddns/logs/dnspod.err` | launchd 接管的 stdout/stderr，正常应为 0 字节；非空说明启动阶段就崩了 |

日志目录默认是脚本同级的 `logs/`，可用环境变量 `DDNS_LOG_DIR` 或 plist 的 `EnvironmentVariables` 覆盖。

```sh
tail -f /opt/ddns/logs/ddns.log
```

关键日志行：

```
INFO  no change: ddns.example.com record=1.2.3.4 actual=1.2.3.4   # 一切正常
INFO  IP change detected for ... record=旧 actual=新               # 发现变化
INFO  record updated -> ddns.example.com 新IP                     # 写入成功
ERROR Record.Ddns rejected for ... (code ...)                     # 被拒，随后会 backoff
```

常见失败：

| 现象 | 原因 |
|---|---|
| `401 Authorization Required` | `login_token` 格式不对，多半是漏了 `ID,` 前缀 |
| `code 10003 传入的 Token 不存在` | Token 已被删除或重建，去控制台确认密钥 ID |
| `code 10 记录列表为空` | `sub_domain` 写错，该名字下没有记录 |
| `Record.Ddns rejected` | `record_id` 与 `sub_domain` 不是同一条记录，或被域名权限拒绝 |
| `all IP sources failed` | 本机出网/DNS 有问题（代理软件、防火墙） |

## 安全说明

- DNSPod Token 等同于账号密码：API 2.0 的 Token 无法按域名收窄权限，泄漏就等于丢掉整个账号。
  所以它只放在 `/.dnspod_token`（600），`.gitignore` 已排除，代码里只留路径。
- Token 一旦泄漏（贴进 issue、提交进仓库、发给别人），去控制台**删除该密钥并新建**，不要试图改回来。
- 想从"账号级"降到"单域名级"：用腾讯云 CAM 建子账号，只授权这一条域名，用 API 3.0
  （`dnspod.tencentcloudapi.com`，`SecretId` 上送、`SecretKey` 只参与签名不出网，配
  `tencentcloud-sdk-python-dnspod`）。本脚本用的是 2.0，如需 3.0 需替换 `api()` 一层。
- 探测 IP 全走 HTTPS：明文 HTTP 探测可被中间人替换返回值，让别人把你的域名指到他的机器上。

## Linux

脚本本身与平台无关，`fcntl` 在 Linux 上同样可用。用 systemd unit 或 cron 定时执行
`python3 /opt/ddns/dnspod.py --once` 即可，`--once` 模式下不会进入常驻循环。

## License

MIT
