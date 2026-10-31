# 波形采集、测量与分析

读取时机：需要解释 `measure`/`fetch`/`analyze`/`plot` 的指标、文件格式或采样率时读本页。

## 从设备到指标

推荐顺序：

1. `inspect`：只读确认 IDN、错误队列和通道状态；
2. `measure`：优先读取示波器自己的测量值；
3. `fetch`：需要原始波形、时间轴或离线复算时读取；
4. `analyze`：对已有 CSV/JSON 复算；
5. `plot`：生成可交付的 SVG 单帧图。

`measure` 的标量指标：

| JSON 字段 | 含义 |
| --- | --- |
| `frequency_hz` | 频率 |
| `vpp_v` | 峰峰值 |
| `vmax_v` / `vmin_v` | 最大/最小电压 |
| `vavg_v` | 平均电压 |
| `vrms_v` | 均方根电压 |

这些值只有在设备错误队列验证通过后才写入 `measurements`。设备拒绝的命令、空响应或非数值响应
会留在 `unsupported` 或 `failures`，不会补成 0。

## fetch 的数据格式

`--format real` 读取 IEEE 488.2 确定块，按小端 float32 解析；`--format ascii` 读取文本列表。
`DATA:HEAD?` 返回的起点、步长和终点会用样本数做一致性检查：

- 一致时 `header_consistent=true`，可由 `xinc_s` 推导采样率；
- 不一致时保留 `header_raw`/`warnings`，不要静默相信 header；
- `--sample-rate` 用于 header 不可信或缺失时覆盖。

`fetch` 结果包含：

- `samples`（默认只保存在文件，stdout 不含完整数组）；
- `sample_count`、`sample_rate_hz`、`xstart_s`、`xinc_s`、`header_consistent`；
- `metrics`（对完整波形计算）；
- `warnings`、`errors`、`failures`、`device_errors`；
- 指定 `--output` 时的 `output`；指定 `--plot-output` 时的 `plot_output`。

CSV 固定为 `time_s,value_v`，时间步长由采样率或 `xinc_s` 推导；JSON 保留完整结果对象。
读取失败不生成伪样本：`sample_count` 为 0，失败原因在 `errors`/`failures`。

## analyze 指标

`analyze` 至少需要 4 个有限采样点，采样率必须是有限正数。输出字段：

| 字段 | 说明 |
| --- | --- |
| `samples` | 有效样本数 |
| `sample_rate_hz` | 采样率；未知时可为 `null` |
| `peak_v` | 绝对值最大的样本值 |
| `peak_to_peak_v` | 最大值减最小值 |
| `minimum_v` / `maximum_v` / `mean_v` | 最小、最大、平均 |
| `rms_v` | 全信号 RMS |
| `ac_rms_v` | 去均值后的 AC RMS |
| `frequency_hz` | 带迟滞的上升沿频率；无法估计时为 `null` |
| `duty_cycle_percent` | 以均值为判决电平的占空比 |
| `rise_time_10_90_s` / `rise_time_s` | 10%–90% 上升时间 |
| `overshoot_percent` / `undershoot_percent` | 相对 P10/P90 区间的过冲/下冲 |
| `dft` / `fft` | 基频、幅值、谐波和 `thd_percent`（可用时） |

`frequency_hz` 不是通过简单过零计数得到的；它使用迟滞和插值，低采样率或噪声波形可能得到 `null`。
`dft`/`fft` 的数值受采样率、窗长和基频估计影响；报告里要写清采样率和数据来源。

## 文件输入

`analyze --file`/`plot --file` 支持：

- CSV：两列 `time_s,value_v`，或单列值；带时间列时用相邻时间的中位差值推导采样率；
- JSON：至少包含 `samples`，可选 `sample_rate_hz`；读取时使用 UTF-8（兼容 BOM）。

没有文件或使用 `--demo` 时，`analyze` 使用内置 1 kHz 演示波形，输出 `source: built-in-demo`。
演示结果只用于检查流程，不代表真实仪器测量。

## SVG 图

`plot` 和 `fetch --plot-output` 生成不依赖 matplotlib 的单帧 SVG，包含波形、标题和可用指标。
它用于快速交付和报告，不替代原始 CSV/JSON；需要复算时保留完整数据文件。

## 报告里的最小信息

每个测量结论至少注明：设备地址、通道、采样率/时基、单位、量程、数据来源（设备测量或离线分析）、
时间戳，以及未支持或未验证项。不要把 `null`、空响应或未取得的值写成 0。
