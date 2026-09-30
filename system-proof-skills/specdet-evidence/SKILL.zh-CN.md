---
name: specdet-evidence
description: "当 system-proof 的某条 caller edge 可能依赖一个 Verus 契约是否唯一决定可观察返回值或后状态时使用；运行有预算、绑定源码的确定性 attack，保留 proof/witness 证据并返回 caller-oriented 结果，不代替 agent 判断规格充分性。"
---

# 检查一个候选契约是否存在可观察欠约束

[English](SKILL.md) · [完整用法与术语](README.zh-CN.md)

本 skill 只回答一条明确的 caller/proof-map 问题。它不是每个 reachable function 都必须运行的流水线，也不是 specification completeness oracle。

## 何时使用

只有当以下证据可能改变当前 caller edge 时使用 `specdet`：

- 暂定或修订后的契约可能没有约束返回值或可变 post-state；
- 两个候选规格需要一个关于输出唯一性的具体区分；
- proof failure 更像缺少 frame/result relation，而不只是缺 lemma；
- 旧 witness 应成为后续候选的 regression；
- 可以把 Human 问题压缩为“这个已证明存在的自由度是否符合意图？”

如果性质跨多个 operation、生命周期阶段、callback、并发或 `await` 区间，不使用本工具代替 protocol/invariant analysis。也不要仅因函数可达就运行。

## 必需上下文

调用前记录：

- active top-level goal、direct caller 和准确 blocking edge；
- 准确 target 与保留的 candidate revision；
- caller 真正关心哪些 return/post-state 差异；
- native source、sealed extract 或 authored model 来源；
- source/dirty-worktree hashes、checker revision 与匹配的 Verus profile；
- 有界的输出目录和预算。

如果 intended observation 不清楚，先停止并解决这个歧义。方便的默认策略不能代替 caller 语义。

## 步骤

1. **形成 attack 问题。** 明确写出：“对同一个允许输入，此候选是否允许两个在 `<caller-relevant observation>` 上不同的输出？”如果唯一性不能帮助 caller，就不运行。
2. **冻结候选和范围。** 根据真实源码上下文使用 `verus.single_file`、`verus.native` 或 `verus.cargo`。不静默把 native source 换成 model，也不为获得成功结果而修改条款。
3. **运行一个有预算的目标。**

   ```bash
   /home/ruize/system-proof-agent/spec-completeness-checking/.venv/bin/python -m specdet analyze \
     --config /path/to/specdet.toml --target 'src/component.rs:operation' \
     --verus /path/to/verus --llm-fallback off --offline \
     --run-timeout 60 --out /path/to/evidence --compact-json
   ```

4. **按记录的 evidence 分类，不只看文字 verdict。**

   | 记录结果 | 对 system proof 的作用 |
   |---|---|
   | `nondeterministic` 且有 verified witness/SAT evidence | 在选定观察上挑战 candidate；保留两份输出和证书，但不自动等于 bug。 |
   | `deterministic` 且有完整 proof/原问题 UNSAT | 支持冻结契约在该观察关系下的唯一性；不证明 adequacy、feasibility 或 implementation correctness。 |
   | `inconclusive` / `timed_out` | 没有决定语义问题；保留部分证据和准确的 tool/solver/constructor blocker。 |
   | `not_evaluated`、`failed` 或 `unsupported` | 不提供确定性结论；修正输入/profile，或记录支持范围边界。 |

   分开保留 `status`、`verdict`、原始 `baseline`、`problem_id`、`decisive_evidence`、coverage、排除项和预算。原始 baseline UNKNOWN 可以与已验证构造性见证并存。
5. **回到 direct caller。** Proof-map effect 只能写成：
   - `challenges candidate`：已证明存在的自由度与 caller 需要冲突；
   - `supports uniqueness only`：减少一个歧义，但没有关闭 caller edge；
   - `does not decide`：结果有条件、不支持、超时或未决。
6. **选择下一项权威检查。** 根据 caller 问题：保留有意的非确定性、修订候选并 replay witness、建立输入可行性、证明实现满足契约、分析 cross-operation invariant，或请求 bounded Human intent review。

## 交付格式

```text
active_goal:
direct_caller / blocking_edge:
candidate_revision + provenance:
attack_question + observation_policy:
status / verdict / raw_baseline:
decisive evidence or blocker:
source_digest / problem_id / run_dir:
ignored dimensions + feasibility/translation boundary:
proof-map effect: challenges candidate | supports uniqueness only | does not decide
next authoritative check:
```

不把工具输出写成“spec complete”“goal closed”或“发现 bug”。自由度是否重要、规格是否应修改，由 agent/项目 authority 判断。

## 停止条件

得到一份可重放、能回答当前 caller 问题的结果后停止；预算到期后停止；发现所需性质不属于单契约确定性时停止。不要扩展到无关 target，也不要在没有新 caller hypothesis 时反复增加预算。

## 硬边界

- 确定性只是欠约束的一个维度；确定的契约仍可能错误、过强或不可行。
- “Global”只指冻结 target 与观察关系下的全部允许建模输入，不是整个系统或未提及的 heap。
- View 和观察策略可能隐藏表示差异；改变策略就是改变问题。
- Native、extract 和 model 证据有不同 trust boundary；模型对应关系需要另行建立。
- 局部/refinement UNSAT 不是全局证明；没有输入可行性的条件性替代不是无条件 witness。
- Async 契约、任意 opaque ownership/resource 构造、部分宏和量词密集问题可能仍不支持或返回 UNKNOWN。
