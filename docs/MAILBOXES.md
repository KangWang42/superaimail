# 邮箱接入教程

SuperAIMail 通过 IMAP TLS 收件，通过 SMTP TLS / STARTTLS 发信。邮箱地址作为登录名；同一个邮箱的收件和发件使用同一授权身份。未知域名不会猜测服务器地址，请从邮箱管理员或服务商文档获取参数。

## 通用流程

1. 在邮箱网页端开启第三方客户端的 IMAP / SMTP 服务。
2. 按服务商要求生成客户端授权码或应用专用密码；Microsoft 使用下文的 OAuth。
3. 打开 SuperAIMail「设置 → 我的邮箱 → 添加邮箱」，填写完整地址和凭据。常见个人邮箱会自动填写服务器，仍可修改。
4. 点「测试收发连接」。测试登录，不发送邮件。收件成功而发信失败时，分别检查 SMTP 权限、端口和加密方式。
5. 保存后，后台开始索引近 60 天邮件。需要更多历史时，在该邮箱设置中补收近 180 天。

收件仅支持隐式 TLS，通常为 993。SMTP 的 465 选择 TLS；587 选择 STARTTLS。不要关闭证书校验来解决连接问题。

## 常见个人邮箱

| 邮箱 | IMAP / TLS | SMTP | 登录凭据 |
|---|---|---|---|
| QQ、Foxmail | `imap.qq.com:993` | `smtp.qq.com:465` TLS | QQ 客户端授权码 |
| 163 | `imap.163.com:993` | `smtp.163.com:465` TLS | 网易客户端授权码 |
| 126 | `imap.126.com:993` | `smtp.126.com:465` TLS | 网易客户端授权码 |
| yeah | `imap.yeah.net:993` | `smtp.yeah.net:465` TLS | 网易客户端授权码 |
| Gmail | `imap.gmail.com:993` | `smtp.gmail.com:465` TLS | 应用专用密码 |
| Outlook / Hotmail / Live | `outlook.office365.com:993` | `smtp-mail.outlook.com:587` STARTTLS | Microsoft OAuth |
| iCloud / me.com | `imap.mail.me.com:993` | `smtp.mail.me.com:587` STARTTLS | Apple App 专用密码 |
| Yahoo | `imap.mail.yahoo.com:993` | `smtp.mail.yahoo.com:465` TLS | Yahoo 应用密码 |
| AOL | `imap.aol.com:993` | `smtp.aol.com:465` TLS | AOL 应用密码 |
| Fastmail | `imap.fastmail.com:993` | `smtp.fastmail.com:465` TLS | Fastmail 应用密码，需套餐支持 IMAP |

预设只提供参数，账号权限、区域域名、套餐和组织策略仍以服务商当前说明为准。上表没有覆盖的邮箱也可以手动接入；新浪、搜狐的代码预设应在其网页帮助中再次核对，未作为实账号兼容性验证结果。

### QQ / Foxmail

登录 QQ 邮箱网页，点击右上角头像，进入「设置 → 账号与安全 → 安全设置」。开启 POP3/IMAP/SMTP 服务，点击「生成授权码」，按页面要求使用 QQ 密码或手机 QQ 扫码验证身份。在 SuperAIMail 中填生成的 16 位授权码。Foxmail 地址也使用 QQ 服务器，登录名填完整 Foxmail 地址。企业微信邮箱的服务器与个人 QQ 不同，应按企业邮箱的说明手动填写。

