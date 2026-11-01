# 上游 MU300 布局：仅卸载与空间归还

## 核对依据

2026-11-01 核对 `dikeckaan/mu300-linux` main，HEAD
`56f3c1c86c84e567320bb8d434b8a48303c37d4f`：

- [tools/storage.sh](https://github.com/dikeckaan/mu300-linux/blob/56f3c1c86c84e567320bb8d434b8a48303c37d4f/tools/storage.sh)
  的 region_probe/region_in_bounds：userdata 后未分配区域，起点为下一个 2 MiB 边界；
  历史固定起点为 27762098176 字节。系统范围不与 GPT 分区、备份表重叠。
- [boot/init](https://github.com/dikeckaan/mu300-linux/blob/56f3c1c86c84e567320bb8d434b8a48303c37d4f/boot/init)：
  EXT4 标签 `mu300root`；系统在 `openwrt` / `openwrt-luci` 等子目录，
  `/.mu300/boot-os` 选择系统。也支持 GPT 中的同名 `mu300root` 分区。
- [tools/android-install.sh](https://github.com/dikeckaan/mu300-linux/blob/56f3c1c86c84e567320bb8d434b8a48303c37d4f/tools/android-install.sh)
  与 android-mount-mu300root.sh：带偏移和长度的 loop，EXT4 标签仍为 `mu300root`。

这通常是文件系统标签，不是新添了一个叫 rootfs 的 GPT 分区。

## 本次范围

仅 `Adapter.probe(..., 'uninstall')` 启用新的裸区域发现。
安装、重装、更新和临时启动不进入此发现路径。独立未分配空间扫描只补充
裸区域的占用识别，避免将已有系统误当空闲；删除仍须走明确确认的卸载入口。
E5 适配器不识别 MU300 裸区域；没有修改两个家族的安装布局或 NV 策略。
桌面、Android 与 Mac 使用同一后端和卸载确认页，不另写平台安装脚本。

支持上游 OpenWrt / OpenWrt-LuCI 的同名 GPT 分区和无 GPT 条目的裸区域。
Ubuntu、缺少系统标记、多个候选和不认识的布局不冒充 OpenWrt 自动移除。

## 检测、确认和执行

1. 校验本机存储身份、主备 GPT、Android 当前槽；只读两个上游约定的起点，
   不扫描整个磁盘。完整 EXT4 标签必须恰好为 mu300root，不能接受 mu300rootfs。
2. 核对备用 Linux boot 与 init 的上游区域查找逻辑；按 EXT4 inode/extent 读取
   boot-os、对应 OpenWrt release。没有挂载文件系统，没有读取用户业务文件。
3. 内核布局、实际文件系统大小与 GPT 外的区域边界必须相符；已有 loop 或映射
   仍占用时不能删除。需要日志回放时，只对已验证区域建立指定 offset/size 的临时
   loop，执行原厂 e2fsck -p -E journal_only，随后解除并重新校验身份与标记。
4. 确认页明确显示裸区域、磁盘偏移、将删除该区域内所有系统/插件/文件、Android
   目标容量及是否保数据。只有用户确认后才进入备份和离线写入。
5. 裸区域使用版本4事务：原 GPT 没有 root 条目，不制造虚假条目。仅扩大 userdata
   到已证明的文件系统末尾；包含其前面的对齐间隙，不自动吞并末尾其他未分配区域。
   同名 GPT 分区继续使用原来的受限移除规则。
6. 离线运行时复核存储身份/GPT/启动备份，核对原4 KiB头的摘要，清除并回读该头，
   再按“备份 GPT → 主 GPT”的顺序写入和回读。重试接受同一个已清零头；未知头拒绝写入。
7. Android 文件系统扩容复用现有保数据流程，包括加密 F2FS 的原厂 Android 路径。
   不支持保数据时仍需单独确认清空；运输故障不转换成格式化许可。回到 Android 后
   核验容量/UUID并完成启动槽和临时文件清理。

## 离线验证边界

覆盖普通分区/裸区域、A/B 两槽、512/4096逻辑扇区、实际 EXT4 元数据解析、
已有 loop、错误标签/标记/boot、多个候选、缺失读取、伪造更大区域及导入事务篡改。
完整 BusyBox worker 在临时稀疏文件上验证 GPT 双份写入、4 KiB头清除、重启后续接、
错误摘要停止以及 Android/NV 哨兵保留。没有连接或刷写实机。

本轮 Windows 单元测试592项（95项平台/环境相关跳过）；Linux 卸载与扩容114项
（1项环境相关跳过），包括真实 BusyBox 文件写入；TypeScript/Vite构建通过。

源码回归不等同于原生二进制重编。发布前须重编安装器各原生平台及 APK，
再更新固件包的安装器交付件；不能把旧 EXE/APK 当作已具备此功能。
