# 参与开发

先阅读 README 和 AGENTS.md。后台依赖 Python 标准库；UI 为原生 HTML/CSS/JavaScript，电脑与 Android 复用同一份源码。不要为界面修改另建功能不一致的移动页面。

```sh
python -m unittest discover -s tests -v
python -m compileall -q superaimail
node --check superaimail/web/app.js
```

行为修改应有覆盖真实实现的回归测试。测试使用临时目录、合成邮件与隔离网络端点，不连接真实邮箱、不发送真实邮件。UI 修改应分别检查桌面/手机、明暗、键盘、表单失败和无 AI 状态。

提交使用 Conventional Commits，例如 `fix(mail): preserve attachment filenames`。PR 说明用户可见变化、验证结果和已知限制。不要附凭据或真实私人邮件。不要修改生成包来替代源码修改。

Windows 和 Android 构建见 [安装与构建](docs/INSTALL.md)。发布前核对版本、包内资源、签名、校验值和许可证。Android 后续版本必须沿用同一签名密钥；密钥只放 `.local-secrets/` 或 CI 的加密凭据存储中。