参考：[QQ 官方授权码教程](https://help.mail.qq.com/detail/106/985)。可以在「账号与安全 → 设备管理 → 授权码管理」撤销不再使用的授权码；界面入口以账号实际页面为准。

### 163 / 126 / yeah

登录对应网易网页邮箱，在设置的 POP3/SMTP/IMAP 或客户端授权管理中开启 IMAP/SMTP，按要求完成验证，生成客户端授权密码。将完整邮箱地址与客户端授权密码填入应用。个人邮箱与网易企业邮箱不共享一套服务器地址；企业邮箱请查管理员给出的配置。

参考：[网易邮箱帮助中心](https://help.mail.163.com/)。若网页端提示客户端登录被限制，先按网页安全验证处理，反复改应用密码不会解除服务商限制。

### Gmail

1. 在 Google 账号中开启两步验证。
2. 打开[应用专用密码](https://myaccount.google.com/apppasswords)，为 SuperAIMail 生成一个密码。
3. 在应用中选择授权码 / 应用密码方式，填完整 Gmail 地址和生成的密码。

本项目当前未提供 Google OAuth。Google Workspace 管理策略、高级保护或部分两步验证方式可能使应用密码不可用；这种账号不能通过填普通密码绕过限制。修改 Google 主密码后，原应用密码可能被撤销，需要重新生成。网络无法直连 Google 时，可在「网络出口」配置自有 SOCKS5 代理，并分配给这个邮箱。

参考：[Google 官方应用专用密码说明](https://support.google.com/accounts/answer/185833)、[Gmail 客户端设置](https://support.google.com/mail/answer/7126229)。

### Outlook / Hotmail / Microsoft 365

微软邮箱使用 OAuth；应用内不要求微软账号密码，也不使用 Client Secret。首次需要一个**你自己的 Microsoft Entra 公共客户端应用**：

1. 登录 [Microsoft Entra 管理中心](https://entra.microsoft.com/)，进入「应用注册 → 新注册」。
2. 个人 Outlook 与组织账号都要使用时，选择支持任意组织目录及个人 Microsoft 账号的账户类型。组织只允许单租户时，请先与管理员确认策略；当前应用 UI 默认使用 `common` 租户入口。
3. 在应用「身份验证」的高级设置中，将「允许公共客户端流」设为“是”。Device Code 流程不需要 Web 回调地址。
4. 在「API 权限」添加 **Office 365 Exchange Online 的委托权限** `IMAP.AccessAsUser.All` 与 `SMTP.Send`；需要管理员批准的组织由管理员授予。不是同名的 Microsoft Graph Mail 权限。
5. 复制概述页的「应用程序（客户端）ID」。不要创建或粘贴 Client Secret。
6. 在 SuperAIMail 添加邮箱，登录方式选择「Microsoft OAuth」，填 Client ID，点「登录 Microsoft」。
7. 打开给出的微软登录页，输入设备代码，在微软页面检查应用名称、邮箱与权限后授权。回到应用，看到登录成功后测试并保存。

应用申请 `offline_access` 以续期登录；令牌只保存在运行后台的设备，刷新令牌轮换后会更新本地凭据。授权失效时重新登录。微软账号设置中可能还需要启用 IMAP；Microsoft 365 管理员也可能禁止 IMAP、SMTP AUTH 或 Device Code 流程。OAuth 不会绕过这些政策。

参考：[微软 IMAP/SMTP OAuth 官方文档](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)、[Device Code 流程](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)、[Outlook.com POP/IMAP 设置](https://support.microsoft.com/en-us/office/pop-imap-and-smtp-settings-for-outlook-com-d088b986-291d-42b8-9564-9c414e2aa040)。

### iCloud

Apple 账号启用双重认证后，在[账号管理](https://account.apple.com/)的登录与安全中生成 App 专用密码。使用完整的 iCloud 邮箱地址和该密码接入，SMTP 选 587 / STARTTLS。`me.com` 等历史别名要以账号允许的身份为准。

参考：[Apple 官方邮件服务器设置](https://support.apple.com/en-us/102525)。

### Yahoo / AOL / Fastmail

在账号安全设置中为第三方客户端生成独立应用密码，不能直接假设网页登录密码可用。Fastmail 要求支持 IMAP 的套餐；仅支持网页与官方客户端的套餐不能使用本应用。地区邮箱、自定义域名和企业账号以提供商说明为准。

参考：[Yahoo IMAP](https://help.yahoo.com/kb/SLN4075.html)、[AOL 第三方客户端](https://help.aol.com/articles/how-do-i-use-other-email-applications-to-send-and-receive-my-aol-mail)、[Fastmail IMAP/SMTP](https://www.fastmail.help/hc/en-us/articles/1500000279921-IMAP-POP-and-SMTP)。

## 企业邮箱、自建邮箱与 Bridge

向管理员确认 IMAP 主机、TLS 端口、SMTP 主机、加密方式、完整登录名、第三方客户端授权和网络出口要求。当前应用使用完整邮箱地址登录，不支持与邮箱地址不同的自定义登录用户名，也不支持客户端证书登录或仅开放 EWS 的系统。

Proton Mail 等不直接提供公网 IMAP 的服务需要额外的官方 Bridge。当前 SuperAIMail 要求受信任证书的 IMAP TLS，不能直接连接只提供明文 IMAP 或自签证书的 Bridge；不要把“支持 IMAP”理解成已兼容任意 Bridge。本版本没有 Bridge 专用适配。

## 排错

| 现象 | 检查方向 |
|---|---|
| 连接超时、DNS 失败 | 运行后台的设备能否访问服务端口；代理是否正确；服务器是否封锁 SMTP 出站 |
| 收件认证失败 | 是否开启 IMAP；是否误填网页登录密码；授权码是否过期 |
| 收件成功、发信失败 | SMTP 端口和 TLS 方式；服务商 SMTP AUTH 权限；组织策略 |
| 证书错误 | 设备时间、服务器名称、证书链；不要关闭校验 |
| Microsoft pending | 授权尚未完成，回微软页面确认；Device Code 过期则重试 |
| 重复索引或状态不同 | 两台设备独立安装会独立保存状态；共用后台才能共享本地状态 |
| 手机锁屏后不再收件 | Android 可能暂停或结束应用；持续收件可使用在线共享后台 |

这些参数依据服务商文档整理；自动测试覆盖协议行为，不代表已用每个服务商的真实账号登录。报告问题时提供邮箱类型和去除个人信息的错误文字即可，不要提交授权码、令牌、邮件正文或数据目录。
