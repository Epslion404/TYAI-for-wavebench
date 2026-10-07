# 管理仪器插件

本页适用于需要检查、安装、升级、降级、卸载或恢复本地 WaveBench 仪器插件的维护者。可执行插件是可信 Python 代码，不是安全沙箱；只处理来源、版本和依赖均已确认的本地源码目录或 wheel。

## 前置条件

- 在目标 WaveBench 虚拟环境中运行命令。
- 已确认插件与当前 WaveBench 版本兼容。
- 已确认源码 build backend 或 wheel 的来源可信。

## 检查并安装

```bash
python -m wavebench plugin package check <plugin-path>
python -m wavebench plugin install <plugin-path> --dry-run
python -m wavebench plugin install <plugin-path>
```

源码目录的检查会在临时目录构建 wheel，因此会执行受信任的 build backend。受管安装只使用当前解释器和明确给出的本地路径；它不联网下载依赖，也不修改 `wavebench.toml`。

安装前检查同时覆盖 wheel 文件与安装器生成的 `console_scripts`／`gui_scripts` 启动器。插件不能覆盖 WaveBench 启动器或声明 `pip`、`easy_install` 安装器入口；与其他分发拥有的文件或未受管启动器冲突时，安装和 dry-run 都会拒绝。Windows 按最终生成名称检查大小写与 `.py*` 别名，wheel 内部重复路径和跨分发归属采用同一平台规则。合法独立启动器仍可随插件安装、升级和卸载。

## 查看已安装插件

```bash
python -m wavebench plugin installed
python -m wavebench plugin list --load
python -m wavebench plugin doctor --load
```

`--load` 会导入可执行插件 descriptor。descriptor 导入不应连接仪器，但导入的 Python 代码仍必须可信。真正的 transport 只在配置选中该 driver 并执行仪器命令时创建。

## 升级、降级、卸载与恢复

```bash
python -m wavebench plugin upgrade <plugin-path> --dry-run
python -m wavebench plugin downgrade <plugin-path>
python -m wavebench plugin remove <driver-id>
python -m wavebench plugin recover
```

在安装、替换或卸载中断后，先运行 `plugin recover`。如果工具无法证明环境处于已知旧态或目标态，应保留环境和诊断信息，进行人工检查；不要通过手工删除记录来掩盖未知状态。

待恢复的事务也会阻止 dry-run。失败回滚执行 pip 前，会重新检查当前失败版本的全部安装文件归属；发现跨分发共享或无法确定归属时，保留原账本与事务记录。

失败回滚重装同一 wheel 时，安装器生成的 Windows 启动器可能因时间戳而改变摘要。工具会重新验证缓存 wheel、metadata、descriptor 和安装文件的 RECORD 哈希，并用目标环境安装器从 wheel 入口重建启动器、比较完整字节；Windows 只允许 ZIP 时间戳变化。验证成功后先更新事务记录，再写入验证后的账本摘要；启动器不符或安装器无法完成校验时保留恢复证据。普通外部重装或篡改仍会标记为 drifted。

## 边界

- V2 descriptor 只声明已实现和已测试的 capability，不会自动生成 CLI、Service 或 run plan step。
- 具体型号、SCPI、profile、quirk、限制和实机 evidence 由[仪器插件仓库](https://github.com/Scaxlibur/wavebench-instrument-plugins)维护。
- 公共插件模型、V1/V2 路径和声明式 SCPI 的边界见[插件 Reference](../reference/plugins/index.md)。
- 安装插件不等于授权硬件写入；仍需配置 access policy、capability 和安全预检。
