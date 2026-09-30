# 面向 system proof agent 的 Specdet 证据说明

[English](README.md) · [中文 skill](SKILL.zh-CN.md) · [English skill](SKILL.md)

集成时选择一个语言版本的 skill，不重复加载。本目录说明如何调用本地工具，不增加 agent adapter、工作流或规格质量判定器。

## 用途与术语

`specdet` 检查选定契约是否允许同一输入对应两个可观测不同的输出：

```text
P(input) && Q(input, output1) && Q(input, output2) && !E(output1, output2)
```

`P` 是前置条件，`Q` 是后置条件，`E` 是选定的输出等价关系；输出包括返回值和可变参数的最终状态。这提供的是**契约确定性**证据，不是“需求已全部表达”或“系统顶层目标已证明”的结论。

| 术语 | 含义 |
|---|---|
| 全局（global） | 对选定契约与观察关系下的所有允许输入而言，不是整个系统，也不包括未建模的 heap。 |
| 观察关系 | 由 caller 语义论证的输出与 post-state 等价关系；必须明确哪些维度被比较、抽象或忽略。 |
| Concrete / abstract determinism | 默认固定具体输入；可选的 abstract 分析比较 View 相同的输入，这是另一个问题，通常先要求 concrete 确定性成立。 |
| Witness / counterexample（见证/反例） | 一个输入与两个满足原契约但可观测不同的输出；这种非确定性可能符合设计意图。 |
| 源级构造性见证 | Verus 验证具体构造、原始条件和输出不等价；这是证书，不是把原始 SMT UNKNOWN 改写为 SAT。 |
| 局部切片（local slice） | 加入额外输入约束后的子问题；切片 UNSAT 不证明全局确定性，输入不可行时还可能真空成立。 |
| 条件性替代 | 假设存在合法输入进行的证明，没有实际构造该输入；没有可行性证据时不是无条件反例。 |
| Frame condition | 规定哪些状态保持不变的条款；返回值固定不代表 heap 未被改变。 |
| UNKNOWN / inconclusive | 当前证明、翻译和搜索没有决定该问题；不是 bug 报告，也不是 completeness 证书。 |

任何内置 comparison profile 都不是普遍正确的“相同输出”定义。解释结果前，必须从 direct caller 或系统性质推导观察关系，并记录所有被忽略的维度。Profile 名称只是工具配置，不是可以直接写进 proof map 的语义结论。改变观察关系就是改变问题，因此不能把一个关系下的证据当成另一个关系下的证据。

## 它在 system proof agent 中能提供什么帮助

System-proof workflow 同时需要 **specification judgment** 和 **proof-map-guided reintegration**。`specdet` 只为这个闭环提供一种有界证据：

```text
candidate contract
→ determinism attack
→ proof/witness 或明确 blocker
→ 回接 caller/proof map
→ 保留、修订或淘汰 candidate
```

具体帮助包括：

- **攻击暂定规格：** 当 result 或 frame 可能遗漏时，生成一对可重放、都满足原契约的不同输出。
- **区分候选：** 证明某个 revision 仍允许 caller-relevant freedom，或另一个 revision 在同一观察关系下获得唯一性支持。
- **把 proof feedback 转成规格问题：** 区分“证明缺一个 lemma”和“契约本身允许多个结果”。
- **让局部证据可审计：** 将结论绑定到 source hashes、`problem_id`、经论证的观察关系、proof/witness 证书和预算，再回到 direct caller，而不是把局部结果直接计成 top-level closure。
- **支持自主迭代：** 保存失败、超时和旧 witness，使 agent 可以先修订并 replay，减少不必要的 Human 同步等待。
- **压缩 Human review：** 有机械证据后，剩余问题可缩小为“这个自由度对 caller 是否有意”，而不是让 Human 重新审查整份规格。

