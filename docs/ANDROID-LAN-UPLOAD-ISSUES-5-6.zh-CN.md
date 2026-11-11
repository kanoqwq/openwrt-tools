# Android 局域网权限与在线缩容上传（2026-11-11）

对应 [issue #6](https://github.com/kanoqwq/openwrt-tools/issues/6) 与
[issue #5](https://github.com/kanoqwq/openwrt-tools/issues/5)。

## 局域网权限

应用 targetSdk 为 37。[Android 官方要求](https://developer.android.com/privacy-and-security/local-network-permission)
在 Android 17 / API 37 及以上申请 ACCESS_LOCAL_NETWORK。Manifest 和 Tauri
权限别名现已声明；无线 ADB、SSH、联网 OTA 及保存的无线任务续接，在执行前给出
中英文说明并申请系统权限。拒绝时提供应用设置入口；取消不会开始设备操作。
USB、本地分析、文件和记录管理不依赖此权限。

实际 TCP 连接前再次检查权限，覆盖 ADB、SSH 和权限撤销后的新连接，检查先于
DNS、socket 创建和路由绑定。不以另一条网络绕过授权。后台工作线程不会等待
权限轮询；授权回调只重投原来的请求，仍经过原任务协调器的设备与并发检查。

本地完成 Python 传输／任务／权限决策回归、Android SDK 类型检查及权限边界的
JVM 回归；没有在 Android 17 实机验收权限弹窗，也未重编或安装 APK。

## 缩容上传及版本识别

TCP 短写的完整帧发送修复及 PREPARE_TIMEOUT=888 已在 eae58093 中落地，
本次保留其发送顺序、单帧 deadline 和既有缩容校验，不回退为 60 秒。
不能凭历史 60 秒日志认定当前二进制仍有同一缺陷，也不能替用户旧日志补版本。

新构建把 Git revision 和完整源码指纹写入 APK 资产；Gradle 把该身份作为任务
输入，构建后核对实际 APK 内的身份，防止复用旧资产。运行时诊断同时提供 Android
版本、targetSdk、APK versionName/versionCode、权限状态以及实际导入的准备超时。
分别展示当前安装器和操作执行时保存的身份；旧任务缺失身份时保持未知。
同 versionCode 并不证明旧运行时残留：当前进程首次初始化仍复制 APK 自身资产，
本次未复现 Android 覆盖安装后运行旧代码的问题，不擅自改变签名或版本编码规则。

移动端 ADB push 提供累计字节进度、速度和实际超时预算，错误保留传输文件名、
已提交给 FileSync 的字节数和总量；完成传输后仍使用原有设备侧 SHA 校验。
adb-shell 0.4.4 的 push 回调传入每个 DATA 块的长度，适配层累加后再发布进度。
真实 vendored FileSync 回归强制 TCP 每次最多发送 8192 字节，验证整份工具传完、
无重复发送及完整进度；错误注入验证失败不会重连重发。

没有实现“静默 10 秒就重连”或缩容写入自动重试：设备内存压力和正在执行的
F2FS／GPT 操作可能合法耗时，连接超时不能证明写入没有发生。继续沿用原来的
持久记录、确认和回读流程，避免把连接问题扩大为重复存储操作。
