# Jetson 上 bpftime v0.2.0 `ssl-nginx` host 性能异常：验证路径与结论

**整理日期：** 2026-07-15  
**测试平台：** Jetson Orin Nano，ARM64，Tegra 6.8.12 kernel  
**主要对象：** `bpftime-v020-no-btf` 与 `bpftime-offical-no-btf`  
**Benchmark：** `benchmark/ssl-nginx`，指标为 Requests/sec，数值越高越好

## 技术总结

Jetson 上 bpftime v0.2.0 的 host 测试没有发生功能错误。最终确认的现象是：

1. **Docker private network namespace 中，v0.2.0 BPFtime 在 1KB 下相对 kernel 为正；切换为 host network namespace 后，结果翻转为负。**
2. **host network namespace 会降低全部模式的吞吐，但 BPFtime 的下降更大。**交错 5 轮 A/B 中，baseline、kernel、BPFtime 分别下降 6.71%、6.85% 和 11.99%，BPFtime 额外多损失约 5.14 个百分点。
3. **Tailscale 是 host 网络环境中的主要影响因素。**关闭 Tailscale 后，kernel 吞吐基本保持在波动范围内，而 BPFtime 在全部 payload 上提高 3.71%～8.34%，相对 kernel 的差距改善 2.21～10.56 个百分点。
4. **v0.2.0 的 userspace reader 成本较高，使其对 host 网络栈开销更加敏感。**5 秒 perf window 中，早期 BPFtime reader 观测到约 27.33B instructions，而 kernel reader 约为 1.86B；即使进入 quiet fast path，BPFtime 仍执行 20B+ instructions。
5. **最新版降低了 reader/runtime 成本，因而在相同 Jetson host 环境中重新获得了小 payload 优势。**最新版 host 5 次平均在 16B～4KB 上相对 kernel 提升 3.17%～7.75%。

因此，适合对外使用的结论是：

> Tailscale 向 host network namespace 注入的 netfilter 链及相关网络处理状态，会增加 Jetson 上 loopback benchmark 的系统开销。v0.2.0 BPFtime reader 本身接近 CPU 瓶颈，因此该开销被放大并吞掉了其相对 kernel 的小幅性能优势；Docker private network namespace 不受 host Tailscale 规则影响。最新版降低 reader/runtime 成本后，小 payload 优势重新变得可观察。

需要保留的措辞边界是：**现有证据锁定了 Tailscale 的 host netfilter/network integration，但尚未证明性能损失只由某一条 `ts-input` 链单独造成。**

## 问题定义与比较口径

### 代码范围

- `bpftime-v020-no-btf`：官方 v0.2.0 代码，只做 no-BTF 适配。
- `bpftime-offical-no-btf`：官方较新版本代码，只做 no-BTF 适配。
- 最终性能比较保留原 benchmark 实现，没有通过修改 `benchmark.py` 改变测试语义。

### 指标定义

本文使用：

```text
BPFtime vs Kernel = (BPFtime Requests/sec - Kernel Requests/sec)
                    / Kernel Requests/sec × 100%
```

- 正数：BPFtime 吞吐高于 kernel eBPF。
- 负数：BPFtime 吞吐低于 kernel eBPF。
- `pp` 表示百分点变化。

### “bridge/private” 的准确含义

Benchmark 中的 `wrk` 与 nginx 在同一容器内，并访问 `https://127.0.0.1:4043`。因此所谓 Docker bridge 模式的关键差异是 **container private network namespace 内的 loopback**，流量并不实际经过 `docker0` 转发路径。本文统一称为 private netns。

## 验证路径概览

