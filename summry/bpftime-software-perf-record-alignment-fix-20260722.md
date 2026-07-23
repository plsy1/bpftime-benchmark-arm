# BPFtime software perf record 8 字节对齐问题：定位、修复与验证

## 技术结论

BPFtime 的 software perf buffer 在写入 `PERF_RECORD_SAMPLE` 时，原实现直接使用“记录头长度 + payload 长度”作为 `perf_event_header.size` 和 ring buffer 的推进长度，没有将完整记录向上对齐到 8 字节。

记录不断以非对齐长度推进后，某个 `perf_event_header` 可能从 ring buffer 的最后一个字节开始。libbpf 在执行 wrap-copy 之前会先直接读取 `ehdr->size`；当该字段跨越 ring 边界时，消费者可能读到 `size=0`。由于消费端按照该值推进 `data_tail`，结果是反复处理同一个地址而无法前进。

最终故障链如下：

```text
software perf record 未按 8 字节对齐
  -> record header 落到 ring buffer 最后一个字节
  -> libbpf 直接读取跨边界的 size 字段，得到 size=0
  -> data_tail 不再推进，callback 反复处理同一地址
  -> sslsniff 消费循环空转，无法继续排空 producer shards
  -> producer shards 填满，后续 append_sample 被拒绝
  -> output_data() 仍返回成功，形成不可见的事件丢失
  -> CPU/指令开销异常，并出现双状态吞吐量
```

修复后，BPFtime `sslsniff` 的异常空转消失；16 B 短测中，消费者进程的 task-clock 降低约 **95.46%**，指令数降低约 **96.07%**。完整七档 benchmark 的 BPFtime 平均变异系数从 **14.21%** 降至 **2.57%**，下降约 **81.94%**。

这项改动首先是一个**正确性修复**，同时显著提高了性能测量的可信度和可重复性。

跨平台短测还确认，Jetson 上观测到的 nginx 侧 `BPFtime/kernel ≈ 1.8×` 不是软件固有常数。在 GitHub-hosted x64 runner 上，硬件 instructions 不可用，但 nginx CPU-time/request 可测；BPFtime 的 nginx tracing delta 只有 kernel 的 `0.49–0.57×`，方向与 Jetson 相反，并与 x64 上 BPFtime 吞吐量优于 kernel 的结果一致。

## 问题是如何定位的

### 1. 异常不只是正常的性能波动

修复前，BPFtime 的吞吐量明显分成两个状态：

- 低状态通常约为 `9–10k RPS`；
- 高状态通常约为 `13–14k RPS`；
- 同一套代码和环境可能在两种状态之间切换；
- baseline 和 kernel eBPF 没有对应幅度的波动。

因此，旧结果中较高的 BPFtime 平均吞吐量并不能直接解释为稳定的性能优势。

### 2. `sslsniff` 消费端出现异常高指令量

修复前的 16 B 短测中，BPFtime `sslsniff` 消费者在 5 秒窗口内执行约 **22.03B instructions**，消耗约 **4.285 秒 task-clock**。这一成本与实际成功消费的事件数量不匹配。

进一步观察 callback 地址和 `perf_event_header` 后发现：同一地址被重复处理数亿次，外层类型仍为 `PERF_RECORD_SAMPLE`，但 `perf_event_header.size` 为零，所以消费位置无法前进。

### 3. 问题精确落在 ring buffer 边界

首次观察到的零长度记录位于 64 KiB data ring 的偏移 `65,535`，即 ring buffer 的最后一个字节。记录头从这里开始时，`size` 字段本身跨越 ring 边界。

生产端按跨边界方式重新拼接记录头后，记录内容和长度均合法。这排除了正常 payload copy 产生损坏记录的解释，确认根因是：

> BPFtime 允许记录从非 8 字节边界开始，而 libbpf 的 perf-buffer 消费路径假定 `perf_event_header` 可以在当前位置连续读取。

## 修复内容

修复提交：`0fcdb0ef4f33cc09d0bf43136154f611c0271132`  
分支：`codex/official-no-btf`

修改位于：

- `runtime/src/handler/perf_event_handler.cpp`
- `runtime/unit-test/test_software_perf_event.cpp`

核心修改包括：

1. 将完整 software perf record 长度向上对齐到 8 字节；
2. 将对齐后的长度写入 `perf_event_header.size`；
3. 使用同一个对齐长度推进 `data_head`；
4. 将记录末尾新增的 padding 清零；
5. 拒绝无法放入 16 位 `perf_event_header.size` 的超长记录；
6. 在 producer-shard copy 路径中拒绝未对齐记录；
7. 新增 ring 边界回归测试。

