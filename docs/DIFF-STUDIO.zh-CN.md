# OpenWrt Diff Studio

完整解压固件 ZIP，保留全部目录。

- Windows：双击根目录 `OpenWrt-Diff-Studio.exe`，无需另装 Python。
- Apple Silicon Mac：打开根目录 `OpenWrt-Diff-Studio.app`，无需另装 Python。
- Python 命令行：`python OpenWrt-Diff-Studio.py --help`，需安装 Python 和相应依赖。

Diff Studio 与安装器共用包内 `tools` 后端。不要单独复制 EXE 或 APP；
不需要再下载一份工具，也不重复包含 boot、rootfs 或另一套 Python 运行环境。

选择设备当前所用的旧完整发布包，再选择目标新完整发布包，点击分析。
可以拖入 ZIP，核对型号、版本、文件变更和冲突信息，再生成更新包。
默认文件名包含型号、系统版本及新旧发布标识，也可自己选择输出名称。

生成的包包含网页可导入的 `update.ota` 和电脑更新入口。
可以在 OpenWrt 网页“系统补丁”导入，也可以完整解压生成包后，
运行 `OpenWrt-Updater.exe` 或 `OpenWrt-Updater.app`，连接设备并更新。
更新保留用户插件、配置和数据；本地文件冲突按界面提示处理。
更新过程中保持设备供电和连接，完成后按页面提示操作。

备份与恢复副本可在系统补丁页面管理；删除恢复副本需要二次确认。
只导入可信来源、适用于当前机型和起始版本的补丁。
