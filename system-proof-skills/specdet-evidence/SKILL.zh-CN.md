---
name: specdet-evidence
description: 为 system proof agent 收集有预算、绑定源码的 Verus 契约确定性证据，用于欠约束检查和 caller/proof-map edge 的证据记录，不代替 agent 判断规格质量。
---

# 收集契约确定性证据

[English](SKILL.md) · [使用方式、证据字段与术语](README.zh-CN.md)

把 `specdet` 当作机械证据工具。正常调用不需要另起 agent、开启 LLM 辅助，或为了运行成功而替换源契约。

## 输入

准备 caller goal / proof-map edge、准确目标、源码 revision 与工作树 hashes、预期观察范围、checker revision、匹配的项目 profile / Verus 工具链，以及本地证据目录。记录输入属于**原生源码**、**封存的源码提取**还是**手写模型**；工具无法自动判定模型与原系统的对应关系。

本工作区应使用 `/home/ruize/system-proof-agent/spec-completeness-checking/.venv/bin/python`，不要误跑 `tools/` 下的旧副本。其他环境的安装方式见使用说明。

## 步骤

1. 检查前保留候选。根据真实构建上下文选择 `verus.single_file`、`verus.native` 或 `verus.cargo`，不静默把 crate 简化成单文件。
2. 使用 profile 和 target 调用 `python -m specdet analyze`，加上 `--compact-json --llm-fallback off --offline`，显式指定证据输出位置。命令见[使用说明](README.zh-CN.md#运行)。默认整次分析预算为 60 秒，`--run-timeout` 可覆盖；`--timeout` 仍只限制单次 verifier 调用。
3. 分开读取执行 `status` 和语义 `verdict`。保留原始 `baseline`、`problem_id`、实际观察策略、排除项及可行性/翻译边界。查看 `decisive_evidence` 和 `counterexample`，不只看退出码或整个构建的 verified 数量。
4. 按需打开相关证书、observation plan、query 或诊断 artifact。精简预览不是完整证据；遇到 omitted/truncated 标记时回看完整报告。源码快照和原始日志保留在本地。
5. 将下面的证据记录附到 caller/proof-map edge。超时后保留已完成结果，明确记录当前未完成目标和 `pending_targets`，不补造结论。

## 证据交付

```text
调用者提供：caller_goal、proof_map_edge、native/extract/model 范围、
            源码 revision + 工作树 hashes、checker revision、提取/模型假设
工具提供：  run_dir/full_report、target/source_digest、problem_id、
            status/verdict、原始 baseline、proof/witness artifact + digest、
            policy/exclusions/coverage、diagnostics、用时与预算
```

不把 `deterministic` 写成“规格足够/实现正确”，不把 UNKNOWN 或超时写成“发现 bug”，不把条件性替代写成无条件反例，不把局部 UNSAT 写成全局证明。已验证的构造性见证可以与原始 UNKNOWN 同时存在。这个自由度是否影响 caller、是否符合意图、是否需要改规格，仍由 system proof agent 和项目负责人判断。
