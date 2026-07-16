# bpftime benchmark results

本目录按代码版本和实验类型归档。新结果脚本默认直接写入对应版本的 `full` 或 `payload-tests` 子目录。

## 目录结构

```text
benchmark-results/
├── latest/
│   ├── full/                 # 最新版完整 payload 测试
│   └── perf/                 # 最新版 perf/reader 诊断
└── v0.2.0/
    ├── full/                 # v0.2.0 完整 payload 测试
    ├── figures/              # v0.2.0 汇总图与绘图数据
    ├── network/
    │   ├── namespaces/       # IPC/network namespace A/B
    │   └── tailscale/        # Tailscale/netfilter 实验
    ├── perf/                 # reader/perf 诊断
    └── raw-generated/        # 仓库 ssl-nginx 目录中提取的旧短测 JSON
```

## 关键完整结果

| 路径 | 内容 |
|---|---|
| `v0.2.0/full/host-single-20260712_202038/` | 最初的 v0.2.0 host 单轮完整结果 |
| `v0.2.0/full/host-10x-20260713/` | v0.2.0 host 10 轮完整结果 |
| `v0.2.0/full/docker-5x-20260713/` | v0.2.0 Docker 5 轮完整结果 |
| `v0.2.0/full/docker-hostnet-1x-20260716_190903/` | v0.2.0 Docker host network 单轮完整结果 |
| `v0.2.0/full/host-tailscale-off-20260714_162723/` | 关闭 Tailscale 后的 v0.2.0 host 单轮完整结果 |
| `v0.2.0/figures/v020-bpftime-throughput-by-payload.png` | 四种运行环境的英文吞吐量折线图 |
| `latest/full/host-5x-20260712/` | 最新版 host 5 轮完整结果与汇总 |
| `latest/full/docker-2x-20260714/` | 最新版 Docker 两轮完整结果 |

## 说明

- `bpftime-v020-no-btf/benchmark/ssl-nginx` 中原有的 167 个生成结果已全部移动到这里，源码目录不再保存运行产物。
- v0.2.0 host 10 轮在 repo 中留下的 `size_benchmark_*` 与脚本保存的 run TXT 内容重复，原始副本保留在 `v0.2.0/full/host-10x-20260713/raw-generated/`，没有删除。
- `raw-generated/ssl-nginx/legacy-and-short-tests/` 保存无法可靠归属到单次完整 campaign 的旧短测 JSON。
- 结果归档以 `.json` 和 `.txt` 为主；`figures/` 额外保存最终 PNG、SVG 与绘图 CSV。原始 `.log` 和 perf data 仍只保留在工作区外部的 `/home/jetson/src/benchmark-results`，未提交。
- Requests/sec 越高越好；相对提升统一使用 `(BPFtime - Kernel) / Kernel × 100%`。
- 诊断总结位于 `../summry/bpftime-v020-jetson-host-network-validation.md`。