这里对齐的是**完整 perf record**，不是要求 HTTP payload 本身必须是 8 字节的倍数。记录实际包含 perf header、raw-size 字段、SSL 数据和末尾 padding。

## 正确性验证

### 单元测试

新增测试使用一个 64 字节 data ring，让记录头依次走到 ring 中最后一个合法的 8 字节 header 位置，并检查：

- 每个记录的起始位置均为 8 字节对齐；
- `perf_event_header.size` 为对齐后的完整长度；
- payload 内容保持不变；
- padding 全部为零；
- ring 边界位置能够被实际覆盖。

software-perf 三项单元测试共 **8,358 个 assertions 全部通过**。

### 真实 benchmark 消费行为

在 latest no-BTF、Docker bridge、16 B payload、100 个连接的环境中：

- 正常输出模式下 callback 持续前进至 157,764 次；
- 最小 callback 模式下持续前进至 170,121 次；
- 未再次观察到 `size=0` 或同一地址无限重复；
- 抽样记录长度为 120 B 和 336 B，均可被 8 整除。

这说明修复消除了已定位的跨边界零长度记录和消费端死循环。

## 消费者 CPU 成本显著下降

以下为同一 16 B、Docker bridge 短测中，对 BPFtime `sslsniff` 进程进行的 5 秒 `perf stat` A/B：

| 指标 | 修复前 | 修复后 | 变化 |
|---|---:|---:|---:|
| Requests/sec | 10,300.35 | 10,114.73 | -1.80% |
| task-clock | 4,284.63 ms | 194.47 ms | -95.46% |
| cycles | 7.390 B | 0.324 B | -95.62% |
| instructions | 22.032 B | 0.866 B | -96.07% |
| CPU utilization | 0.828 CPU | 0.039 CPU | -95.29% |

这组单轮短测的用途是验证空转是否消失，而不是比较稳定吞吐量。结果表明，修复后 `sslsniff` 不再通过错误记录反复执行 callback，CPU 和指令开销均降低约 95% 以上。

## `perf` 统计必须同时覆盖 nginx worker

字节对齐问题发生在 `sslsniff` 消费端，但 `sslsniff` 进程并不包含完整的 tracing 成本。SSL uprobes attach 在 nginx 使用的 OpenSSL 函数上，probe 程序、BPFtime JIT、helper 和 event-production 工作实际都在 nginx worker 的请求上下文中执行。

事件中的 PID/TID 来自 `bpf_get_current_pid_tgid()`。实际捕获结果显示，kernel 和 BPFtime 两种模式下的事件 PID 都与对应 nginx worker PID 一致。因此：

- 只对 `sslsniff` PID 执行 `perf stat`，只能得到 perf-buffer 消费者成本；
- kernel BPF VM/JIT 执行不会出现在 kernel `sslsniff` 进程中；
- BPFtime probe、JIT、helper 和 software-perf event production 同样不会出现在 BPFtime `sslsniff` 进程中；
- 完整归因至少需要分别测量 nginx worker 和 `sslsniff` reader。

nginx 侧额外成本按下面的方式计算：

```text
nginx tracing delta
  = traced nginx instructions/request
  - baseline nginx instructions/request
```

测量结果表明，BPFtime 在 nginx 请求路径中产生的额外指令约为 kernel eBPF 的 **1.8 倍**：

| Payload | Kernel nginx 额外指令/request | BPFtime nginx 额外指令/request | BPFtime / kernel |
|---|---:|---:|---:|
| 16 B | 25.1 k | 46.0 k | **1.83×** |
| 256 KB | 221.6 k | 391.2 k | **1.77×** |

16 B 数据来自对齐修复后的同轮 baseline/kernel/BPFtime 进程归因；256 KB 数据来自此前相同 latest Docker-bridge 路径的三轮进程归因。两档 payload 得到接近的比例，说明约 `1.8×` 不是只在某个 payload 下出现的偶然值。

这里比较的是“挂载 tracing 后相对 baseline 新增的 nginx 指令”，不是 nginx 的总指令量，也不是完整后端成本。完整的描述性归因应写成：

```text
attributed tracing cost
  = nginx tracing delta
  + sslsniff reader cost
```

### nginx 侧具体重在 event-production/helper 路径