| 阶段 | 假设 | 验证方式 | 结果 | 判定 |
|---|---|---|---|---|
| 1 | benchmark 或 tracer 构建错误 | 保留原 benchmark；按 Docker 工具链重建；确认 no-BTF 对象可加载 | baseline、kernel、BPFtime 均可运行并产生数据 | 排除功能与加载错误 |
| 2 | root/non-root 身份导致差异 | host root 与普通用户短测；Docker root 与 ubuntu 用户短测 | Docker 两种身份均为正；host root 仍为负 | 排除运行身份 |
| 3 | Boost 版本差异 | host 使用 Boost 1.74 重建并跑 1KB 10 次 | BPFtime 相对 kernel 平均 -4.55% | Boost 不是根因 |
| 4 | host 与 Docker runtime 二进制不同 | host 直接使用 Docker 中相同的 agent/syscall-server 二进制，1KB 10 次 | 平均仍为 -1.82% | 二进制差异不能解释翻转 |
| 5 | benchmark 顺序造成漂移 | 多轮、交错次序和 payload 定向测试 | 负结果稳定存在 | 排除测试顺序 |
| 6 | BPFtime 事件更多导致 reader 更重 | perf stat、perf report、quiet fast path、poll stats | BPFtime 事件更少但 instructions 显著更多 | 根因在单位事件/消费路径成本 |
| 7 | Docker IPC namespace 导致差异 | `--ipc=host` 与默认/private network 对照 | private network 下仍为 +4.20% | IPC 不是根因 |
| 8 | network namespace 导致差异 | `--network=host` 单变量 A/B，随后交错 5 轮复验 | private +3.67%，host -2.06% | 已确认 network namespace 会翻转结果 |
| 9 | Tailscale/netfilter 是 host netns 中的关键变量 | 模拟 `ts-input`、loopback INPUT bypass、关闭 Tailscale 完整测试 | 关闭 Tailscale 后 BPFtime 单独显著改善 | Tailscale 整体网络集成是主要因素 |
| 10 | 最新版已优化 reader/runtime | 最新版 host 5 次与 Docker 2 次完整 payload 测试 | 最新版小 payload 重新为正 | 与 reader 成本解释一致 |

## 构建、权限和依赖均不能解释结果翻转

### 构建与 no-BTF 路径

host 的 sslsniff 曾按与对应 Docker 镜像一致的 LLVM/Clang 工具链重新构建。最终运行使用原始 `ssl-nginx` benchmark，并确认 kernel tracer 能加载和附着。由此排除了以下情况：

- kernel 结果为空是由错误 BPF object 导致；
- host 使用了不同版本的 sslsniff；
- benchmark 为了得到数据而改变了测试逻辑；
- no-BTF 适配破坏了 tracer 功能。

### root 与非 root

1KB 短测中：

- Docker root：kernel 11024.96，BPFtime 11421.54，BPFtime 为正。
- Docker ubuntu 用户：kernel 11179.17，BPFtime 11614.92，BPFtime 为正。
- host root 可读测试：kernel 11097.31，BPFtime 10120.14，BPFtime 仍为负。

因此运行身份与结果翻转没有一致对应关系。早期约 25 Requests/sec 的 root 结果来自文件/目录可读权限错误，属于无效样本，未纳入分析。

### Boost 1.74 与 Docker 完全相同 runtime

| 1KB host 条件 | 轮数 | Kernel 均值 | BPFtime 均值 | BPFtime vs Kernel | 胜出次数 |
|---|---:|---:|---:|---:|---:|
| Boost 1.74 重建 | 10 | 11285.41 | 10771.96 | -4.55% | 2/10 |
| 使用 Docker 相同 runtime 二进制 | 10 | 11262.82 | 11060.02 | -1.82% | 3/10 |

完全相同 runtime 二进制缩小了差距，但没有让 host 结果稳定转正，所以它不是最终解释。

## v0.2.0 reader 在 Jetson 上具有较高固定成本

### 正常输出路径的 perf stat

早期 5 秒统计窗口：

| 模式 | task-clock | cycles | instructions | IPC | wrk Requests/sec |
|---|---:|---:|---:|---:|---:|
| Kernel sslsniff | 1.42s | 1.25B | 1.86B | 1.49 | 9506 |
| BPFtime sslsniff | 4.19s | 5.62B | 27.33B | 4.86 | 8994 |

BPFtime reader 的 instructions 约为 kernel reader 的 14.7 倍。该差异主要来自 userspace perf-event/mock 消费路径，而不是 cache miss。

### quiet fast path 仍然很重

临时 `--quiet` 诊断先关闭事件打印，再把 fast path 前移到 `handle_event()`，避免 payload copy 与 `print_event()` 大栈帧。结果仍为：

| 模式 | task-clock | cycles | instructions | wrk Requests/sec |
|---|---:|---:|---:|---:|
| Kernel quiet fast path | 1.01s | 0.76B | 1.06B | 9623 |
| BPFtime quiet fast path | 3.07～4.07s | 4.11～5.45B | 21.37～26.67B | 8561～9321 |

这说明输出、格式化和 payload copy 会放大开销，但并不是唯一根因。

### perf report 与 poll stats

正常输出路径最初的主要热点为：

