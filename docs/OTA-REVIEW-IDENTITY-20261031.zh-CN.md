# SSH OTA 的会话标识与补丁身份交接

## 确认的原因

`remote_prepare()` 使用随机 64 位十六进制值生成私有 RAM 目录，避免复用旧会话。
打入旧 OTA 的 `bootstrap.sh` 却将 `/tmp/unisoc-ota-` 后的目录名当成补丁 ID，
传给 `rpc.py start`。`stage_upload()` 对真正的清单重新计算 SHA-256 后发现不匹配，
因而拒绝并提示 `Upload differs from the patch you reviewed; inspect again`。

Android、Windows、macOS 共用这个 SSH 后端，故均可能受影响。LuCI 直接传递
检查得到的补丁 ID，不从临时会话目录推导，所以不受这个交接缺陷影响。

## 修正

- RAM 目录标识只用于隔离会话；控制端显式传递检查所得的补丁 ID，并在 start 时
  校验格式。三参数旧调用也可从受控的 `upload-<id>.ota`／`reviewed-<id>.ota`
  文件名取得同一身份；不再从临时目录推导。通用 `upload.ota` 必须显式传入检查 ID。
- SSH 控制端对完整验证过的旧 OTA 清单重新生成当前交接脚本，并校验上传脚本的摘要。
  不改 OTA 压缩包、其中的设备端引擎或文件 blob，也不删除真正的清单校验。
  因此已有差异包无需重新制作；更新控制端即可修正本次交接。
- 网页的检查 ID、确认令牌、设备 SSH 身份检查和设备端实际内容比对保持生效。

## 回归范围

临时文件夹内运行真实 POSIX shell 的交接前缀，再调用真实 `rpc.stage_upload()`：
旧逻辑产生随机会话 ID，稳定复现同一错误；新逻辑产生实际清单 ID，进入已检查副本。
同时覆盖第二次会话隔离、旧包原样保留、脚本上传摘要、generic upload 仅可检查、
非法启动名称拒绝、reviewed 名称及失败候选清理。没有写真实设备分区或 NV。
