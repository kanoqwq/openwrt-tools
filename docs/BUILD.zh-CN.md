# Agent 本机构建指导

## 工作区与边界

本机 Windows 固件仓库为 D:\Git\e5-linux 与 D:\Git\mu300-linux-clean-tf-7.2；两边 installer/ 都是本仓库子模块。已有 Node/Cargo/Gradle/Python/SDK 缓存不复制、不迁移。优先在已有缓存的 E5 installer/ 子模块构建一次，两边复用同一份经 sources() 校验的桌面交付件。

Linux/WSL 唯一工作区 /home/kano/openwrt-build。独立仓库通过 tools/build-release.py run 使用同一个 locks/openwrt-release.lock；从固件根目录构建时也可沿用父仓库工具。不要同时嵌套调用两个 runner。产物 artifacts/，正式独立工具发布 out/releases/，日志和状态 out/build-logs/ 与 out/build-state/。

## 离线验证

有线 Android ADB 的确认执行流程在设备身份和 root 检查后读取
`sys.usb.config`，只移除其中的 `mtp`，保留现有 USB 网络、ADB 及其他功能；
必要时补上 `adb`。例如 `ecm,mtp,adb` 变为 `ecm,adb`，不会强制改成 RNDIS。
没有 MTP 时不写属性。仅修改当前组合，不写持久属性。
切换后最多等待 120 秒让 ADB 恢复，核对原序列号、同次启动身份和 root，
再开始备份／安装。MTP 仍保留、USB 属性未更新或顺序不同均不阻塞，不等待
属性与请求组合完全相等。现有 ECM／NCM／RNDIS 或厂商网络功能原样保留，
不会固定选择 RNDIS。Android 控制端同时丢弃旧 USB 句柄，按系统要求重新授权。
无线 ADB、设备枚举和查看计划不改变 USB 配置；续接进行中的离线写入或
已启动的在线缩容时跳过此步骤，避免干扰维护进程。
旧 `windows_mtp.py` 仅保留为空兼容入口，不再禁用 Windows 的 MTP 设备节点。

```powershell
$env:OPENWRT_E5_SOURCE='D:/Git/e5-linux'
$env:OPENWRT_MU300_SOURCE='D:/Git/mu300-linux-clean-tf-7.2'
C:\Python312\python.exe -X utf8 -m unittest discover -s tests -v
npm --prefix ui run build
```

省略两个源码变量时，核心测试仍可运行；需要对应固件入口的集成测试会明确跳过。不要把 skip 数量算成通过。POSIX 设备脚本和临时文件系统测试在 WSL 执行，不连接真实设备。

## Windows

在 WSL 仓库目录用固定 runner 调用本机 PowerShell。已有本机 Python C:\Python312\python.exe、Node/Rust 环境；ADB 输入使用已有 platform-tools，不重复下载。

```sh
python3 tools/build-release.py run --workspace /home/kano/openwrt-build --label windows -- /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command 'Set-Location D:\Git\e5-linux\installer; $env:OPENWRT_TOOLS_WORK="D:/Git/e5-linux/work"; & C:\Python312\python.exe build.py --adb-dir D:/Git/e5-linux/work/installer-platform-tools/platform-tools; exit $LASTEXITCODE'
```

仅当已核实当前 UI/Rust 源码与现有二进制一致才能使用 --skip-ui。sources()、二进制摘要和运行资源必须匹配，不得手改 build.json 冒充重编。

## Android ARM64

Android 的耗时操作使用前台服务与 CPU 唤醒锁，并在 App 前台显示时保持屏幕常亮。
安装、更新、卸载的保护跟随后台执行器，在清理完成或安全暂停返回后释放；
导入、分析、导出等同步任务按独立租约管理，不会提前释放仍在运行的安装任务。
页面／Activity 重建后会恢复当前任务的屏幕保护。用户主动锁屏或切到后台时，
Android 仍可关闭显示，后台 CPU 保护和操作通知继续保留；本功能不修改系统休眠设置。

复用 SDK D:/Android/Sdk、NDK27.0.12077973、既有 JDK21、Python3.13 与 Gradle/Rust 缓存。

```sh
python3 tools/build-release.py run --workspace /home/kano/openwrt-build --label android -- /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command 'Set-Location D:\Git\e5-linux\installer; & C:\Python312\python.exe build-android.py --sdk D:/Android/Sdk --jdk C:/Users/kano/.gradle/jdks/eclipse_adoptium-21-amd64-windows.2 --build-python D:/Android/Python313-portable/python.exe --signing-config C:/Users/kano/.android/openwrt-tools-signing.json; exit $LASTEXITCODE'
```

签名配置与密钥在仓库外，密码不写命令行或 Git。保留现有证书轮换 lineage，保证覆盖升级身份一致。只在用户确认控制端空闲时安装 APK；构建不等于实机安装/分区验收。

## Apple Silicon macOS

正式包附带 OpenWrt-Installer-macOS.command；Diff Studio 和其导出的更新器
分别附带对应的 -macOS.command。用户完整解压并确认来源可信后双击，
只修复该 App 和相邻 tools/macos 的权限、移除 com.apple.quarantine，
再打开 App。脚本不改全局 Gatekeeper、不使用 sudo、不重签或修改二进制内容。
ZIP 生成器强制保留这些入口的执行位，Windows 组包也相同。
这不是 Apple Developer ID 签名或公证；系统若连脚本也拦截，仍需用户在
“隐私与安全性”允许此入口一次，不能承诺无证书时自动获得系统信任。

用户没有本地 Mac Agent。macOS App 只能使用真实 Apple Silicon 原生环境；按用户授权可使用本仓库的 macOS GitHub Actions。不能用旧 App 冒充新源码，也不能要求用户恢复不存在的 Mac 连接。下载产物后核验 sources()、架构及文件摘要，再接入父仓库打包。

## 两边接入

子模块固定到本仓库提交，构建后更新父仓库的 gitlink。package_gui.attach 允许通过 OPENWRT_GUI_ARTIFACT 与 OPENWRT_GUI_MACOS_ARTIFACT 指向同一份已校验交付件，避免两边重复编译。旧 artifact 指纹包含父目录资源路径，提取后必须第一次重编；以后只有相关源码变化才重编。

固件刷新/热补命令显式设置 OPENWRT_FIRMWARE_SOURCE。installer 本身无需固件源码即可构建；不得用相邻旧仓库文件兜底补缺输入。


## 固件包根目录的 APK

package_diff.py 的公共收尾步骤调用 package_android.py，固定附带一份 OpenWrt-Tools.apk、docs/GUI-BUILD-android.json 和 docs/ANDROID-INSTALLER.md。完整打包与父仓库的 refresh-installers 都必须保留这些文件及完整校验清单。可用 OPENWRT_GUI_ANDROID_ARTIFACT 显式复用另一检出的已验证 artifacts/android；源码、release 签名记录、ABI/ELF 和文件摘要必须匹配。只改打包胶水不重编未变化的桌面或 APK。