```text
76.17%  __memset_zva64
        print_event
        handle_event
        perf_buffer__process_records
        perf_buffer__poll
```

去掉每事件 8KB buffer 清零后，热点转移到 `print_event`、`handle_event`、`strcmp` 和 `perf_buffer__process_records`。quiet fast path 中主要热点为：

```text
51.39%  handle_event
31.02%  strcmp
14.51%  perf_buffer__process_records
```

5 秒 poll stats：

| 模式 | polls | ready | events | wrk Requests/sec |
|---|---:|---:|---:|---:|
| Kernel quiet | 190785 | 190753 | 191201 | 9482 |
| BPFtime quiet fast path | 202 | 169 | 15097 | 8785 |

BPFtime 的事件数量明显更少，但 reader CPU 与 instructions 更高，因此不能用“BPFtime 产生了更多事件”解释性能下降。

## network namespace 是使结果翻转的已确认变量

### 10 次单变量 A/B

1KB、同一 v0.2.0 Docker 镜像：

| Docker 配置 | Baseline | Kernel | BPFtime | BPFtime vs Kernel |
|---|---:|---:|---:|---:|
| host IPC，private network | 16002.45 | 10833.87 | 11287.40 | **+4.20%** |
| private IPC，host network | 14906.89 | 10208.72 | 10025.16 | **-1.80%** |
| host IPC，host network | 14946.36 | 10132.17 | 9946.78 | **-1.83%** |

IPC namespace 的变化没有改变方向；只要切换为 host network，BPFtime 相对 kernel 就从正数变为负数。

### 交错 5 轮复验

为降低时间漂移和热状态偏差，private/host netns 按交错顺序各跑 5 次：

| 网络环境 | Baseline | Kernel | BPFtime | 配对 BPFtime vs Kernel |
|---|---:|---:|---:|---:|
| Private netns | 16036.38 | 10910.30 | 11307.24 | **+3.67%** |
| Host netns | 14960.33 | 10162.59 | 9951.19 | **-2.06%** |

private 与 host 的相对差为 **5.72pp**。从 private 切换到 host 后：

- Baseline：-6.71%
- Kernel：-6.85%
- BPFtime：-11.99%
- BPFtime 相比 kernel 额外敏感约 5.14pp

因此不是“network namespace 只影响 BPFtime”，而是 host netns 让所有端到端吞吐下降；BPFtime 因 userspace reader 已接近 CPU 瓶颈而受到更大的非线性影响。

## Tailscale 是 host 网络环境中的主要影响因素

### netfilter 链结构

Tailscale 在 host network namespace 的 netfilter `INPUT` 链前部安装无条件跳转：

```text
-A INPUT -j ts-input
```

进入 `INPUT` 链的报文会先跳到 `ts-input` 进行规则匹配，未被处理的报文再返回原 `INPUT` 链。benchmark 使用 host loopback 时，`127.0.0.1` 流量也会进入 host netfilter 路径。

### private netns 模拟 `ts-input`

在 private netns 中模拟 Tailscale INPUT 规则后，1KB 10 次结果为：

| 条件 | BPFtime vs Kernel | 标准差 | 胜出次数 |
|---|---:|---:|---:|
| Private netns，未模拟 | +4.20% | 2.28pp | 9/10 |
| Private netns，模拟 `ts-input` | +0.34% | 4.20pp | 5/10 |

模拟链使相对优势损失约 3.87pp，说明 Tailscale/netfilter 规则具有贡献，但该实验没有完整复现 host netns 的 -1.80%。

### 只绕过 host `INPUT -> ts-input` 没有恢复性能

随后在 host `INPUT` 链最前面插入针对 `127.0.0.0/8` 的 `ACCEPT`，让 benchmark loopback 包在进入 `ts-input` 前返回。规则计数器记录约 **8,012,589** 个命中包，证明绕过实际生效；但 1KB 10 次中 BPFtime 仍比 kernel 低 6.08%，0/10 胜出。

这条反证说明：**仅遍历 `ts-input` 一条链不足以解释全部差异。**其他 filter/mangle/nat 规则、conntrack、mark、tailscale0、后台流量或调度竞争仍可能参与。

### 关闭 Tailscale 的完整 payload 测试

关闭 Tailscale 后跑了一次 v0.2.0 host 全 payload。下表将该单轮与此前开启 Tailscale 的 host 10 次平均比较：

