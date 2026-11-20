# OTA 上传弹窗按钮被禁用

三个固件仓库原先均固定安装器提交 `865dbfd`，使用同一份系统补丁网页。
按钮工厂向 LuCI 的 `E()` 传入 `disabled: false`。已核对正式 Pro 包中的
LuCI DOM.attr 实现：除 null/undefined 外，它会调用 setAttribute。
因此生成的 `disabled="false"` 仍是有效布尔属性，浏览器将按钮禁用。
页面主按钮后来经 controls() 修改 disabled 属性恢复，而新弹窗的取消按钮
没有这一步，所以一直不可点击。

修复让有写入权限时完全省略 disabled 属性，保留只读用户的写入限制。
文件选择弹窗的取消是本地操作，始终可点击。上传在选择合法文件后启用，
选择时或点击时失去写入权限则不能上传。未选择文件时上传仍保持禁用。
不改后端、上传目录、文件校验、生成／应用确认或设备状态。

测试先更正浏览器夹具，使布尔属性行为与 LuCI 一致，成功复现旧版本的
取消按钮断言失败，再修复生产代码。此前夹具将 disabled=false 当成 DOM
属性赋值，掩盖了真实 HTML 行为。修复后的真实 Chromium 回归覆盖：

- 中文、英文、土耳其文；390 和 1280 像素视口。
- 补丁导入、当前版基准 ZIP、新版 ZIP、双包模式旧版 ZIP 四个入口。
- 空选择、错误扩展名、合法文件、取消后重新打开（每入口两轮）。
- 弹窗中失去写入权限时禁止上传，仍能取消。
- 原有拖放、进度、上传目录准备、失败恢复、只读／忙碌／卸载限制。
- 原有生成包管理、确认前不生成／不应用，以及 LuCI 状态机回归。

命令：`python tests/ota_drop_browser.py`、`node tests/ota_luci.js`。
两项均通过；没有进行设备热部署。固件及独立安装器重新打包前应使用匹配的
新源码产物，旧 ZIP 不会因源码修复而自动变化。

## 布尔属性扩大检查

随后检查 E5、MU300、Pro 的 `openwrt/`、`devices/`、共享 `installer/runtime/`
及独立 SIP 插件，共 155 份自有运行时 JS；不包括第三方 vendor、构建输出或测试夹具。
同时检查原始 HTML、setAttribute 调用、传入变量的属性集合和控件封装。
新增发现为三仓库旧 `openwrt/luci-app-unisoc-ota/` 副本中相同的按钮工厂，
均已改为 `!L.hasViewPermission() || null`。当前完整包入口使用共享
`installer/stage-ota.py`；旧 Makefile 仍能复制旧副本，不能留着错误写法。

未发现其他同类原始 HTML 布尔属性错误。SIP 的 button/input 封装已有
false 属性过滤；Dropdown 的 multiple 选项、RPC 数据、ARIA 字符串及
`node.disabled = false` 等 DOM 属性赋值不适用 LuCI E() 的序列化规则。
没有替换这些正常写法，没有改全局 LuCI DOM 实现。

新增静态回归（使用 ui 已有 TypeScript 开发依赖）：

```powershell
node tests/luci_boolean_attributes.js D:/Git/e5-linux D:/Git/u30pro_openwrt D:/Git/mu300-linux-clean-tf-7.2 D:/Git/e5-sip-gateway
```

修复前准确报告上述三处，修复后 155 份源码通过。检查覆盖 E/dom.create 的
直接属性对象、原生 setAttribute、引号键名、跨行属性与条件分支，并用反例
保护 DOM 属性赋值、ARIA、Dropdown 选项及 SIP 封装。它不是完整的数据流分析，
变量属性集合和新控件封装仍需代码审查。
