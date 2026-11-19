# 版本基准 ZIP 上传目录准备

适用：系统补丁的“初始化或修复版本基准”、旧版／新版完整 ZIP 上传。

## 已确认的代码缺口

原流程在打开 LuCI 选文件弹窗之前调用 build_status，借此创建固定的
/etc/unisoc-ota/builder。用户真正选择文件并提交时，可能已过去较长时间；
期间目录若被更新或清理移除，CGI 不会自动创建父目录。

cgi-io 在接收完成后才打开最终目标。因此即使发送前目录存在，
传输期间被移除仍可能产生 Failed to open target file（ENOENT / 2）。
只有这句文案不能确认群友设备的具体原因，也可能是 ENOSPC、EROFS、
EACCES 等写入失败；需要原始错误码判断，不能当作 ZIP 基准校验失败。

反馈补充已确认：/etc/unisoc-ota 存在，缺少 builder 子目录，old.zip 无法创建。

参考：OpenWrt cgi-io/main.c 的上传完成处理：
https://lxr.openwrt.org/source/cgi-io/main.c

## 修正

- 完整 ZIP 的点击选择与拖放共用原来的认证 CGI 上传逻辑及固定 old.zip /
  new.zip 目标。选好文件、确认上传后才进行最终 build_status 预检并建目录。
- 若 CGI 在落盘时明确返回 ENOENT + Failed to open target file，再通过
  同一后端准备目录，使用原 File 重传一次。重复失败即停止，不无限重试。
- 空间不足、只读及权限错误不自动重传；保留 errno 和固定目标路径，
  并给出中／英／土说明，避免只显示通用失败。
- 取消选择、离开页面、选文件期间生成任务开始，均不会继续上传。
- 不自动生成基准、生成差分、应用补丁，也不重启 rpcd 或设备。
- 不更改 OTA 内容校验、版本比较、回滚保护、密码或用户配置。

## 验证

真实 build_status 的目录重建测试覆盖 E5、MU300、Pro；
Builder / RPC / ZIP 导入共 68 项离线测试通过。
浏览器及 Node 测试覆盖点击、拖放、目录在选择期间被删除、
传输末尾 ENOENT、仅重试一次、磁盘满／只读／权限错误、
取消／页面卸载、重复点击、生成任务互斥和三语言移动／桌面布局。

这些是离线测试，未访问或修改反馈者设备。旧版若确认仅缺目录，可临时执行：

```sh
mkdir -p /etc/unisoc-ota/builder
```

如果目录存在仍失败，应读取完整错误码及文件系统空间／挂载状态，
不要清除版本基准或反复删除上传目录。