| Payload | 开启 Tailscale：BPFtime vs Kernel | 关闭 Tailscale：BPFtime vs Kernel | 改善 |
|---|---:|---:|---:|
| 16B | -5.62% | -1.59% | +4.03pp |
| 1KB | -4.01% | -0.81% | +3.20pp |
| 2KB | -5.01% | -2.80% | +2.21pp |
| 4KB | -3.43% | +1.66% | +5.09pp |
| 16KB | -8.83% | -6.45% | +2.38pp |
| 128KB | -9.47% | -3.13% | +6.34pp |
| 256KB | -10.02% | +0.54% | +10.56pp |

关闭 Tailscale 后的绝对变化：

- Baseline：+0.04%～+1.83%
- Kernel：-3.04%～+2.43%，整体接近波动范围
- BPFtime：**全部 payload 均提高 3.71%～8.34%**

这组结果说明 Tailscale 对 BPFtime 具有明显的额外影响，而不是简单地等比例降低 benchmark 的全部三组数据。由于关闭 Tailscale 同时改变多个 netfilter 表、conntrack/mark 状态、接口和 daemon 活动，这项测试支持“Tailscale 整体网络集成是主要因素”，但不支持“已证明只由 `ts-input` 单链造成”的更强因果表述。

## 最新版结果与 reader 优化解释一致

### 最新版 host 5 次平均

在 Tailscale 正常运行的 host 环境中：

| Payload | BPFtime vs Kernel | BPFtime 胜出次数 |
|---|---:|---:|
| 16B | +6.24% | 4/5 |
| 1KB | +3.40% | 4/5 |
| 2KB | +3.17% | 4/5 |
| 4KB | +7.75% | 5/5 |
| 16KB | -3.84% | 0/5 |
| 128KB | +1.05% | 4/5 |
| 256KB | -3.61% | 0/5 |

最新版已重新复现“小 payload BPFtime 优于 kernel，大 payload 优势下降或转负”的预期趋势。

### 最新版 Docker 两次平均

| Payload | BPFtime vs Kernel |
|---|---:|
| 16B | +15.17% |
| 1KB | +0.36% |
| 2KB | +8.31% |
| 4KB | +13.51% |
| 16KB | +2.06% |
| 128KB | +1.97% |
| 256KB | +8.44% |

只有两轮，不能用于精确估计均值和方差，但方向上显示最新版相对 v0.2.0 的提升更大。

保存的 5 秒 perf 样本中，最新版 BPFtime reader 为 9.65B instructions，v0.2.0 后续可比复测为 12.06B instructions，约下降 20%。与早期 v0.2.0 高成本样本 27.33B 相比则约下降 65%；由于这些 perf 样本并非严格交错、完全同条件的跨版本实验，所以只作为优化方向证据，不把 20% 或 65% 当作精确版本收益。

## 最终结论

### 已确认

1. v0.2.0 no-BTF 代码和 tracer 功能正常，host 负结果不是加载失败或错误数据。
2. LLVM/Clang、Boost、root 身份、benchmark 顺序和单纯的 runtime 二进制差异均不能解释 host/Docker 结果翻转。
3. v0.2.0 BPFtime userspace reader 在 Jetson 上具有较高固定成本，并接近单核 CPU 预算。
4. Docker private netns 与 host netns 的单变量 A/B 可以稳定翻转 BPFtime 相对 kernel 的结果。
5. 关闭 Tailscale 后，kernel 基本保持不变，而 BPFtime 全 payload 提升 3.71%～8.34%。因此 Tailscale host 网络集成是 v0.2.0 host 性能异常的主要环境因素。
6. 最新版降低 reader/runtime 成本后，即使在相同 host/Tailscale 环境中，小 payload 仍可保持正收益。

### 尚未精确证明

1. 性能差异由 `INPUT -> ts-input` 这一条跳转单独造成。
2. netfilter、conntrack、mark、softirq、调度竞争各自贡献了多少。
3. 单次关闭 Tailscale 的结果能否给出稳定的精确改善百分比。

这些未决项不影响当前 benchmark 的操作性结论，但如果论文需要把根因写成某一条具体 netfilter chain，则仍需更细粒度的逐表、逐链 factorial A/B。

## 建议的 benchmark 规范

