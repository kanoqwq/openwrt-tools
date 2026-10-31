# Windows 深层解压目录导致 SSH 依赖加载失败

## 现场与确认范围

2026-10-31 F50／MU300 安装器诊断：临时启动的 `misc-trial` 已完成并输出
`MISC-TRIAL-VERIFIED`，ADB 重启命令成功返回。随后电脑端
`Service.wait_openwrt()` 首次导入 Paramiko 时，`_cffi_backend` 加载失败，
提示“文件名或扩展名太长”。重试仍停在本机导入步骤。

这份日志不能证明设备已经进入 OpenWrt，也没有提供设备分区或启动失败的证据。
不应因此重新安装系统或重复写 misc。

## 可重复复现

本机 Windows 11／Python 3.12.6／PyInstaller 6.20.0，引用同一份现有冻结后端，
只使用目录 junction，不复制依赖缓存：

- 普通路径中的后端 EXE 完整路径长度 224 字符时，SSH 和 AES 导入成功。
- 长度 228、232、236、240、244 字符时，EXE 能启动，但较长的
  `_internal/_cffi_backend.cp312-win_amd64.pyd` 路径触发相同异常。
- 同一批路径使用 Windows verbatim 可执行文件路径启动时，SSH 和 AES 全部成功。

本机已经启用 LongPathsEnabled，仍可复现；不需要用户修改注册表。

## 修复

1. Rust 启动器对后端可执行文件使用 `canonicalize()`；Windows 返回带
   `\\?\`（网络路径为 `\\?\UNC\`）前缀的路径。PyInstaller 据此初始化
   `_MEIPASS`，避免后续扩展 DLL 仍受普通路径长度限制。
2. 所有安装／更新／临时启动事务在缩容、备份和切槽之前，先加载本机 SSH
   依赖并实际初始化 AES。失败归类为 `host_runtime_unavailable`，保留底层原因，
   明确提示完整解压最新版到较短目录重试。
3. Android 卸载流程不依赖 SSH，不增加无关依赖检查。已经完成 misc 写入的
   事务在重试时仍只进入 OpenWrt 验证，不能重放写入。

## 验证入口

- `python -m unittest discover -s tests -p test_ssh_runtime.py -v`
- Windows 原生 Rust：`cargo +1.90.0 test --manifest-path ui/src-tauri/Cargo.toml --bin openwrt-installer`
- 重编 Windows artifact 后运行 `tests/frozen_windows_paths.py --backend
  artifacts/windows/tools/backend/openwrt-backend.exe --work <已有 work 路径>`，
  实际加载长路径中的冻结 SSH 与加密库，覆盖中文与空格。
- `tests/frozen_flow_smoke.py` 可使用新冻结后端对既有 E5 和 MU300 完整包进行
  模拟传输、事务恢复、启动结果检查和诊断测试。

上述回归均不连接设备，不等同于 F50 的硬件启动验收。旧发布包不会自动改变；
应在关闭旧安装器后更换完整 Windows GUI／后端交付件，保留原安装记录。
