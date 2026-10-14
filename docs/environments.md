# 不同环境怎么改

README 是按我自己的环境写的：日本、公网 IPv4、iPhone、EC20、x86 NAS。下面是其它情况的改法。

标了“没实测”的是我根据原理写的，大方向没问题，细节可能要你自己调。

## 网络

### 没有公网 IPv4

先确认一下：
- 路由器 WAN 口的 IP；
- `curl -4 ifconfig.me` 的结果。

两个不一样，或者 WAN 口是 `100.64.x.x`、`10.x.x.x` 这种，就是在运营商的大内网里，外面连不进来。可以按你需要的功能选：

#### 只要短信推送（最简单）
收短信、推送都是网关主动往外连，不需要任何入站端口：

1. Bark 不用自建的，用官方服务器 `https://api.day.app`。在 Bark App 里直接复制官方服务器的测试链接，填到「设置 → Bark 推送设备」。建议同时开推送加密，因为内容要经过官方服务器。
2. 只启动需要的服务：`sudo docker compose up -d asterisk app`。
3. 在外面看网页的方法：
   - 用 Tailscale：手机连上 Tailscale，打开 `http://<机器的 100.x 地址>:18300`；
   - 或者用 Cloudflare Tunnel：把一个域名指到 `http://localhost:18300`。经过 Tunnel 的请求带着 `Cf-Connecting-IP` 头，程序会把它当成公网请求，安全上没问题。
   - 用 Tunnel 的话，记得在设置里把“本页面的公网地址”改成 Tunnel 的域名，否则通行密钥用不了。
4. 来电在「设置」里选“全部拦截”，只留记录。

#### 电话也要，用 Tailscale（没实测）
思路是让手机通过 Tailscale 直接连家里，不经过公网：

1. 把 `SIP_DOMAIN` 的 A 记录指向这台机器的 Tailscale 地址（100.x.x.x）。公网 DNS 记录指向内网地址是允许的。
2. 证书没法用 HTTP 验证申请了，要换成 DNS 验证。见下面“80/443 被封”。
3. iPhone 上 Tailscale 要一直开着。推送唤醒是 Flexisip 主动往外连 linphone.org，这部分不受影响。

代价是手机耗电多一点，而且 Tailscale 一断，电话就进不来。

#### 电话也要，租一台 VPS 中转（没实测）
1. 家里机器用 WireGuard 连到 VPS。
2. VPS 上用 iptables 把 TCP 80、443、5061 和 UDP 10000-10099 转发到家里机器的 WireGuard 地址。
3. 域名指向 VPS。

麻烦的是通话声音：Asterisk 用 STUN 查到的是家里的出口地址，而不是 VPS 的地址。需要：
- 把 `asterisk/conf/rtp.conf` 里的 `stunaddr` 去掉；
- 在 `pjsip.conf` 的 transport 里加 `external_media_address = <VPS 公网 IP>`；
- 回程流量也要走 WireGuard：用策略路由，或者在 VPS 上做 SNAT。

这条路能走通，但要调试。

### 动态公网 IP

- 加一个 DDNS 容器自动更新 A 记录，比如 Cloudflare 可以用 `favonia/cloudflare-ddns`。
- Asterisk 每次通话都用 STUN 重新查公网地址，不用改。
- linphone.org 的推送密钥绑定在申请时的 IP 上。IP 变了之后，到设置页重新点一次“保存并测试”。

### 80/443 被运营商封了（国内家宽很常见）

分成证书和访问端口两个问题。

#### 证书改成 DNS 验证
官方 Caddy 镜像不带 DNS 插件，要自己做一个镜像。以 Cloudflare 为例：

```dockerfile
# caddy/Dockerfile
FROM caddy:2.11.4-builder-alpine AS build
RUN xcaddy build --with github.com/caddy-dns/cloudflare
FROM caddy:2.11.4-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
```

然后：
- `compose.yaml` 的 caddy 服务里，把 `image:` 改成 `build: ./caddy`，并加一个环境变量 `CF_API_TOKEN`（只给这个域名的 DNS 编辑权限）；
- Caddyfile 开头的全局配置里加一行 `acme_dns cloudflare {env.CF_API_TOKEN}`。

其它 DNS 服务商（阿里云、DNSPod 等）也有对应插件，见 caddy-dns 组织。

#### 网页换端口
`.env` 里设：

```sh
CADDY_HTTPS_PORT=8443
CADDY_BIND=0.0.0.0
```

路由器转发 8443，之后网页地址变成 `https://sms.example.com:8443`。下面这些地方也要带上端口：
- Bark 的服务器地址；
- 设置页里的“本页面的公网地址”。

Flexisip 的推送转发地址会自动带上这个端口，不用改。

5061 和 10000-10099 一般不会被封。

### 只有 IPv6