1. 同一组对比必须固定 network namespace；不要把 Docker private netns 与 host netns 结果直接混为一组。
2. 在 host 跑网络/loopback benchmark 时，记录 Tailscale、VPN、容器网络和 netfilter 状态。
3. 对外报告至少运行 5～10 次，给出均值、样本标准差和胜出次数。
4. 若目标是比较 bpftime 与 kernel 本身，优先使用干净 private netns，或在本地控制台下暂时停止 Tailscale后测试。
5. 保留 `iptables-save`/`nft list ruleset`、CPU power mode、工具链版本、镜像 ID 和结果原文件。
6. 最新版与 v0.2.0 的性能差异应单独报告，不应把最新版的 reader 优化倒推为 v0.2.0 的固有表现。

## 证据文件索引

### v0.2.0 host 与 Docker

- [v0.2.0 host 10 次完整测试](../benchmark-results/v0.2.0/full/host-10x-20260713/)
- [v0.2.0 Docker 5 次完整测试](../benchmark-results/v0.2.0/full/docker-5x-20260713/)

### namespace A/B

- [host IPC + host network，1KB 10 次](../benchmark-results/v0.2.0/network/namespaces/v020-docker-host-ipc-network-1kb-10x-20260714_021946/summary.json)
- [host IPC + private network，1KB 10 次](../benchmark-results/v0.2.0/network/namespaces/v020-docker-host-ipc-only-1kb-10x-20260714_022957/summary.json)
- [private IPC + host network，1KB 10 次](../benchmark-results/v0.2.0/network/namespaces/v020-docker-host-network-only-1kb-10x-20260714_023824/summary.json)
- [private/host netns 交错 5 轮](../benchmark-results/v0.2.0/network/namespaces/v020-docker-netns-interleaved-1kb-5x-20260714_031708/summary.json)

### Tailscale/netfilter

- [private netns 模拟 Tailscale INPUT，1KB 10 次](../benchmark-results/v0.2.0/network/tailscale/v020-docker-private-net-ts-input-sim-1kb-10x-20260714_025103/summary.json)
- [host loopback INPUT bypass，1KB 10 次](../benchmark-results/v0.2.0/network/tailscale/v020-host-loopback-bypass-1kb-10x-20260714_030542/summary.json)
- [host loopback bypass 规则与计数器](../benchmark-results/v0.2.0/network/tailscale/v020-host-loopback-bypass-1kb-10x-20260714_030542/metadata.txt)
- [关闭 Tailscale 的 v0.2.0 host 完整结果](../benchmark-results/v0.2.0/full/host-tailscale-off-20260714_162723/size_benchmark_20260714_162723.txt)

### perf 与最新版

- [v0.2.0 host BPFtime 1KB perf 复测](../benchmark-results/v0.2.0/perf/v020-host-bpftime-1kb-perf-recheck-20260712_223610/v020-host-bpftime-1kb-perf-recheck-20260712_223610.perf.txt)
- [v0.2.0 Docker BPFtime 1KB perf](../benchmark-results/v0.2.0/perf/v020-docker-bpftime-1kb-perf-20260712_222632/v020-docker-bpftime-1kb-perf-20260712_222632.perf.txt)
- [v0.2.0 host/Docker reader 对比原始 perf 与 wrk TXT](../benchmark-results/v0.2.0/perf/v020-host-vs-docker-reader-20260712_223747/)
- [最新版 host BPFtime 1KB perf](../benchmark-results/latest/perf/latest-host-bpftime-1kb-perf-20260712_220903/latest-host-bpftime-1kb-perf-20260712_220903.perf.txt)
- [最新版 host kernel 1KB perf](../benchmark-results/latest/perf/latest-host-kernel-1kb-perf-user-20260712_221247/latest-host-kernel-1kb-perf-user-20260712_221247.perf.txt)
- [最新版 host 5 次汇总](../benchmark-results/latest/full/host-5x-20260712/summary_5runs.txt)
- [最新版 Docker 两次结果目录](../benchmark-results/latest/full/docker-2x-20260714/)

## 方法说明与限制

- 文中采用表格而非图形，因为核心证据是少量离散 A/B 条件及精确数值，表格更便于逐项审计。
- 最强证据是同一镜像、单变量、交错顺序的 private/host netns A/B；跨时间的 host 与 Docker完整结果只用于一致性检查。
- Tailscale-off 完整 payload 目前只有一轮，适合判断方向，不适合估计稳定方差。
- perf 统计来自多个诊断阶段，部分运行模式、输出选项和时间点不同，因此用于定位热点与解释方向，不用于给出严格的跨版本因果效应大小。
- 本报告区分“操作性根因范围”和“单链机制归因”：前者已经足够解释 benchmark 结果，后者仍需更细粒度实验。
