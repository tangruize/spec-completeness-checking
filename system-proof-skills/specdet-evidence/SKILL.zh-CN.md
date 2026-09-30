---
name: specdet-evidence
description: "主要用于攻击 top-level intent contract 和 assumed/opaque TCB contract 中返回值或后状态的欠约束；对已验证的中间层，只在当前证明或抽象边界依赖输出唯一性、状态保持时选择性使用。"
---

# 检查一个候选契约是否存在可观察欠约束

[English](SKILL.md) · [完整用法与术语](README.zh-CN.md)

本 skill 只回答一条明确的 caller/proof-map 问题。它不是每个 reachable function 都必须运行的流水线，也不是 specification completeness oracle。

## 在 proof stack 中哪里最有价值

按契约在证明栈中的角色确定优先级：

| 边界 | Attack 价值 | 原因 |
|---|---|---|
| Top-level intent/goal contract | 高 | 证明只能建立写下来的目标；证明成功不能机械地说明这个目标完整表达了 Human intent。应攻击任何可能削弱目标的 result/state 自由度。 |
| Assumed、external 或 opaque TCB contract | 高 | 没有 verified body 独立检查契约。遗漏的 result relation 或 frame condition 会使 trusted boundary 缺少本应提供的 guarantee。 |
| 已验证的中间层 contract | 选择性 | 如果实现满足它，并且 top-level theorem 只使用它就能成功，未提及的维度可能与当前目标无关。只有当它阻塞 proof frontier、构成复用抽象边界、向 caller 隐藏 effect，或自身被当作 assumption 使用时才 attack。 |

这不表示中间层规格永远不重要：proof 可能通过展开代码绕过一个过弱 contract，而另一个 caller 只能使用 contract。反过来，如果当前 top-level property 不观察、也不依赖某个遗漏维度，就不应对每个 internal helper 消耗 completeness budget。

本 attack 检查的是 **欠约束**：契约是否允许多个 caller 可区分的 result 或 post-state。它不能发现一个 **过强或不真实的 assumption** 是否没有被实现。因此 TCB review 还需要 source/model correspondence、可行时的 implementation validation，以及对 trusted assumption 的 Human review。

## 它怎样帮助 agent 写规格

System-proof agent 通常先从 caller、实现和周围 invariant 得到一个暂定契约。在投入 implementation proof 之前，本 skill 攻击一个问题：

> 这个 candidate 是否真的决定了 direct caller 所依赖的 result 和 post-state 部分？

把证据放入规格编写闭环：

| 写规格阶段 | Attack 可能揭示什么 | Agent 应采取的动作 |
|---|---|---|
| 第一版 candidate | 两个 caller 可区分的返回值都满足契约 | 把差异追溯到 caller requirement，编写最小且有依据的 result relation。 |
| Mutable operation | 返回值固定，但允许两个 caller 可区分的 post-state | 编写所需 state relation 或 frame condition，保护必须保持的状态。 |
| Proof failure | 存在 verified alternative output | 不只修改 proof，也把 contract 作为 revision candidate。 |
| Candidate revision | 旧 witness 仍然成立 | Revision 没有消除该自由度；继续修订，或明确记录它是有意的。 |
| Candidate revision | 原 witness 被拒绝，并且完整 global uniqueness proof 成功 | 记录“该歧义已被消除”的支持证据，再继续 feasibility 和 implementation proof。 |

例如，初稿可能只规定 allocation 成功。如果 caller 要求返回 identifier 必须指向刚插入的对象，而初稿允许两个不同 identifier，那么 witness 暴露的是可能缺少 caller-result relation。一个 mutator 也可能只约束返回值，却没有约束无关 map entry 保持不变；两个允许的 post-state 暴露的是可能缺少 frame condition。反过来，如果 caller 本来就接受两个输出，则应保留自由度，而不是为了确定性盲目加强契约。

工具不会自己编写缺失条款。它只是把契约允许的自由度具体化。Agent 必须把不同的 output/state 维度连接到 caller requirement，提出由该 requirement 支持的最小条款，replay witness，然后证明实现满足 revision。

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
   specdet analyze \
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
6. **选择下一项权威检查。** 如果 evidence 挑战 candidate，识别不同的 result/state 维度，追溯排除该差异的 caller requirement，只增加由它支持的 result relation 或 frame condition，然后 replay witness。否则保留有意自由度、建立输入可行性、证明实现满足契约、分析 cross-operation invariant，或请求 bounded Human intent review。

## 交付格式

```text
active_goal:
direct_caller / blocking_edge:
candidate_revision + provenance:
attack_question + justified_observation_relation:
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
- 观察关系和抽象可能隐藏表示差异；改变它们就是改变问题。
- Native、extract 和 model 证据有不同 trust boundary；模型对应关系需要另行建立。
- 局部/refinement UNSAT 不是全局证明；没有输入可行性的条件性替代不是无条件 witness。
- Async 契约、任意 opaque ownership/resource 构造、部分宏和量词密集问题可能仍不支持或返回 UNKNOWN。
