# SuperAIMail 项目状态

## 范围与规则

独立的单用户多邮箱与 AI 工作台。Windows、Android 可运行同一 Python 后台与响应式 UI，也可连接一个 HTTPS 共享后台。AI 默认关闭，IMAP 只读，发送必须预览并确认。原运维服务独立保留，不从中导入任何真实账号、邮件、私有接口或运行数据。

源码唯一来源为本仓库 `superaimail/`。Android Gradle 直接使用该目录；Windows 打包从该目录取资源。不得手改生成的 EXE/APK。运行数据、构建缓存和签名密钥均被 Git 忽略。

## 当前进展

- 0.1.0 源码与教程已具备：多邮箱、Microsoft Device Code OAuth、本地邮件状态、纯文本发送、规则、收集任务、兼容 AI 接口、PWA、共享服务。
- README 按产品用途、三步开始、真实界面图解、教程入口和限制编排；配图来自合成邮件的实际浏览器渲染。
- Windows 原生窗口已打开检查；最终安装器通过隔离安装、覆盖安装、冻结后台启动、教程与许可资源检查、卸载及数据保留验证。ZIP 和 Android APK 均已构建。
- 浏览器覆盖 20 个场景，桌面 1440、手机 390、边界 320/430、明暗、邮箱表单、空搜索、发送预览、AI 摘要与任务草稿。无脚本异常、HTTP 失败、横向溢出；DevTools 的 5 条取消记录均对应 HTTP 304，保留原始事件并单列说明。
- Python 测试 104 项通过，包含未知域名必须手动提供 IMAP 主机、关闭邮箱不建立连接、列表 ETag 复用及修改后失效的回归；compileall、JavaScript 语法与静态页面检查通过。README 内部链接、源码敏感内容规则和关键颜色对比度检查通过。
- Android APK 的签名与页对齐已校验。当前主机模拟器在关闭硬件加速后仍未于 5 分钟内启动，未执行设备测试；已有 Android instrumentation 测试源码，等待可用的隔离设备。
- GitHub 仓库已公开。Windows / Ubuntu × Python 3.11 / 3.13 的四组 CI 全部通过，包含 104 项测试与语法检查。Linux 首轮发现概览包含实时状态，已将缓存断言改到稳定的邮件列表，并验证数据变化后旧 ETag 失效。

## 当前限制

尚无真实邮箱、商业模型或 Android 真机连通验收；OAuth 验证使用隔离响应，模型协议验证使用本机 HTTP 服务。Docker 模板未在本机运行。发信不支持附件与富文本、不上传服务商已发送文件夹；本地状态不回写 IMAP。手机不提供持续保活、推送和全量备份导出。iOS 通过在线共享后台/PWA 使用。

## 仓库与发布入口

源码与教程：<https://github.com/KangWang42/superaimail>。0.1.0 首个公开预览的发布入口：<https://github.com/KangWang42/superaimail/releases/tag/v0.1.0>，提供 Windows 安装器、便携 ZIP、Android APK 与 SHA-256 清单。上述设备与真实服务验收限制继续适用。