`perf report` 和 producer-path 直接计时将 nginx 中的主要 BPFtime 路径定位为：

```text
bpf_perf_event_output
  -> bpftime_perf_event_output
  -> software_perf_event_data::output_data
  -> software_perf_event_data::get_current_thread_shard
  -> software_perf_event_buffer::append_sample
```

其中最大的单项成本不是 map/handler 查询或 payload copy，而是每次事件输出都要执行的 CPU affinity 操作：

```text
sched_getaffinity
  -> sched_setaffinity（固定到当前 CPU）
  -> 选择并写入对应的 perf handler / producer shard
  -> sched_setaffinity（恢复原 affinity）
```

直接计时结果如下：

| nginx producer-path 指标 | 16 B | 256 KB | 大/小 payload |
|---|---:|---:|---:|
| Event-output calls/request | 1.977 | 17.918 | 9.06× |
| Attempted output bytes/request | 418 B | 131,772 B | 315.11× |
| 整个 helper 时间/call | 8,297 ns | 8,991 ns | 1.08× |
| Affinity 保存/固定/恢复/call | 5,784 ns | 6,119 ns | 1.06× |
| Affinity 占 helper 时间 | 69.7% | 68.1% | — |
| Map type + handler 查询/call | 327 ns | 382 ns | 1.17× |
| `bpftime_perf_event_output`/call | 2,042 ns | 2,328 ns | 1.14× |
| `get_current_thread_shard`/call | 1,276 ns | 1,430 ns | 1.12× |
| 成功 ring append/call | 414 ns | 747 ns | 1.80× |
| 被拒绝的 ring append/call | 193 ns | 210 ns | 1.09× |

大 payload 并没有让单次 helper 变得特别慢：从 16 B 到 256 KB，单次 helper 只增加约 8%。真正的放大因素是 event-output 调用次数从每请求约 `1.98` 次增加到 `17.92` 次，增长约 **9.06 倍**。每个事件固定支付约 `5.8–6.1 μs` 的 affinity 成本，因此 payload 越大、事件越多，这项固定成本被重复支付得越多。

### 跳过 affinity 的诊断性 A/B

临时诊断版本只跳过 affinity 保存、固定和恢复，其余 producer 路径保持不变：

| Payload | 当前 producer 路径 | 跳过 affinity | 吞吐量变化 |
|---|---:|---:|---:|
| 16 B | 10,129.11 RPS | 12,208.05 RPS | **+20.52%** |
| 256 KB | 1,040.97 RPS | 1,272.67 RPS | **+22.26%** |

相比之下，只缓存 map type 和 handler fd 在 16 B 下提高 `1.21%`，在 256 KB 下反而降低 `2.55%`，没有表现出可重复收益。由此可以确认：

> nginx 侧 BPFtime 路径的主要可定位成本，是 `bpf_perf_event_output` 每次调用中的 CPU affinity 保存、固定和恢复；大 payload 通过增加 event-output 调用次数放大了这项固定成本。

该 A/B 用于定位成本，不是可直接合入的优化方案。当前 affinity 固定操作用于防止线程在“选择 CPU-indexed perf handler”和“写入事件”之间迁移。正式优化必须通过 migration-safe retry、稳定的 per-thread/per-CPU 设计或其他机制继续保证这一正确性属性。

因此，目前已经区分出两条独立路径：

- **消费者路径：**8 字节对齐缺陷导致 `sslsniff` 在零长度记录上空转；本次修复已解决。
- **nginx 生产路径：**每事件 affinity 操作约占 helper 时间的 `68–70%`，并随事件数增加而放大；已经定位，但尚未实施生产级优化。

## x64 跨平台短测：nginx 路径成本方向相反

为了判断 Jetson 上的 nginx 侧 `1.8×` 是否可以外推到 x64，新增了独立的 GitHub Actions 短测。配置如下：

- runner：GitHub-hosted `ubuntu-24.04`，4 vCPU，Intel Xeon Platinum 8573C；
- 内核：Azure `6.17.0-1020-azure`；
- 代码：`codex/official-no-btf`，包含 8 字节对齐修复；
- 工具链：与 x64 full benchmark 相同的 LLVM/Clang 15、RelWithDebInfo 和 LTO；
- payload：16 B；
- 每种模式三轮，8 秒 `wrk`，内部 5 秒 PID 计数窗口；
- 分别监测 nginx worker、`sslsniff` reader 和 `wrk` PID；
- `kernel-global` 保留原 benchmark 的全局 attach scope；
- `kernel-nginx-only` 使用 `-p <nginx-worker-pid>`，只 attach nginx。

