# Ubuntu 22.04 + 已有 Nginx，使用公网 IP 访问

## 当前默认：直接访问 18080，不使用 Nginx

安装脚本默认监听 `0.0.0.0:18080`，不安装或修改 Nginx 配置。

```bash
cd /root/clf/wechat/wechat-vote-mvp
bash deploy/install-ubuntu.sh
```

访问 `http://公网IP:18080/`（不加 `/vote/`）。阿里云安全组和已启用的系统防火墙须允许 TCP 18080。密码与数据库位置保持不变。

已经安装旧版服务时，可以直接更新两份 service 文件并重启，无须重新上传全部项目：

```bash
cd /root/clf/wechat/wechat-vote-mvp
sed -i 's/--host 127\.0\.0\.1/--host 0.0.0.0/; s/ --trust-proxy//g' deploy/wechat-vote.service /etc/systemd/system/wechat-vote.service
systemctl daemon-reload
systemctl restart wechat-vote
ss -lntp 'sport = :18080'
```

下文是后续接入 Nginx 的步骤。届时先将 service 的启动参数改回 `--host 127.0.0.1` 并加上 `--trust-proxy`，重载、重启服务，再执行 Nginx 相关步骤。此前已运行旧版安装脚本的服务器已有 Nginx snippet；新版安装脚本不再自动安装它，需要时执行：

```bash
install -d /etc/nginx/snippets
install -m 644 deploy/nginx-vote.conf /etc/nginx/snippets/wechat-vote.conf
```

## 后续可选：Nginx 反向代理

源码目录：`/root/clf/wechat/wechat-vote-mvp`。对外地址：`http://公网IP/vote/`。

这里的 `root@iZ...` 是 SSH 用户和主机名，不是手机可访问的公网 IP。公网 IP 请在云服务器控制台查看。`(base)` 是 Conda 环境；服务使用 Ubuntu 的 `/usr/bin/python3`，不依赖 Conda。

## 1. 上传项目

把本项目的 `server.py`、`app.js`、`vote.html` 和整个 `deploy/` 目录上传到上述目录。可以上传 README 和测试，但不要上传本地测试的 `votes.sqlite3`、`__pycache__`。正式库将独立保存在 `/var/lib/wechat-vote/votes.sqlite3`，更新代码不会覆盖它。

## 2. 安装后台服务

在 Ubuntu 服务器上运行：

```bash
cd /root/clf/wechat/wechat-vote-mvp
bash deploy/install-ubuntu.sh
```

若提示缺少系统 Python，先运行 `apt-get update && apt-get install -y python3`，再执行脚本。服务器已有 Nginx，无需重复安装。安装脚本不会更改或重载现有 Nginx 站点。

脚本生成固定随机管理员密码并保存于 `/etc/wechat-vote.env`（仅 root 可读），重启或重复安装不会更换密码。查看密码：

```bash
cat /etc/wechat-vote.env
```

后端仅监听 `127.0.0.1:18080`。systemd 负责开机启动和异常重启。因为源码位于 `/root`，服务按指定目录以 root 身份运行，并限制文件写入；如需改为独立服务账户，应同时将源码迁到该账户可读取的目录并修改服务配置。

## 3. 接入现有 Nginx

先查看现有配置，找到公网 IP 请求实际进入的 `server { ... }` 块（通常是对应端口的默认站点）：

```bash
nginx -T
```

在这个 `server` 块内部加入一行，不要放进 `http` 块或现有 `location` 块：

```nginx
include /etc/nginx/snippets/wechat-vote.conf;
```

如果该站点已有 `/vote/` 的 location，请合并该段，不要重复定义。不要覆盖整个 Nginx 配置，也不要删除现有站点。如果现有站点将全部 HTTP 请求重定向到域名，需先确认 IP 请求的路由，不要直接套用。

检查通过后重载：

```bash
nginx -t && systemctl reload nginx
```

手机访问 `http://公网IP/vote/`。阿里云安全组及已启用的系统防火墙需要允许 Nginx 对外监听端口（通常为 TCP 80）。后端 18080 不需要对公网开放；如果当前站点监听其他端口，访问地址也须带该端口。

这里先提供 IP + HTTP 的接入配置。HTTP 不加密投票码和密码；正式活动需要在 Nginx 配置适用于访问地址的 HTTPS 证书。项目不包含证书，也没有自动更改现有 TLS 配置。

Nginx 配置保留原始 Host，使页面身份校验通过；覆盖 `X-Real-IP` 后由后端读取访客 IP，避免所有用户被当成一个代理 IP 限流。如果 Nginx 前面还有 CDN 或负载均衡，需要另行配置可信代理的 real_ip，不要直接信任用户提供的转发头。

## 4. 检查与维护

```bash
systemctl status wechat-vote --no-pager
journalctl -u wechat-vote -n 100 --no-pager
curl http://127.0.0.1:18080/api/status
curl http://公网IP/vote/api/status
```

打开“管理”页，使用 `/etc/wechat-vote.env` 中的密码登录，录入至少三队成员，分发各自投票码，最后开放投票。

更新代码后运行 `systemctl restart wechat-vote`。修改 `/etc/wechat-vote.env` 中的密码也需重启；重启会让原管理员会话失效，不影响成员投票码或已提交选票。

备份先停止服务，再复制数据库，完成后启动：

```bash
systemctl stop wechat-vote
cp -p /var/lib/wechat-vote/votes.sqlite3 "/var/lib/wechat-vote/votes-$(date +%Y%m%d-%H%M%S).sqlite3"
systemctl start wechat-vote
```

不要删除或覆盖正式数据库。开始新一轮活动需使用新的数据库文件，同时保留旧库。

配置参考：[Nginx 代理请求头](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[systemd 执行环境](https://www.freedesktop.org/software/systemd/man/250/systemd.exec.html)。
