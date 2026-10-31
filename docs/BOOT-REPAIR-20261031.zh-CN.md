# 在“临时启动已安装的 OpenWrt”内修复启动

## 已确认的 F50 故障

2026-10-31 通过无线 ADB 读取：Android 在 A，OpenWrt boot 在 B；8 GiB
`mu300root` 为 `/dev/block/mmcblk0p76`。B 槽内核能够启动，但 ramdisk 是
TF-only，`etc/mu300-root-target=sd`，没有 desktop 根模式或内置存储脚本。
pstore 明确记录找不到 TF、跳过内置区域、恢复 misc 并回 Android。

这是用户直接写入 `payload/boot-linux.img` 后的启动路径不匹配。
仅修改 misc 的切换器不能修复它；原始 payload 是安装器的基础输入，不能代替
为现有安装布局生成的最终 boot。Android A、GPT 和现有 OpenWrt 不需要重装。

## 交互与边界

不增加第二个修复入口，也不修改系统切换 APK／UFI 插件。

原“临时启动已安装的 OpenWrt”检查对侧 boot 的格式、ramdisk 和存储路径。
健康镜像保持原来的 misc-only 选择。损坏或布局不匹配时，在原确认页展示
boot 修复目标、现有布局、内核版本和数据影响；还可勾选“重建启动镜像”处理
头部未显出异常的启动失败。PC／Android 共用界面和后端，兼容 Python CLI
在同一菜单中请求明确确认。

自动修复覆盖可识别的内置独立分区与 TF 系统。有效的旧隐藏区域／userdata
镜像启动路径保持原有行为；不能从损坏镜像可靠确认这些旧布局时，不猜测
userdata 或未分配空间的位置，也不自动重装或格式化。

## 修复事务

1. 只读核对设备、动态 Android/Linux 槽位、现有系统标记和 ext4 身份。
   直接读取小范围元数据，不挂载根分区、不 fsck、不回放日志、不写 NV。
2. 使用完整包内校验过的启动输入；检查包内 Image 的内核版本与现有模块目录、
   实际模块 vermagic 对应。用户须选择与现有内核模块配套的包；版本检查不等于
   对任意自改内核和模块完成了完整 ABI 认证。
3. 在控制端生成匹配布局与实际槽位的 boot；确认计划后才进入写入。
   boot-chain check 必须先通过；不会借修复 boot 自动同步 BSP。
4. 只备份对侧 boot 和 misc 到本次安装记录，逐文件校验。不再为正常临时启动
   重复备份两槽整套 BSP，也不备份整机、rootfs 或 NV。
5. binary ADB 上传；复核上传摘要、当前 Android 槽位及写前摘要，仅写对侧 boot，
   fsync 后回读摘要。成功之后沿用原有 misc 32 字节试启动与重启流程。
6. 断线重试先检查同一事务的远程 worker。若实际已写完，仅接受摘要并继续；
   写入中断时保留原 boot 备份，基于同一已确认镜像续写，不改 rootfs。
   备份在安装记录管理器中导出／删除，远程载荷成功后清理。

## 验证与限制

- `test_boot_recovery.py`：TF 基础镜像误刷、空白 boot、A/B 互换、两家族、
  模块版本不符、系统标记变化、上传损坏、远程 worker 未结束、回复丢失及部分写入。
- `test_boot_switch.py`／`test_mu300_boot_switch.py`：正常路径仍只写 misc，
  boot-chain 校验失败不得启动修复、写 misc 或重启。
- `boot_repair_ui_smoke.py`：同一入口、中英布局、默认不强制重建、展示目标、
  确认前不可开始、没有额外修复入口。
- `frozen_boot_recovery.py`：使用 E5／MU300 成品包的实际重打包器，在冻结程序
  内生成两种 Android 槽位的修复 boot，验证原包 Image 字节不变与对应启动路径。
- F50 已完成真实设备只读规划：识别 TF 错误路径、内置 root 和 7.2.8 模块，
  成功生成修复计划；**本任务尚未授权刷入，不宣称硬件启动验收成功。**

本次不重新编译固件内核、不打完整固件包。Windows 工具单独重编；Android 和
macOS 后续发布必须各自重新构建，不能修改旧 artifact 指纹冒充新程序。