工具不会自己选择 candidate、判断 intent 或更新 proof map。Agent 需要将其与 implementation proof、测试、源码分析、protocol invariant 和 bounded Human review 结合。Skill 也可能给 planning 带来偏置或额外成本，因此只在一条明确的 caller-relevant uniqueness 问题上调用；实验中仍值得比较 skill-on/off。

## 潜在限制

| 限制 | 对 agent 的含义 |
|---|---|
| 确定性比 adequacy 窄 | 唯一输出仍可能是错误行为、缺少必须成功的要求，或依赖不可行前提。 |
| 结果依赖观察关系 | 粗粒度关系可能合并 caller 关心的差异；过度具体的关系也可能把系统有意抽象的表示自由暴露出来。 |
| 单个契约不是 protocol | Cross-operation preservation、生命周期、并发、callback、failure compensation 和跨 `await` 性质需要其他分析。 |
| 契约证据不是实现符合性 | 确定的 postcondition 不证明实现满足它；原生实现验证是另一份证据。 |
| Source、extract、model 的 trust 不同 | Sealed extract 必须记录省略/stub；authored model 需要独立的 source-to-model correspondence。 |
| Solver/search coverage 不完整 | UNKNOWN、超时、不支持的构造器或有限候选预算，都不表示确定或不存在 witness。 |
| 通常不检查 feasibility 和 termination | 没有合法输入时 global UNSAT 可能真空成立；条件性替代不是无条件反例。 |
| 语言/后端支持有界 | Async 契约、opaque ownership/resource、部分宏和量词密集目标仍可能不支持或未决。 |

## 准备