Azure 虚拟机明确将 `cycles` 和 `instructions` 报告为 `<not supported>`，所以该平台不能复现 Jetson 的 instructions/request 统计。以下使用可用的 `task-clock/request`，即处理一个请求实际消耗的进程 CPU 时间。

### 三轮均值

| Mode | RPS | nginx CPU/request | reader CPU/request |
|---|---:|---:|---:|
| Baseline | 60,647.49 | 16.507 μs | — |
| Kernel global | 23,063.78 | 43.397 μs | 13.828 μs |
| Kernel nginx-only | 21,086.51 | 47.469 μs | 8.173 μs |
| BPFtime | 31,550.95 | 31.728 μs | 2.440 μs |

所有模式下 nginx worker 在 5 秒计数窗口中都消耗约 5 秒 task-clock，即持续占满一个 CPU。因而这里的 nginx CPU/request 表示“一个饱和 nginx worker 每完成一个请求消耗的 CPU 时间”，不是由空闲比例差异产生的结果。

BPFtime 相对原 benchmark scope 的 kernel-global 吞吐量高 **36.80%**。此前不带 perf 的 x64 full benchmark 中，16 B 的 BPFtime 为 `12,587.59 RPS`，kernel 为 `10,033.71 RPS`，BPFtime 高 **25.45%**。不同 GitHub runner 的绝对吞吐量差异较大，但两次测试的方向一致。

### 同轮 baseline 扣除后的 tracing 成本

```text
nginx tracing delta
  = traced nginx CPU/request
  - same-round baseline nginx CPU/request
```

| Mode | nginx tracing delta | reader CPU/request | 描述性总成本 |
|---|---:|---:|---:|
| Kernel global | 26.891 μs/request | 13.828 μs/request | 40.719 μs/request |
| Kernel nginx-only | 30.962 μs/request | 8.173 μs/request | 39.134 μs/request |
| BPFtime | 15.221 μs/request | 2.440 μs/request | 17.661 μs/request |

x64 上得到：

- BPFtime nginx delta 是 kernel-global 的 **0.566×**，低 **43.4%**；
- BPFtime nginx delta 是 kernel-nginx-only 的 **0.492×**，低 **50.8%**；
- BPFtime 描述性总成本比 kernel-global 低 **56.6%**；
- BPFtime 描述性总成本比 kernel-nginx-only 低 **54.9%**；
- BPFtime reader CPU/request 比 kernel-global 低 **82.4%**，比 kernel-nginx-only 低 **70.1%**。

这里的关键结论不是三轮短测的精确吞吐量，而是路径成本方向：

> Jetson 上 BPFtime 的 nginx 新增指令约为 kernel 的 `1.8×`；GitHub-hosted x64 上，BPFtime 的 nginx 新增 CPU 时间只有 kernel 的 `0.49–0.57×`。因此，nginx producer/helper 路径的相对成本具有明显的平台相关性，Jetson 结果不能直接外推到 x64。

该结果还不能单独证明 x64 上究竟是哪一个子步骤更便宜。Jetson 已经通过内部计时和跳过 affinity 的 A/B 定位到 affinity 是 BPFtime helper 的最大单项成本；若需要解释 x64 为什么方向翻转，下一步需要在 x64 上增加同样的 producer-path 内部计时或 affinity A/B。当前数据只确认 x64 nginx 路径总体更便宜，不对具体子步骤作未经验证的因果判断。

## 完整 benchmark 的方差验证

测试配置：

- 镜像基础：`bpftime:official-no-btf`；
- 网络：Docker 默认 bridge，压力流量使用容器 loopback；
- PID namespace：host；
- benchmark、sslsniff 和 BPF 程序保持原版；
- 每档 payload 分别执行 baseline、kernel eBPF 和 BPFtime 各 10 次；
- 修复前使用两轮完整测试作为对照；
- 使用标准差除以均值得到变异系数（CV），以消除不同吞吐量量级的影响。

### BPFtime 变异系数对比

