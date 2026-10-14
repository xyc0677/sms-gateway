# 部署时容易踩的坑

大部分是我自己搭的时候真遇到过的，按部署顺序排。

## 模块

### `lsusb` 能看到模块，但没有 `/dev/ttyUSB*`
内核的 `option` 驱动没认这个设备。先试：

```sh
sudo modprobe option
echo 2c7c 0125 | sudo tee /sys/bus/usb-serial/drivers/option1/new_id
```

还没有的话，多半是 NAS 系统的内核没编这个驱动，只能换系统或者换机器。参考 [environments.md](environments.md) 里关于 NAS 的部分。

### `quectel show devices` 显示 `Not connected`，或者 State 一直不是 Free
按这个顺序查：

1. `ls -l /dev/quectel-at`：这个链接还在不在？模块重新插拔或者重启后，ttyUSB 的编号可能会变，udev 规则会把链接重新指好，但前提是规则装上了。
2. ModemManager 是不是又把串口抢走了：`sudo lsof /dev/ttyUSB*`，除了 asterisk 不应该有别的进程。udev 规则里的 `ID_MM_DEVICE_IGNORE` 只有在 ModemManager 启动前就生效才管用，最省事的办法是 `sudo systemctl disable --now ModemManager`（不过别的设备如果要用它，就不能这样做）。
3. 有没有别的程序也开着同一个串口，比如之前调试用的 picocom 没退出。

### 模块重启后有一会儿收不到短信
正常现象。模块重新注册网络要一两分钟，漫游时可能更久。chan_quectel 会自己重连。

### `arecord -l` 里没有 Quectel 声卡
- 打开 UAC 的那条 AT 命令没执行，或者执行后模块没重启。
- 也可能是系统内核没有 `snd-usb-audio` 模块：`lsmod | grep snd_usb_audio`。很多 NAS 系统都没带，这种情况下短信照样能用，只是打不了电话。

### 状态页 VoLTE 显示“不可用”
在日本、美国这些已经关掉 2G/3G 的地方，没有 VoLTE 就完全打不了电话。能不能用取决于三件事：模块固件、MBN 配置、你的运营商和当地网络有没有 VoLTE 漫游协议。

可以先查这几个：

```sh
sudo docker compose exec asterisk asterisk -rx 'quectel cmd quectel0 AT+QCFG="ims"'
sudo docker compose exec asterisk asterisk -rx 'quectel cmd quectel0 AT+QMBNCFG="list"'
```

返回结果只会出现在 `docker compose logs asterisk` 里。具体怎么开，要看 Quectel 的 IMS 应用手册。

另外注意，状态页的数据是从模块的 USB 接口 03 读的，所以模块的 USB 组合要和 EC20/EC25 默认的一样。

## 证书和域名

### Caddy 一直申请不到证书
- 先确认从外网能访问 80 端口。可以用手机关掉 Wi-Fi，打开 `http://sms.example.com` 试试。
- 家用宽带的 80 和 443 被运营商封了，在国内很常见。见 [environments.md](environments.md) 里的“80/443 被封”。
- 开了 Cloudflare 代理（橙色云）时，“始终使用 HTTPS”之类的跳转设置有可能让证书验证失败。最省事的排查办法是先关掉橙色云，等证书申请下来再打开，之后续期一般不受影响。
- 短时间内失败太多次，会被 Let's Encrypt 限流，要等一个小时左右。

### Flexisip 一直显示 `waiting for .../sip.example.com.crt`
sip 域名的证书还没申请下来，看 `docker compose logs caddy`。最常见的原因是 sip 域名在 Cloudflare 开了代理。

### 手机在外面能连上，在家里 Wi-Fi 下连不上
两种可能：
- 路由器没有 NAT 回环，看 README 里“家里连不上、外面正常”那一节。
- IPv6：iPhone 会优先用 IPv6。如果域名有 AAAA 记录，但这台机器其实收不到 IPv6 流量（有些路由器会在内网丢掉 IPv6），就会连不上。没把握的话，删掉 AAAA 记录。