使用[维护中的 fork](https://github.com/tangruize/spec-completeness-checking/tree/improve-real-system-evidence)，分支为 `improve-real-system-evidence`。需要 Python 3.11+、Git、用于编译固定 grammar 的 C 编译器，以及另外安装的、与目标项目匹配的 Verus 工具链。

本说明对应提交 [`3585e71`](https://github.com/tangruize/spec-completeness-checking/commit/3585e713ac1bbfd6eca53cdfa6118d93fac2b452)，包含精简证据输出和默认整次运行超时。

```bash
cd /path/to/spec-completeness-checking
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[verus]'
.venv/bin/python -m specdet doctor --verus /path/to/verus
git rev-parse HEAD
```

本工作区的新版本位于 `/home/ruize/system-proof-agent/spec-completeness-checking`；`tools/spec-completeness-checking` 是另外一份旧副本。请使用新 checkout 的 `.venv/bin/python`，或在自己的环境中安装 fork。

## 运行

从匹配的[项目 profile](https://github.com/tangruize/spec-completeness-checking/tree/improve-real-system-evidence/examples/real_systems) 开始。自包含文件用 `verus.single_file`；crate 用指定 entrypoint 的 `verus.native`，或保留真实 package/features/injection file 的 `verus.cargo`。依赖及 Rust/Verus 配置也必须匹配，不能静默退化成简化模型。

```bash
.venv/bin/python -m specdet analyze \
  --config /path/to/specdet.toml --target 'src/component.rs:operation' \
  --verus /path/to/verus --llm-fallback off --offline \
  --run-timeout 60 --timeout 30 --solver-timeout-ms 1000 \
  --max-rounds 8 --counterexample-candidates 16 \
  --out /path/to/evidence --compact-json
```

配置中的相对路径相对于配置文件解析。输出与源码输入分开存放。Target include 只选择声明，不决定整个快照的复制范围：排除构建产物、旧运行和无关目录，不要把 session artifacts 目录当项目根目录。

**默认整次分析预算是 60 秒**，准备阶段和所有目标共享。用 `--run-timeout 180`，或 `[limits] run_timeout_seconds = 180` 延长；`--timeout` 仍只限制单次 verifier。到期后停止本次启动的进程并保存部分证据，清理可能使实际退出稍晚，报告会如实计时。取消机制目前要求 POSIX 主线程；其他线程应调用 CLI。Profile runner 中独立的 probe 或实现验证不自动属于 `analyze()` 的预算。

同时记录 stdout JSON 和退出码，不因非零退出码就丢弃有效证据。

| 退出码 | 含义 |
|---|---|
| `0` | 所有适用的已分析目标均确定。 |
| `1` | 存在确认的非确定性。 |
| `2` | 配置、执行或支持范围失败。 |
| `3` | 仍有未决结果，包括整次运行超时。 |
| `130` | 调用被中断。 |

执行错误优先；超时前已确认的非确定性仍可能保留退出码 `1`。必须同时看执行 `status` 和每个目标的 `verdict`。

## 读取与保留证据

`--compact-json` 的格式为 `specdet.summary.v1`。较大值和额外诊断有明确的省略/截断标记，完整 artifact 不截断。

| 字段 | 提供的证据 |
|---|---|
| `target.source_digest`、`problem_id` | 源码身份与冻结的契约/观察问题。 |
| `status`、`verdict`、`baseline` | 执行状态、语义结论，以及未被改写的原始 query 状态和原因。 |
| `decisive_evidence`、`counterexample` | 决定性 proof/query 或已验证见证、具体值、摘要和证书链接；UNKNOWN baseline 与构造性见证可以并存。 |
| `coverage` | 策略、忽略维度、翻译可信性及可行性/heap 边界；`trusted_translation` 不证明手写模型对应真实系统。 |
| `resources`、`duration_ms`、`run_timeout_seconds` | 使用量、上限和实际时间；批次中断且无法确定最终次数时，`used = null`，`known_used` 只是下界。 |
| `diagnostics`、`artifacts`、`full_report`、`pending_targets` | 阻塞原因、详细证据位置、完整报告，以及超时前没有开始的目标。 |

缺少 opaque pointer 构造器不等于反例搜索耗尽：Nanvix 的 `NonNull<u8>` 案例实际尝试了零个候选。解析/能力失败、单次调用超时、总预算到期和 solver UNKNOWN 也应分别保留。Baseline 为 null 表示没有记录到原始 query 结果，不等于 solver 返回 UNKNOWN。

```bash
.venv/bin/python -m specdet report --run /path/to/RUN_ID --compact-json
.venv/bin/python -m specdet report --run /path/to/RUN_ID --json
.venv/bin/python -m specdet replay --run /path/to/RUN_ID --reexecute \
  --verus /path/to/verus --run-timeout 180 --out /path/to/new-evidence
```

读取成功的命令退出码是 `0`，但报告保留原分析的语义退出码和用时。磁盘上的 JSON artifact 有带摘要的 envelope，应读取 `payload`。Proof 链接可能是含 `check.json` 的 attempt 目录，witness 链接则指向 replay 证书。重新执行从冻结快照重新开始，不是恢复到中断阶段；如果准备阶段就被打断、尚无完整快照，应从原 profile 重新运行。`report/replay` 接收正常 analyzer run，不接收 profile 中独立 probe 的自定义摘要。

## 交付与边界

Caller goal / proof-map edge、源码 revision 与 dirty-worktree hashes、checker revision，以及 **native / sealed_extract / authored_model** 来源说明，由调用者提供；工具不会自动推断。将精简结果和 artifact 链接附到该记录，不能仅凭某个 helper 确定就宣告系统顶层目标闭合。

原生契约证据不是实现验证；提取版本需要记录依赖替换和 body stub；手写模型需要另行说明与源系统的对应关系，有限 conformance 测试不是 refinement proof。输入可行性、终止性、未提及的 heap 和高层 adequacy，除非另有证据，否则不在结论内。Async 契约、任意 opaque ownership 构造及部分宏仍不支持。

保留原候选、语义/观察变更和源码假设。自由度是否合理、是否遗漏了要求、要不要加强规格，由 agent/项目判断，不由工具或本 skill 代判。原始快照可能包含私有源码，应留在本地，不随精简记录公开。