没试过。理论上 Caddy、Flexisip、Asterisk 都支持 IPv6，但 ICE 候选地址、各处的 ACL 都是按 IPv4 写的，估计要改不少地方。

## 手机

### 安卓

- 推送：Bark 只有 iOS 版，而代码里目前只实现了 Bark 推送。可选的办法：
  - 改 `app/main.py` 里的 `bark_push()`，换成 [ntfy](https://ntfy.sh)、Gotify 或者 Telegram Bot。它们都是一个 HTTP POST 就能发，改动不大。
  - 不要推送，只用网页看短信。
- 电话：
  - 安卓版 Linphone 应该也能用，linphone.org 的推送服务支持 Firebase。没实测。
  - 网页电话在安卓的 Chrome 里能用。

### 不想用 Linphone

任何支持 TLS 加 SRTP（SDES）的 SIP 客户端都能注册（用户名 `iphone`），比如 Acrobits Groundwire、Zoiper。

但锁屏唤醒靠的是 Flexisip 加 linphone.org 的推送，只对 Linphone 有效。换别的客户端，就只能在 App 开着的时候接电话，而 iOS 会很快把后台的 App 挂起。只用来往外打的话，网页电话就够了。

### 只要短信、不要电话

下面这些都可以不要：
- Flexisip 和 Linphone；
- 5061 和 10000-10099 端口；
- VoLTE 和模块的 USB 声卡。

启动时只起需要的服务：

```sh
sudo docker compose up -d asterisk app bark caddy
```

来电拦截设成“全部拦截”，这样来电会被直接拒接，只留记录。

## 模块

- EC25：跟 EC20 的 USB ID 一样，直接能用。
- EG25-G、EC21 等 EC2x/EG2x 系列：chan_quectel 支持，没实测。先用 `lsusb` 确认 ID，不是 `2c7c:0125` 的话，改 `host/77-quectel-sms-gateway.rules`。
- EC200、EC600 等其它 Quectel 型号：USB 接口的布局和 AT 指令集都不一样，没测过。需要：
  - 自己确认哪个是 AT 口；
  - 改 udev 规则里的接口号；
  - 确认模块有没有 UAC 声卡。
- SIMCom（SIM7600 等）和华为模块：chan_quectel 不是给它们写的，得换 Asterisk 的通道驱动，工作量不小。

怎么找接口号：

```sh
for t in /dev/ttyUSB*; do echo "$t $(udevadm info -q property $t | grep ID_USB_INTERFACE_NUM)"; done
```

然后用 picocom 挨个连，输入 `AT` 能回 `OK` 的就是 AT 口。udev 规则里，`02` 是给 Asterisk 用的口，`03` 是给状态页用的口。

状态页的模块详情只是锦上添花。读不到时状态页会显示提醒，不影响收发短信和电话。

## 运行的机器

- Debian、Ubuntu 等普通 Linux：最省心。
- 飞牛 fnOS：我自己用的就是这个，直接能跑。
- 群晖 DSM 7：DSM 7 去掉了很多 USB 设备驱动，USB 串口和 USB 声卡都可能要装社区驱动包；udev 规则也不一定能持久保存。这种情况下：
  - 把 `asterisk/conf/quectel.conf` 里的 `/dev/quectel-at` 直接写成 `/dev/ttyUSB2`；
  - 但模块重新插拔后编号可能会变。

  更省事的办法是在群晖上开一个虚拟机，或者另外找一台小主机来跑。
- PVE、ESXi 虚拟机：
  - USB 直通时按设备 ID（2c7c:0125）直通，不要按物理端口。模块重启后会重新枚举，按 ID 才能跟上。
  - 有些虚拟化平台转发 USB 声卡的等时传输效果不好，通话可能断断续续；短信不受影响。
- 树莓派等 ARM64 机器：没实测。
  - 用到的基础镜像（Alpine、Debian、Python、Caddy）都有 arm64 版本，bark-server 用之前确认一下。
  - Flexisip 在树莓派 4 上编译大概要一两个小时，建议加 swap，并把 `JOBS` 调到 2。

## 所在地区和号码

- 人在国内：卡不漫游，也一样能用。拨号规则不用改。
- 不在日本：
  - 改 `.env` 的 `TZ`；
  - 改 `asterisk/conf/extensions.conf` 的 `[outbound]`：把 `+81` 换成你所在国家的区号，或者干脆删掉，只留 `+86`。
- 不是中国大陆的卡：下面这些都是按中国大陆写的：
  - 营销短信规则；
  - 百度号码标记；
  - 号段归属地；
  - 拨号规则（`[from-iphone]` 里不带区号的号码都会加上 `+86`）。

  需要：
  - 改拨号规则；
  - 在设置里关掉“自动拦截营销短信”；
  - 来电拦截改成“只拦黑名单”或“全部拦截”。

  收短信、推送、接打电话本身不受影响。