## 网页

### 第一次设置密码提示“只能在局域网里进行”
你是通过域名打开的。要用 `http://<LAN_IP>:18300` 打开。通过 Tailscale 的 100.x 地址打开也行。

### 加不了通行密钥
通行密钥绑定在公网域名上，只能在 `https://sms.example.com` 下添加和使用，用 IP 打开时这个按钮不会出现。

### 页面更新后看起来没变化
iPhone 主屏幕上的网页 App 会缓存。从后台划掉再打开。

## Bark

### 测试推送提示成功，但手机没收到
- 在 Bark App 里看这个 key 属于哪个服务器。在官方服务器下复制的 key，拿到自建服务器上是用不了的，反过来也一样。
- 同一台手机在不同服务器下的 key 是不同的。
- 网关给每台设备单独发，失败的设备会在测试结果里写出来。

### 收到的推送是一串乱码
设置了加密密钥，但这台设备的 Bark 没开加密，或者填的密钥不一样。

### 拦截通知收不到提醒
拦截记录本来就是静默通知：不响、不亮屏，只进通知中心。这是故意这么设计的。告警和普通短信会响。

## Linphone 和通话

### 来电时 Linphone 显示失败，日志里是 488 Not Acceptable
Linphone 的媒体加密没设成 SRTP。网关强制 SRTP，因为声音要在公网上传。

### Linphone 能注册，但来电一响就挂，或者对方听到忙音
- 在「设置 → 来电拦截」里看是不是被拦了，拦截页有记录。
- 不是拦截的话，看 `docker compose logs asterisk` 里 Dial 的结果。CONGESTION 通常是 Flexisip 没有这台手机的注册信息，比如 Flexisip 刚重启过，打开一次 Linphone 就好了。

### 锁屏时不响，打开 Linphone 时才响
推送没走通：
1. 网页设置里有没有显示“推送密钥正常”。linphone.org 的推送密钥绑定在申请时的公网 IP 上，家里换了 IP 之后，到设置页重新点一次“保存并测试”。
2. Linphone 的通知权限和推送开关有没有打开。
3. `docker compose logs flexisip | grep -i push`，看有没有发出推送。

### 接通了但没声音，或者只有一边有声音
- UDP 10000-10099 没转发，或者被防火墙挡了。这是最常见的原因。
- `.env` 里的 `LAN_IP` 写错了。在家用 Wi-Fi 时，声音是按这个地址走的。
- 先用网页拨 `*43` 回声测试：
  - 回声测试有声音、Linphone 没有：问题在 Linphone 的网络或设置。
  - 回声测试也没声音：问题在服务器端口。

### 能打进来，打不出去
拨出时号码会被统一转成国际格式（+86…）。只有 +86 和 +81 开头的号码能拨出，其它会被拒绝，日志里会有一行 `Refused outbound call`。

### 改了 PBX_TOKEN 之后来电不响

asterisk 和 flexisip 两个服务都要重启，暗号才能对上：`sudo docker compose up -d --force-recreate asterisk flexisip`。

### 启动时报 `... is missing or shorter than 16 characters`

`.env` 里有密钥没填，或者太短。用 `openssl rand -hex 24` 生成一个填进去。

## 构建

### Flexisip 编译到一半机器卡死，或者被 OOM 杀掉
在 `flexisip/Dockerfile` 里把 `JOBS` 调小：

```sh
sudo docker compose build --build-arg JOBS=2 flexisip
```

内存 2GB 以下的机器建议加点 swap，或者在别的电脑上 build 好，用 `docker save` / `docker load` 拷过来。

### `docker compose build` 时 git clone 失败
构建时要从 GitHub（chan_quectel）和 gitlab.linphone.org（Flexisip）下载源码。国内网络可能需要给 Docker 配代理。
