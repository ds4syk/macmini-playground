# 自训模型本地推理实测

日期：2026-11-12。机器：Mac mini M4，10 核，16GB 统一内存。**只加载用户自训的 VLA/Share 检查点，不涉及任何外部 LLM。**

## 环境

```bash
cd ~/labs/VLA/Share/v2
~/miniconda3/bin/python -m venv --system-site-packages .venv
.venv/bin/python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple torch scipy
```

`torch 2.15.0`，MPS 可用；`numpy`/`pandas`/`scipy` 已就绪。模型权重在 `local_data/models/latest_*.pt`，数据在 `local_data/panel.npz`。

## 复现

```bash
cd ~/labs/VLA/Share/v2
.venv/bin/python /Users/suyingke/.superagent-work/paper-trading-platform-fe9b2dd99152/scripts/bench_local_models.py
```

## 实测（2026-11-09，300 只有效股票，输入 300×60×25）

| 模型 | CPU 延迟 | CPU 吞吐 | MPS 延迟 | MPS 吞吐 |
| --- | ---: | ---: | ---: | ---: |
| GRU + Attention | 16.9 ms | 17,700 股/s | 6.0 ms | 49,600 股/s |
| TCN | 125.3 ms | 2,400 股/s | 3.5 ms | 86,400 股/s |
| Transformer | 21.3 ms | 14,100 股/s | 8.0 ms | 37,500 股/s |
| CNN | 35.7 ms | 8,400 股/s | 1.6 ms | 182,900 股/s |
| GDN + NTP | 121.1 ms | 2,500 股/s | 92.7 ms | 3,200 股/s |

- 五条路线各跑一遍（300 只股票）：MPS 合计约 **0.11 秒**；CPU 合计约 **0.32 秒**。GDN 在 MPS 上反而慢，单独用 CPU 更合适。
- 这是**模型前向**的耗时。真正的日更瓶颈是把新行情整理成 60×25 特征（panel 准备、校验与落盘），不是推理。
- 显存/内存：模型 3–10 万参数，远小于 16GB；MPS 与 CPU 共享统一内存，峰值远低于 1GB。
- 2026-11-13 已在本机补算 11-02→11-12 的特征与分数：BaoStock 增量 → `prepare.py` 重建 panel → `latest_*.pt` 打分，最后一次自训信号 2026-11-12。
- 本地只跑自训模型；LLM 家族使用云端 DeepSeek/智谱 API，不在本机运行。
