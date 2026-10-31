# 自托管与多端共用

SuperAIMail 是单用户工作台。多台设备共用一个后台时，也共用邮箱凭据、访问密码和所有邮件。不要用它向互不信任的用户提供多租户服务。

## 先在本机运行

```sh
git clone https://github.com/KangWang42/superaimail.git
cd superaimail
python -m superaimail --data ./data
```

打开 `http://127.0.0.1:8787`，设置至少 10 个字符的访问密码。CLI 的 `init` 也可初始化或重设访问密码，重设后旧会话失效。请使用设备磁盘加密保护数据目录；应用不会加密其中的邮箱凭据和邮件。

## HTTPS 共享服务

1. 在运行后台的设备上执行 `python -m superaimail init --data ./data`。
2. 将你拥有的域名解析到这台主机，配置有效的 TLS 证书。
3. 启动后台，并指定浏览器实际使用的完整来源地址：

```sh
python -m superaimail --data ./data --host 127.0.0.1 --port 8787 \
  --public-url https://mail.example.com --no-browser
```

4. 反向代理将整个站点转发到 `127.0.0.1:8787`，保留 `Host`。不要把后台端口另外暴露到公网。
5. 电脑或 Android 的「连接设置」填 `https://mail.example.com`；其他设备直接用浏览器访问。

公共地址只支持来源地址，不能带子路径。HTTPS 模式使用 Secure Cookie；若把公共地址写成 HTTPS，却通过 HTTP 浏览器访问，将无法保留登录。代理来源、Host 和公共地址应一致。不要用跳过证书验证的方式连接。

Caddy 示例见 [`deploy/Caddyfile`](../deploy/Caddyfile)：

```caddyfile
mail.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

替换域名再使用。按 Caddy 官方说明开放证书签发所需端口；本项目不会替你修改 DNS、防火墙或公网路由。

## Linux systemd

已有 Python 3.11+ 时，创建专用非登录用户 `superaimail`，把源码放 `/opt/superaimail`，数据放 `/var/lib/superaimail`，仅服务用户可读写数据。使用现有 Python 创建隔离环境并安装源码：

```sh
cd /opt/superaimail
python3 -m venv .venv
.venv/bin/python -m pip install .
sudo -u superaimail .venv/bin/python -m superaimail init --data /var/lib/superaimail
```

确认数据目录已由 `superaimail` 用户拥有，然后修改 [`deploy/superaimail.service`](../deploy/superaimail.service) 中的域名和路径，复制到 `/etc/systemd/system/`：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now superaimail
sudo systemctl status superaimail
```

本项目不自动安装系统 Python、Caddy 或 systemd。请先按你使用的发行版说明准备环境。日志用 `journalctl -u superaimail` 查看；分享前去除邮箱地址和私人信息。

## Docker Compose

需要已安装 Docker 与 Compose。仓库提供构建文件，不依赖尚未发布的镜像。

1. 创建 `data/` 和 `secrets/`。在 `secrets/workspace-password` 写入一个至少 10 字符的随机访问密码。文件不会提交到 Git。
2. Linux 上让 UID 10001 可写 `data/`、可读密码文件，例如 `chown -R 10001:10001 data secrets` 后设置最小权限。该操作只针对你新建的这些目录。
3. 在当前终端设置 `SUPERMAIL_PUBLIC_URL=https://mail.example.com`，或在未提交的 `.env` 中填写该变量。
4. 运行 `docker compose up -d --build`。默认仅将端口发布到主机 loopback，由主机上的 TLS 反代接入。
5. 用 `docker compose logs --tail 50` 检查启动情况，再从公共域名登录。

密码文件只用于首次初始化，不会覆盖已有访问密码。忘记访问密码时，停止服务，通过 CLI `init` 使用同一个数据目录重设。Docker 模板没有在本工作环境中运行 Docker 验证，部署后仍需按实际主机检查权限、反向代理和收件连接。

## iPhone / iPad 与 PWA

Safari 打开 HTTPS 服务，登录后选择「分享 → 添加到主屏幕」。Android 浏览器也可安装网页应用。PWA 仍连接该后台，不会在 iPhone 上独立运行 Python，也不提供离线收件或锁屏持续推送。

网页只把主题选择保存在本地；邮件数据不写入离线 Service Worker 缓存。主动退出会撤销所有浏览器会话。使用共享设备时退出，并按浏览器自身的隐私设置清理浏览数据。

## 备份、升级与回退

先停止后台，备份**整个数据目录**和当前版本，再升级应用。它包含 `access.json`、邮箱设置、AI Key、SQLite 数据库和附件目录；不要只复制数据库而漏掉附件。每次只让一个后台写入同一数据目录，不要放到多机同时写入的同步盘。

升级后先检查登录、邮箱列表、旧邮件阅读与任务附件，再继续使用。需要回退时，停止新版本，恢复对应旧版程序与同次备份。Android 卸载会清除应用私有数据，当前没有应用内全量备份导出；需要可靠备份和跨设备迁移时，推荐共享后台。Windows 卸载保留个人数据目录。

## 安全边界

仅 TLS 保护传输，凭据与邮件仍以可用形式保存在后台设备；备份同样敏感。不要把数据目录、Android 签名密钥、API Key 或日志中的私人内容上传到 GitHub。应用过滤邮件 HTML 并隔离显示，但下载的附件仍是外部文件，应按操作系统的方式审查后打开。AI 不自动执行邮件中的命令，也不会自动确认发送。
