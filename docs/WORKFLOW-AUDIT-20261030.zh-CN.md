# 安装器跨平台模拟回归（2026-10-30）

范围为公共安装器的 Windows、Android 主机接口和 CLI，以及 E5/MU300 两种包适配。macOS App 不作运行验收。本次不枚举、连接或写入真实随身 WiFi，也没有安装、停止用户的控制端 App。

## 已复现并修复

1. **损坏记录影响整个首页/记录管理。** 非对象 JSON、缺少列表字段或非数值时间可导致异常。记录列表现在隔离这些条目，保留磁盘原件；旧的最小恢复元数据仍能读取和迁移，不因新增界面检查破坏旧记录。
2. **导出备份可能包含自己。** 导出目的地不能位于这条记录的目录内；同时拒绝将记录目录之外的链接文件混入备份。
3. **失败/并发导出留下不完整恢复 ZIP。** 使用独占创建，失败删除本次未完成输出，不覆盖已有用户文件。写入期间等待安全点；导出保存固定事务快照，并复核文件和关键写入状态，变化时不发布伪完整备份。Android 重置等待阶段仍允许导出，没有禁止所有活动任务的备份。
4. **新安装包校验失败仍保留旧包。** 开始校验先撤销旧的已选包状态，防止后续使用上一次的包。
5. **新 OTA 选择失败仍保留旧确认。** 开始选择先撤销旧补丁路径及应用许可；失败后旧 token 不能继续写入。保留正在运行的旧更新状态查询能力。
6. **异常协议消息结束 CLI 服务。** JSON 数组、空命令等返回错误响应后仍能处理下一条合法消息。
7. **Android 失败的文件/目录导入残留上传副本。** 每次文件选择独立持有一个临时目录，成功或失败后清理其拥有的上传目录；已发布安装包、去重缓存、恢复记录和用户原文件不在清理范围。导出到 Android 文档提供器失败时，已完成的导出文件仍保留供恢复。

USB 拔出后清理旧会话、重新接入后认证、保留无线连接的行为经测试原本正确，本轮未添加重复实现。未更改 NV 字节、AT 时序、分区算法、两家的 UFS 身份或机型风险确认规则。

## 功能覆盖

| 功能 | 模拟及离线验证 |
| --- | --- |
| 包目录/ZIP/摘要、中文及空格路径、损坏载荷、未知机型输入 yes | package_paths、release_archive、model_compat、family_integration、legacy_cli 和浏览器机型确认 |
| USB/无线 ADB、root/su、SSH、断线与授权、Windows MTP 范围 | android_host、wireless_adb、binary_io、windows_mtp；ADB/SSH 使用内存或 loopback 协议端点 |
| TF、内置分区、已有分区重装、实验镜像、临时启动、更新 | method_routing、state、recovery、flow_boundaries、update_lifecycle；缺卡不影响其他安装方式 |
| eMMC/UFS、两种 Android 槽位、保数据在线 F2FS 缩容、清空后备和续装 | storage_ufs、online_shrink、reclaimed_gap、android_growth_boot、真实临时文件系统测试 |
| 卸载归还空间、无旧记录的设备证据、日志回放与恢复 | uninstall 系列测试，模拟不同 GPT、加密/无加密、ext4/F2FS、重启与中断 |
| E5 工作 NV 保留、原机恢复输入 yes、MU300 不走 E5 NV 分支 | nv_recovery、family_integration、冻结更新/恢复测试 |
| 记录管理、导入/导出/删除、诊断和密码脱敏 | recovery、diagnostics、audit_regressions、Kotlin ExportTargets/ImportWorkspace |
| Diff、摘要、OTA 导出/检查/应用/撤销/删除恢复副本与重启 | ota 系列测试；本地临时系统文件和模拟 SSH，不改设备 |
| 新手引导、帮助、赞赏、主题、两语言、返回/重复导航、加载与错误 | 17 组生产前端浏览器流程，包括 40 组手机/平板页面与 80 组弹窗布局 |

## 最终结果

- Windows Python：506 项，83 项因平台/可选环境跳过，其余通过。
- WSL/Linux Python：506 项，2 项平台不适用跳过，其余通过。这里包含临时文件系统和设备 shell 脚本，不等于实机刷写。
- 前端 Vitest：10 项通过。此前 `npm test` 没有测试文件，本轮补了两平台桥接、文件选择取消、监听清理及异常对象处理。
- Android Kotlin/JVM：5 项通过；直接编译插件生产代码，测试导出目标及临时导入目录生命周期。
- Windows Rust：12 项通过，覆盖入口、包定位和原生桥接声明。
- 浏览器：17 组最终通过。旧帮助文本断言、移动模拟桥接漏接返回键已修正；新增重复导航和系统返回键的主页行为核验。
- 实际 Windows 冻结后端读取历史 E5、MU300 完整包：二进制备份、恢复续接、最终容量/设备身份和脱敏通过；MU300 hidden-area/TF/part/data-image 四种更新模式通过；两家 GUI/CLI 的 Android A/B 方向切换通过。
- Windows 后端和 Android ARM64 release APK 重新构建；源码指纹、文件摘要、APK 签名及 ELF 布局校验通过。没有重新打固件包，没有用旧 Mac App 冒充本轮产物。

## 日志与复跑

本机完整日志位于父仓库 `out/build-logs/installer-audit-*.log`；原生构建日志位于子模块 `out/build-logs/*audit*/command.log`。这些是本地报告，不把设备或签名资料提交进 Git。详细浏览器和冻结后端 JSON 保存在忽略的 `installer/verification/audit-*.json`。

按 `docs/BUILD.zh-CN.md` 设置两个可选固件源码路径，运行 Python unittest、`npm --prefix ui test`，再编译前端、运行 `python tests/run_ui.py`。可用 `--checks <名称...>` 复跑指定浏览器流程。Kotlin 测试入口为 Android 工程的 `:tauri-plugin-mobile-host:testDebugUnitTest`；Windows 原生测试为 `ui/src-tauri` 的 `cargo test --offline`，均复用现有构建缓存。

这些结果说明列出的模拟场景已通过，不是“穷尽所有缺陷”的保证。Android 的真实 USB 权限弹窗、厂商文档提供器、系统进程回收及真实刷写效果仍需独立实机验收；本轮没有擅自执行。