| Payload | 修复前第 1 轮 | 修复前第 2 轮 | 修复前平均 | 修复后 | CV 降幅 |
|---|---:|---:|---:|---:|---:|
| 16 B | 13.81% | 14.14% | 13.97% | 2.21% | 84.16% |
| 1 KB | 13.38% | 13.85% | 13.62% | 1.04% | 92.35% |
| 2 KB | 13.16% | 15.19% | 14.17% | 2.02% | 85.72% |
| 4 KB | 13.64% | 11.01% | 12.33% | 1.80% | 85.36% |
| 16 KB | 15.41% | 16.59% | 16.00% | 3.10% | 80.63% |
| 128 KB | 13.40% | 16.48% | 14.94% | 3.77% | 74.75% |
| 256 KB | 17.85% | 11.03% | 14.44% | 4.01% | 72.24% |
| **七档平均** | — | — | **14.21%** | **2.57%** | **81.94%** |

以 16 B 为例，修复前两轮标准差分别为 `1,768.74 RPS` 和 `1,799.52 RPS`；修复后降为 `224.86 RPS`。

修复后 BPFtime 的方差仍高于 kernel eBPF：本轮 kernel CV 为 `0.66%–1.38%`，BPFtime 为 `1.04%–4.01%`。因此测试稳定性已经大幅改善，但大 payload 下仍有进一步降低波动的空间。

## 修复后的七档吞吐量

| Payload | Baseline RPS | Kernel eBPF RPS | BPFtime RPS | BPFtime 相对 kernel |
|---|---:|---:|---:|---:|
| 16 B | 16,385.01 | 11,377.51 | 10,159.76 | -10.70% |
| 1 KB | 15,871.11 | 10,796.07 | 10,027.37 | -7.12% |
| 2 KB | 15,030.87 | 10,332.44 | 9,788.18 | -5.27% |
| 4 KB | 14,298.73 | 10,087.80 | 9,462.77 | -6.20% |
| 16 KB | 9,912.14 | 7,058.14 | 6,330.19 | -10.31% |
| 128 KB | 2,971.46 | 2,050.11 | 1,903.70 | -7.14% |
| 256 KB | 1,647.29 | 1,179.13 | 1,069.88 | -9.27% |

### 为什么 CPU 成本下降后，吞吐量没有同步提高

修复前的 BPFtime 数据不是围绕一个均值随机波动，而是存在明显的低、高两个状态。高状态通常伴随着消费链路没有完成正常工作：消费端卡在错误记录后，producer shards 填满，大量后续事件被拒绝，但 `output_data()` 仍对 BPF 程序报告成功。

因此，旧数据中的部分高吞吐量实际上是“少处理了 tracing 工作”产生的虚高。修复后，结果收敛到原有低状态附近，表示系统稳定执行了正确的消费路径。不能把旧的异常高均值与修复后的正确均值直接解释为性能下降。

## 尚未解决的问题与结论边界

- `output_data()` 仍然忽略 `append_sample()` 的 Boolean 返回值，并向 BPF 程序返回成功；producer buffer 满时的显式丢失统计仍需单独修复。
- 8 字节对齐修复解决的是消费者空转和记录布局错误，不等同于已经优化 nginx 侧的 event-output 成本。
- 原 benchmark 中 kernel sslsniff 默认同时追踪 nginx 和使用 OpenSSL 的 `wrk`，而 BPFtime agent 只注入 nginx；未经 scope 对齐的结果不能作为严格等价的后端成本比较。
- 完整测试表中的吞吐量用于描述修复后的稳定结果，不应与修复前的异常高状态直接作性能回归判断。
- GitHub-hosted x64 Azure runner 不提供 hardware cycles/instructions；x64 跨平台章节比较的是 CPU-time/request，不能与 Jetson 的 instructions/request 数值直接作绝对量换算。

## 证据与复现路径

- 修复后完整结果：`benchmark-results/latest/full/aligned-ssl-nginx-20260722_211509/`
- 修复前两轮完整结果：`benchmark-results/latest/full/docker-2x-20260714/`
- 16 B `perf stat` A/B：`benchmark-results/latest/diagnostics/alignment-16b-perf-short-20260722/`
- 修复验证记录：`benchmark-results/latest/diagnostics/alignment-fix-bridge-16b-20260722_115025/fix-validation.md`
- 完整定位过程：`summry/bpftime-latest-jetson-ssl-nginx-bridge-path-analysis-20260721.md`
- 代码仓库：`bpftime-offical-no-btf/`
- 修复提交：`0fcdb0ef4f33cc09d0bf43136154f611c0271132`
- x64 nginx-path perf workflow：`https://github.com/plsy1/bpftime-benchmark/actions/runs/29924995620`
- x64 原始 artifact：`benchmark-results/latest/diagnostics/x64-nginx-path-perf-20260722-run29924995620/`
