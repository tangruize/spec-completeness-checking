# 架构设计：独立的规格确定性分析工具

日期：2026-09-11  
状态：架构目标与迁移约束；已开始实现，实际命令、交付范围和限制以 `README.md` 为准。  
工作目录：`/home/chentianyu/spec-determinism-tool/`  
暂定发布名：`spec-determinism-tool`；Python 包和 CLI：`specdet`。

## 1. 设计结论

采用一个**可安装的模块化单体工具**，而不是重写成服务或通用 agent 框架：

```text
source + project configuration + observation policy + accepted overrides
                              |
                    LLM-free analysis pipeline
                              |
                 stage gaps / evidence / final report
                              |
              optional stage-level assistance controller
                              |
                  untrusted candidate proposals
                              |
              mechanical validation + explicit adoption
                              |
               resume affected stages / create new attempt
```

辅助流程可以在中间阶段遇到缺口时介入，不要求先跑完整条分析链。**所有适合由 LLM 提供候选的阶段都设计兜底，不限于 `extract/search`。** 范围包括目标发现、项目准备、契约 / 类型抽取、观测关系、义务生成、证明生成、搜索，以及诊断解释；每个阶段的允许范围与不能替代的机械职责见第 8.4 节。

Completeness checking 显式包含 `生成 det fn -> proof generation -> proof checking`，不是只有生成 det fn 后直接看 solver 状态。证明生成可以使用机械规则或已提供的 proof，也可以在辅助模式下请求 LLM；关闭模型后仍保留同样的阶段接口。

关键决定：

| 目标 | 架构决定 |
|---|---|
| 确定性部分与 LLM 解耦 | 分离 `analysis`、`assistance` 和 `providers`。机械阶段返回结构化 gap / evidence，由外层控制器按显式策略决定是否调用 LLM 兜底。 |
| 流程模块化 | 每个阶段有显式输入、输出、诊断和可持久化产物，不通过修改共享对象或全局变量交接。 |
| Completeness checking | 独立建模 proof generation 与 proof checking；把 verifier 诊断反馈到生成阶段，保留每次尝试和原始 baseline。 |
| 独立工具 | 包可以从任意目录运行；输入项目、构建环境、输出目录显式配置，不依赖旧仓库。 |
| 保留语言扩展性 | Verus parser、类型系统、View、语法改写、harness 和 SMT 日志协议都属于 Verus 后端。 |
| 保留现有实现 | 旧目录原样保留；新目录独立演进，不能通过 `sys.path`、软链接或导入旧脚本获得运行能力。 |
| 保留已有优势 | 继续复用类型驱动的结构化 narrowing，以及固定 harness 编译一次、后续在 solver 中查询的设计。 |

这里有两种不同的“确定性”：

- **工程上的无 LLM 流程**：不调用模型；输入、规则和控制流显式可追踪。
- **被分析的规格确定性**：给定相同输入，规格是否只允许一个观测等价类的输出。

两者不能混淆。无 LLM 也不等于跨机器逐位可复现：验证器版本、求解器参数、超时、资源限制和查询顺序仍会影响结果，尤其是 `unknown`。

## 2. 基于现有实现的判断

### 2.1 阅读基线

本设计以旧目录中**当前存在的工作区源码**为主，不假定它等于 Git HEAD。记录的 HEAD 为：

```text
d7e7542439dcca46a9e2214bdf0152d92a809e33
```

工作区包含已有修改和未跟踪实现。根目录的 `README.md`、`ARCHITECTURE.md`、`pyproject.toml` 等在工作区中已删除；其 HEAD 内容只作为历史参考读取，没有恢复。特别是旧架构文档关于“只有一个入口”、harness 形态和 witness 的部分描述，不能直接当作当前实现契约。

### 2.2 不是从零开始，也不能只搬文件

| 现有部分 | 已有价值 | 需要切开的边界 |
|---|---|---|
| `extract/extractor.py`、`attrs.py`、`type_registry.py` | tree-sitter-verus 抽取、类型依赖、泛型、属性和源码定位 | 全部属于 Verus 前端，不能直接命名为语言无关 IR。 |
| `codegen/gen_det.py`、`equal_policy.py` | 双运行规格义务、返回值和 `&mut` 后态比较、View-aware equality | 观测策略、规格替换、证明义务和 Rust 文本输出应分开。 |
| `extract/narrow.py`、`predicates.py` | `AssumeNode`、递归策略、结构化谓词、`SearchContext` | 策略仍构造 `@`、`->Ok_0` 等 Verus 表达式；必须把语义路径和语法渲染分开。 |
| `schema_search/schemas.py`、`search.py` | guarded schema、增量查询、避免逐轮启动 Verus | schema 语义、Verus 模板、SMT transcript 解码和 Z3 session 混在一起。 |
| `view/registry.py` | L1 内置规则、L2 alias、L3 源码 View、L4 缓存结果 | registry 不直接请求模型，但会按短类型名读取 LLM 缓存；输入来源仍然隐式。 |
| `llm_type/`、`llm_proof/`、`view/llm.py`、`policy_llm.py` | 候选生成、源码证据、解析、缓存和局部验证 | “调用模型”和“处理模型产物”混合；不同路径对候选的信任程度不同。 |
| `corpus/run_all.py`、`verus/single_file.py` | 工作区和单文件的完整执行链 | `single_file` 实际是大型 orchestration，不只是 verifier wrapper。 |
| `scripts/source_native_*`、specgen feedback、`vstd-survey/` | 保留原项目上下文、生成规格闭环、库规格实验 | 重复编排、项目修补、固定路径和实验统计不应变成核心算法。 |

已有 `SearchContext` 和结构化谓词是很好的切入点，不需要推翻。但当前 `DetCheckSpec` 同时含 Verus 源码、`verus_config`、symbol、策略和 LLM projection，不能直接成为新工具的公共协议。

现有实现**已经有 proof generation 的实质工作**：`llm_proof/prover.py::run_llm_proof_loop` 构造 prompt、生成 proof block / helper lemmas，再重跑 Verus；`verus/single_file.py:1606-1658` 在 baseline 为 `unknown` 且启用 LLM 时调用它。此前架构草案只将它写成辅助分支，没有在主阶段表中明确区分“生成证明”和“检查证明”；本设计将二者提升为 S6 / S7。

### 2.3 要覆盖的实际使用形态

不再按 “Nanvix runner / VeruSAGE runner” 划分核心，而按输入与执行方式划分：

| 使用形态 | 新架构归属 |
|---|---|
| 单个自包含 Verus 文件 | `verus.single_file` 项目执行适配器 |
| Cargo 项目 / workspace | `verus.cargo` 项目执行适配器，加项目 build profile |
| 在真实源码及依赖上下文中分析目标 | `verus.native` 项目执行适配器，加显式 preparation plan |
| VeruSAGE、SpecGym、vstd 目标批次 | target manifest / dataset importer / experiment client |
| `assume_specification`、trait contract 等无函数体规格 | Verus 前端的 contract declaration 形态，不要求伪装成普通 exec body 才能进入公共流程 |
| View-quotient / abstract determinism | 另一类 analysis problem，复用执行链，不是另写一套 corpus runner |
| 规格生成与 determinism feedback | 可选客户端工作流，调用稳定的分析 API |

新工具不能把“文件里搜到 `ensures`”视为完整的目标发现接口。是否存在可分析契约、是否只是 parser recovery、是否支持该声明形态，都需要单独报告。

## 3. 先固定分析语义，再设计模块

### 3.1 工具证明的是什么

令：

- `x` 为完整输入及前态；
- `y` 为返回值及需要建模的可变状态后态；
- `P(x)` 为前置条件；
- `Q(x, y)` 为后置关系；
- `E_out(y1, y2)` 为**已选定并记录的**输出观测关系；
- `Gamma` 为语言、依赖、定义可见性及已声明可信假设构成的逻辑上下文。

规格确定性对应：

```text
Gamma |- P(x) && Q(x, y1) && Q(x, y2) ==> E_out(y1, y2)
```

反例查询为：

```text
C = Gamma && P(x) && Q(x, y1) && Q(x, y2) && !E_out(y1, y2)
```

当前 `_build_template` 的实现是规格关系的双实例化，不是实际执行函数两遍。重构继续保留这个语义。

因此工具不能宣称：

- 确定性成立就等于规格完全正确；
- 任意形式的非唯一性都属于 bug；
- trusted `assume_specification` 已经被证明符合真实 Rust 实现；
- 没有显式建模的 heap、全局状态、并发行为或 ownership effect 已经被覆盖。

`E_out` 是问题定义的一部分，而不是求解失败后可以悄悄调松的参数。

### 3.2 三类状态必须分离

| 维度 | 示例 | 含义 |
|---|---|---|
| 执行状态 | `completed`、`failed`、`unsupported`、`skipped` | 这条分析执行到了哪里。 |
| 原始证据 | `sat`、`unsat`、`unknown`、`not_run`；或 verifier 的目标证明结果 | 哪个具体 query / obligation 得到了什么结果。 |
| 分析结论 | `deterministic`、`nondeterministic`、`inconclusive`、`not_evaluated` | 在所记录问题定义下，证据允许得出什么结论。 |

另设 `annotations`，存放人工审查、疑似 intentional nondeterminism、permissive-OR 等信息；它们不是 solver verdict。

具体规则：

1. 原始 `C` 为 `unsat`，或同一义务获得有效的完整证明，才能得出相对当前观测关系的确定性结论。
2. `C` 为 `sat`，才能由该证据报告规格非确定性；这仍不自动等于非确定性是缺陷。
3. `unknown` 不升级为 `sat`；超时、验证失败和 `assumes != []` 也不等于反例成立。
4. LLM proof 成功后追加新 attempt，不覆盖原始 `r0`。证明辅助来源放在 provenance 中，不另造一种逻辑真值。
5. 同一问题出现互相矛盾的确认结果时，报告 `inconsistent_evidence` 并阻止正常结论，不按“哪个更好看”选结果。

0.3 增加另一种明确区分的正向证据：`source_verified_constructive`。它通过无候选
前提的源级重放函数，证明具体输入及两组输出满足原 `P`、两份完整 `Q` 和
`!E_out`，可以确认该规格的非确定性。它不是 SMT 原查询的 `sat`，不能覆盖
baseline 的 `unknown`，也不能把失败 / 耗尽的候选搜索当成确定性证明。

旧 `classify_ok` 的 `permitted + unknown -> incomplete` 和缺少 `r0` 时的历史推断，只保留在 legacy report importer 中，明确标记为历史标签；不能进入新结果语义。

### 3.3 Narrowing 不等于自动获得 witness

对额外约束 `A` 查询 `C && A`：

| 查询结果 | 允许的动作 |
|---|---|
| `sat` | 这组约束是已确认的反例切片，可以继续细化。 |
| `unsat` | 排除这组约束；不能推出整个 `C` 为 `unsat`。 |
| `unknown` | 保存为未确认候选或尝试其他策略；不能将其标成确认 witness。 |
| 无法翻译 | 报告该 predicate / dimension 不受支持；不是 proof pass。 |

搜索同时保存 `last_confirmed_constraints` 和 `candidate_constraints`。如果 baseline 为 `sat`、后续 refinement 为 `unknown`，保留 baseline 非确定性证据，但不谎称新的细化条件已被确认。反过来，baseline 为 `unknown`，后来同一问题的 `C && A` 为 `sat`，可以追加一条有效的非确定性证据。

继续不依赖 `solver.model()` 生成可读 witness；输出结构化约束及原生语言渲染。没有完整赋值时，称为“反例约束 / 切片”，不称为已经重建的可执行输入输出。

### 3.4 防止 vacuous success

额外记录：

- `pre_feasibility`：`Gamma && P(x)` 是否可满足；
- `contract_feasibility`：`Gamma && P(x) && Q(x, y)` 是否可满足；
- `observation_coverage`：比较了哪些输出，忽略了哪些维度，关系是否显然恒真；
- `model_coverage`：目标涉及的状态中哪些已建模、哪些不受支持。

后端不支持或未决定 feasibility 时明确记录 `unsupported` / `unknown` / `not_run`，不能填成成功。确定性 theorem 可以仍成立，但报告必须标明其可能 vacuous，不能转述为规格质量良好。

这些存在性查询不证明 `forall x. P(x) ==> exists y. Q(x,y)`，也不证明实现正确；totality 不属于首版新增目标。

## 4. 分层与依赖方向

```text
CLI / Python API / experiment clients
        |                         |
        v                         v
   analysis pipeline <---- assistance workflow ----> providers
        |                         |
        +------ domain + ports ---+
                      ^
                      |
          adapters + artifact storage
```

依赖约束：

| 层 | 负责 | 不能做 |
|---|---|---|
| `domain` | 问题、关系、谓词、候选、证据、诊断的数据模型及纯规则 | 导入 Verus parser、Z3、Copilot，读取文件，启动进程。 |
| `ports` | 语言、项目、验证器、查询会话和存储的接口 | 定义 `rust_expr`、`cargo_dir` 之类通用字段。 |
| `analysis` | 阶段编排、completeness proof loop、机械证明策略、候选搜索、证据归并 | 导入 `assistance` / `providers`，或用项目名分支。 |
| `adapters` | 语言语义、原生语法、toolchain、solver 和构建方式的具体实现 | 隐式请求模型，或按实验 allowlist 改分析结论。 |
| `assistance` | 阶段级兜底控制、构造生成任务、解析响应、验证、采用、恢复受影响阶段 | 把模型的自评当证明，绕过 acceptance policy，或把任意异常都交给模型。 |
| `providers` | 一次模型请求的传输及调用元数据 | 修改输入项目、选择等价关系、解释 SMT 结论。 |
| `storage` | 产物、缓存、事件和检查点 | 通过缓存命中隐式批准候选。 |
| `cli` | 配置、依赖装配、命令和展示 | 实现另一套 analysis pipeline。 |

进一步区分两种机械模块：

- **纯转换**：解析已有文本、选择既定规则、生成义务、构造 prompt、解析候选、分类证据。
- **受控副作用**：读取 snapshot、创建工作目录、运行 Verus / Z3、写 artifact。

两者都可以不依赖 LLM，但只有前者适合要求“同一输入必得同一输出”。Provider 调用是独立的第三种副作用。

## 5. 建议目录结构

下面是按职责展示的目标布局。当前实现将部分小型契约合并在 `domain/models.py`、`domain/proposals.py`，并将可复用的机械 Verus 逻辑保留在后端私有的 `adapters/verus/native/`；文件粒度可以逐步调整，依赖方向不能改变。

```text
spec-determinism-tool/
  pyproject.toml
  README.md
  ARCHITECTURE.md
  src/specdet/
    domain/
      targets.py
      contracts.py
      observations.py
      predicates.py
      proofs.py
      proposals.py
      results.py
    ports/
      language.py
      project.py
      verifier.py
      query.py
      artifacts.py
    analysis/
      pipeline.py
      stages.py
      completeness.py
      proof_generation.py
      search.py
      verdicts.py
    assistance/
      workflow.py
      fallback.py
      requests.py
      responses.py
      adoption.py
    providers/
      copilot.py
      replay.py
    adapters/
      verus/
        frontend/
        observations/
        lowering/
        proofs/
        execution/
        smt/
        validation/
      z3/
        session.py
    storage/
      artifacts.py
      cache.py
    cli/
      main.py
      config.py
  profiles/
    examples/
    legacy/
  tests/
    unit/
    contracts/
    integration/
    fixtures/
```

首版使用静态注册表装配内置后端和策略，不先做动态插件市场、通用 DAG DSL 或多进程 RPC。将来确有外部分发需求时再加 entry points；核心接口不依赖注册机制。

## 6. 公共数据契约与 IR 的范围

### 6.1 不建立“所有形式化语言的统一 AST”

采用**薄的语言无关问题模型 + 后端原生语义模型**：

- 公共层知道 target、input/output slot、前后态角色、观测关系、gap、query 和 evidence。
- Verus 的表达式 AST、`TypeInfo`、泛型、`Self`、`Ghost` / `Tracked`、`old` / `final` 等只存在于 Verus 后端。
- `ContractHandle` 引用版本化的原生模型 artifact；公共层不解释其中的源语言文本。
- 不能把旧 `requires: list[str]` 放进公共层，再让公共层用正则替换变量。

这保留当前 parser 的价值，也避免要求未来 Dafny 后端先模拟一遍 Rust 类型系统。

### 6.2 核心契约

| 契约 | 主要内容 |
|---|---|
| `SourceSnapshot` | 实际输入文件清单与 digest、项目根标识、源码版本信息、构建相关文件；不能只记录 Git SHA。 |
| `TargetRef` | 语言、相对文件、声明种类、限定 symbol、signature / impl identity、源码 span、实例化信息和 source digest。 |
| `PreparedProject` | 独立工作区引用、build profile、依赖 / cfg 上下文、transformation manifest、source map。 |
| `ContractHandle` | target、原生模型引用、输入输出 slot、可信假设、模型覆盖信息、抽取来源及其忠实性证据。 |
| `Gap` | 项目上下文、语法、类型、观测、义务生成、证明或搜索等缺口，及位置、影响、可请求的候选种类；是否触发 LLM 由外层策略决定。 |
| `StageOutcome` | stage identity、输入 digest、产物、gaps / evidence / diagnostics 和恢复位置；机械实现与候选采用路径共享同一阶段契约。 |
| `StageCapability` | 每个阶段的候选种类、触发码、validator / materializer、恢复依赖和语义影响；不适用 LLM 的职责必须给出理由。 |
| `ObservationPlan` | 选中的输入 / 输出关系、可观测维度、忽略项、来源、版本和 digest。 |
| `AnalysisProblem` | contract、problem kind、逻辑上下文、观测关系、accepted override 引用和 `problem_id`。 |
| `SearchPlan` | 有序维度、predicate kind、值域、深度和预算；不包含 Rust guard 名。 |
| `ObligationArtifact` | 冻结的 det fn 目标与 harness skeleton、预期目标列表、源映射、lowering digest 及翻译忠实性证据；证明体作为独立候选装配。 |
| `ProofCandidate` | 所属 problem / obligation digest、原生 proof block、helper lemmas、引用依赖、生成策略与来源；生成成功不等于证明成功。 |
| `ProofGenerationResult` | 候选列表或明确的 `no_candidate` / generation gap、所用策略、反馈引用及预算；不返回逻辑真值。 |
| `ProofCheckEvidence` | 目标及 helper obligations 的验证结果、实际检查范围、proof / harness digest、原始诊断、耗时及工具身份。 |
| `QueryBundle` | 从指定 verifier 产物提取的准确查询、binding map、版本与语义角色。 |
| `CheckEvidence` | query / obligation 引用、结果、范围、工具身份、诊断、assumption 集合和产物 digest。 |
| `AnalysisReport` | 问题定义、各 attempt、原始 baseline、最终结论、候选 / 确认证据、覆盖及辅助来源；区分对模型 / 生成义务的结论与对原规格的结论，解释性文字不修改证据。 |

公共 artifact envelope 至少包含 `schema_version`、`kind`、`producer_version`、`input_digests`、`payload_digest`。不使用无版本的任意 `dict` 作为长期接口。

同名方法不能只靠 `fn_name` 定位。源码行号可以作为选择器和诊断信息，但不能单独承担跨版本 identity；源位置变化后需要重新解析并匹配限定声明。

若 parser 尚未识别目标，gap / proposal 先引用 snapshot、文件、源码范围及用户 selector，不伪造完整 `TargetRef`。候选采用前必须确认目标身份；未消除的歧义仍然阻断正式分析。

### 6.3 谓词与语法解耦

公共搜索操作类似：

```text
Equal(observable_id, literal)
Range(observable_id, lower, upper)
Variant(observable_id, variant_id)
LengthRange(observable_id, lower, upper)
Contains(observable_id, element)
Distinct(observation_relation_id, left_output, right_output)
```

`observable_id` 指向有类型的语义维度，例如“第二次运行返回值的成功分支的第一个字段”，而不是字符串 `r2->Ok_0.field`。

Verus 后端负责：

- 从原生类型导出 scalar / sequence / set / map / product / variant 等可搜索形状；
- 生成对应的 Verus 路径、父分支条件和合法访问前提；
- 将 predicate 绑定到 schema 的 guard / 参数；
- 将同一个结构化 predicate 渲染为 witness 文本。

公共层不要求所有类型都可结构化展开。Opaque、别名歧义、递归深度或不支持的 reference effect 应产生明确 coverage gap。

Predicate 是单一事实来源，但不再让 predicate 自己实现 `to_rust()`。新增 kind 必须具备序列化、能力声明、后端 lowering 和报告渲染的一致支持；不能靠 kind 名字符串巧合关联。

## 7. 模块化主流程与 completeness checking

下面列出机械阶段及其数据契约。每个阶段是否允许候选生成兜底由 `StageCapability` 声明；LLM 调用仍在外层，不嵌入这些机械实现。

### 7.1 阶段与输入输出

| 阶段 | 输入 | 输出 | 副作用 / 停止条件 |
|---|---|---|---|
| S0 配置与环境解析 | 配置、CLI | 有效配置、toolchain identity、profile gaps | 机械确认工具与参数；缺少受支持的项目 profile 可请求配置候选，工具缺失 / 权限错误正常报错。 |
| S1 输入固化与目标发现 | 项目、selector | snapshot、target manifest、discovery gaps | 读取输入；未识别声明可请求定位候选，不凭模型自述补全覆盖率或选择歧义目标。 |
| S2 准备分析工作区 | snapshot、build profile | prepared project、transformations、preparation gaps | 可消费受检查的 imports / normalization / profile 候选；只写独立工作区。 |
| S3 契约与类型抽取 | prepared project、target | contract、gaps、coverage | Parser / resolver 工作；未支持语法返回 gap，外层可请求抽取或 normalization 候选。 |
| S4 解析观测关系 | contract、policy、accepted overrides | observation plan、observation gaps | 可请求 View / projection / equality 候选；不按可证明程度偷偷修改关系。 |
| S5 构造分析问题与 det fn | contract、observation plan | problem、search plan、obligation、lowering gaps | 生成冻结目标和 skeleton；未覆盖语法可请求 lowering 候选，但必须保留输入 / 后态和公式语义。 |
| S6 Proof generation | 冻结 obligation、语义上下文、已有 proof、失败反馈、策略与预算 | `ProofGenerationResult`、`ProofCandidate` | 机械策略或已采用候选提供证明体 / helper lemmas；没有候选时可请求 LLM，不在此决定证明成功。 |
| S7 Proof checking / 编译 | obligation、proof candidate、toolchain、limits | verifier run、`ProofCheckEvidence`、诊断 | Verus 等实际检查 det fn 与新增 helper；生成源码编译错误和证明未闭合分开，反馈回对应生成阶段。 |
| S8 原始查询与 feasibility | verifier run、obligation、query bundle | baseline、feasibility、diagnostics | 机械解码并运行 solver；不支持查询导出的后端明确跳过，不由模型补写结果。 |
| S9 可选搜索 | problem、query session、search plan | 确认切片、候选、trace、gaps | 未覆盖维度或策略停滞可请求搜索候选；预算耗尽保留已有证据。 |
| S10 归并与报告 | 全部 evidence、annotations | report、JSON / 人类可读输出 | verdict / 统计只按证据机械归并；可附加有来源的 LLM 解释，不修改结果。 |

默认 CLI `analyze` 调用无 LLM application API；开启辅助模式时，CLI 装配 `assistance` 控制器，仍复用同一组机械阶段 API。实验脚本也可以直接消费这些接口，不需要通过解析终端表格获取结果。`analysis` 不反向导入 `assistance` 或 provider。

### 7.2 Completeness checking：生成证明与检查证明是两个环节

这里的 completeness checking 指第 3.1 节的规格唯一性问题，不扩大为“规格完全正确”。`det fn` 是待证明的目标；生成它的签名和后置条件，不等于已经生成了能让它通过验证的 proof。

```text
S5: freeze det fn signature / requires / ensures / equality / context
                               |
S6: proof_generation(obligation, context, prior_diagnostics)
                               |
             ProofCandidate: proof block + helper lemmas
                               |
              candidate boundary / trust checks
                               |
S7: verify_det_fn(frozen obligation, candidate)
       | proved              | unproved / proof error
       v                     v
  proof evidence      diagnostics -> S6, within budget
       |
       +----> S10 report

unresolved / no candidate -> S8 raw query -> S9 search, when supported
generation / lowering error -> corresponding upstream stage, not "incomplete"
```

**生成器的输入**包括完整 det fn 目标、原规格与类型 / View 上下文、可引用的已知定义和引理、已有尝试以及 verifier 诊断。**输出**为 proof block、必要的 helper lemmas 及其元数据；可以包含 case split、assert、合法 reveal 或引理调用，但不能修改目标或加入新的 unchecked 假设。

证明策略按同一接口组合：

| 策略 | 工作 | LLM 依赖 |
|---|---|---|
| `baseline` | 装配不含额外生成提示的默认证明体，尝试 verifier 自动完成目标 | 无 |
| `rules` | 使用后端明确支持的证明模板、展开 / 引理选择等机械策略 | 无 |
| `accepted` | 使用用户显式提供或已采用的 proof artifact，并针对当前目标重新检查 | 本次无调用，保留上游来源 |
| `assisted` | 机械策略未闭合目标时，由控制器生成或修改 proof / helper 候选 | 可选 live / replay |

默认先做 baseline attempt，并在后端支持时取得 S8 的原始 R0；baseline 未决后进入其余 proof generation 策略，再交 S7 检查，不要求先耗尽 narrowing。baseline 已证明或已有同一问题的确认 SAT 时可跳过不必要的 proof generation 尝试，记录原因。没有可用策略时返回 `no_candidate`，不是成功生成了证明。

`generated`、`accepted_for_check`、`verified` 三种状态必须分开。候选通过语法 / 修改边界检查，只意味着可以交给 verifier；只有 det fn 及新增 helper 的必要义务都得到有效证明，才能追加确定性证据。证明失败、生成失败和预算耗尽均不意味着规格不完整。

完整性证明不允许借用搜索切片的额外 assumptions：S7 检查的是未加 narrowing 条件的原问题。Verus 可以使用独立的无 search guards 证明 harness，或证明所有 guards 均关闭的同一目标；只在某个已固定切片下成立的 proof 不能提升为全局 completeness。

Proof helper 必须有可检查的证明体，其依赖和目标进入 attempt manifest；单纯将新公理或 external body 加入上下文不算 proof generation 成功。通过的 proof 追加 `ProofCheckEvidence`，不覆盖原始 `r0`；baseline 与新 proof 的问题身份不一致时，先处理目标漂移，不能合并结论。

### 7.3 保留 compile-once 的优化

对固定的 `problem + search plan + harness revision`：

```text
Verus harness
    -> one compilation / SMT capture
    -> Verus transcript decoder
    -> normalized query bundle
    -> persistent Z3 session
    -> many ordered checks with different refinements
```

关键约束：

- transcript 的 `Function-Def`、作用域、全局 axioms 和符号命名属于 Verus decoder，不属于公共搜索或通用 Z3 session。
- decoder 按 obligation identity 选择查询，并检查必需的 guard / 参数绑定。不能按“最大的 SMT2 文件”或“第一个同名函数”猜目标。
- guard 的未激活语义、初始状态和查询顺序明确记录；baseline 不携带额外 refinement。
- 一份 artifact 含多个 proof obligations 时，需要确认所有相关目标和 helper 的结果，不能把单个 VC 成功当整体成功。
- 一次失败的 predicate 翻译是 `unsupported_dimension`，不是 `pass`。

S8 / S9 的反例查询必须对应原始 `C`，绑定到已确认的 baseline obligation / guarded harness。Proof attempt 中某条 `assert` 的失败 VC、包含未证明 helper 的上下文，不能当成该查询。未通过的 proof 日志只作为 S6 的诊断反馈；需要搜索时使用原 baseline query bundle，或从同一冻结问题重建经过角色检查的 bundle。

“一次编译”只承诺同一 harness 的搜索阶段；换 policy、补充类型、修改 proof 或生成额外 feasibility 义务可能需要新的编译。LLM 提出的约束若在已有 schema 内，复用 session；若需要新的 schema，必须重新 lowering、编译并建立 session，不能向旧 session 塞入不存在的 guard。不能把这项优化表述为整个辅助闭环永远只调用一次 Verus。

### 7.4 Abstract determinism 作为 problem kind

保留已有 Step 2 的方向，但不让它绑定七个实验项目：

```text
C_abstract =
  Gamma
  && E_in(x1, x2)
  && P(x1) && P(x2)
  && Q(x1, y1) && Q(x2, y2)
  && !E_out(y1, y2)
```

共享参数通过 `E_in` / input pairing 明确保持一致。工作流可以沿用“Step 1 已证明且输入有可解析 View 才运行 Step 2”的策略；记录被依赖的 Step 1 evidence 和相同输出观测关系。

Domain preservation 是另一项独立义务，不因 abstract determinism 成立而自动宣布成立。首版先保留接口和现有能力的迁移路径，不扩展新的 View audit 项目。

0.2 实现中，`analysis.kind = "abstract_determinism"` 默认先执行 concrete
prerequisite，成功后再使用同一 source contract 和 output policy 检查成对输入。
`AnalysisReport.concrete_result` 保存前一问题的完整结果；当前 baseline、proof 和
search 证据只属于当前 `analysis_kind` / `problem_id`。控制器的请求预算按原逻辑
target 共享，不因为切换问题或源码重新定位而重置。

Verus 特有职责继续留在后端：`abstract.py` 选择输入 View，
`type_context.py` 从源码解析别名和 trait 的 View 方法，
`native/codegen/pairing.py` 分配成对绑定，`expressions.py` 处理作用域感知替换，
`proof_hints.py` 产生仍需 verifier 检查的证明提示。不把某个项目的 trait 名称
作为内置白名单，也不把自定义 `spec fn view` 错当成额外的 `T: View` 假设。

默认无可抽象输入时明确记录 `not_applicable`；显式跳过 prerequisite 的模式只
检查抽象公式本身，不能直接诊断“隐藏表示依赖”。Domain preservation 仍未实现，
报告保留 `not_checked`，不将其混入已有证明。

## 8. 阶段级 LLM 兜底：流程显式管理，内核不隐式调用

### 源级反例确认（0.3）

`counterexample` 是机械反例搜索阶段。它从冻结类型构造小范围的具体值，显式
实例化泛型，使用原 det fn 的前置条件、后置关系与输出等价关系生成独立 replay
harness。每个候选必须经过真实 verifier 检查；类型错误、约束不满足、相同输出、
超时或未覆盖类型都不能形成正向证据。

`CounterexampleEvidence` 保存 bindings、type arguments、原 problem / obligation
identity、证书 digest 和 artifact 路径；它与 `CheckEvidence`、`ProofCheckEvidence`
分开。输入绑定被修改或证据属于其他问题时不能采用。构造器搜索不执行目标
implementation，也不依赖预置的期望输出对；原源码保持只读。

当前完整 Verus SMT 的无限装箱域会使某些真实欠约束函数始终返回 UNKNOWN。
这里不删除背景公理后把缩减查询的 SAT 冒充原始 SAT，而是保留原状态，并单独
报告源规格确认的具体反例。该证据标准已经由用户明确选择。

**所有可能受益于候选生成的阶段都提供 LLM 兜底设计，不限于 `extract/search` 或最后补 proof。** 解耦限制的是调用位置和信任边界，而不是禁止 LLM 接手机械实现尚未覆盖的工作。实际执行、证据判定等不适合由 LLM 替代的职责，也必须在阶段能力表中明确说明，不能漏掉后就默认为“不支持兜底”。

机械模块只返回阶段结果；`assistance` 控制器在用户开启辅助模式后，按阶段、缺口种类和剩余预算自动选择候选生成任务。模型产物仍通过同一阶段的数据契约和对应的采用规则进入后续流程，不能因为来自 fallback 就跳过检查。

新增阶段必须声明 `StageCapability`：可生成什么候选、何时触发、如何检查与采用、从哪里恢复，以及哪些职责禁止模型代替。首版使用内置、版本化的注册表，不需要为了覆盖各阶段而引入通用 agent / DAG 框架。

### 8.1 按行为划分，不按文件夹名字划分

| 行为 | 所属 | 是否调用 LLM |
|---|---|---|
| 检测未支持语法 / 未解析类型 / 缺失投影 | Verus frontend / resolver | 否 |
| 检测搜索停滞 / 未覆盖维度 / 未决查询 | analysis search / query backend | 否 |
| 根据阶段结果和预算选择兜底任务 | assistance controller | 否 |
| 用既定规则生成 proof / helper 候选 | analysis proof generation / Verus proofs | 否 |
| 构造 prompt / schema | assistance 与语言任务 codec | 否 |
| 读取已固定的响应 / 候选 | storage / replay provider | 否 |
| 请求模型 | live provider | 是 |
| 解析、规范化、源码证据检查 | deterministic validator / codec | 否 |
| 类型检查、重新生成 harness、验证 proof | Verus validator / verifier | 否 |
| 采用候选 | acceptance policy | 否 |
| critic 给出意见 | 可选 provider task | 是，意见仍不是机械证明 |

因此不把 `llm_type/` 整包简单搬到“非确定性目录”。其中的 gap detection、parse、validator 和 apply 都应成为可独立运行的机械组件。

### 8.2 候选协议

`ProposalEnvelope` 的逻辑结构：

```text
schema_version
proposal_id
kind: project_profile | target_candidates | preparation | extraction | normalization
      | type_model | observation | equality_policy | obligation | proof
      | search_plan | diagnostic | explanation | contract
language
source_ref                 # snapshot + 文件 + 源码范围；尚未识别目标时也必须具备
target_ref                 # 已确认目标时填写，否则保留 selector 和歧义诊断
stage_id
base_input_digests
base_source_digest
base_problem_id             # 在候选针对已建立的问题时必填
request_digest
payload_artifact            # 语言相关、有版本的候选内容
source_evidence[]
origin                     # manual / imported / llm
generation_metadata        # provider、model、prompt version、调用预算
```

验证和采用不原地写进候选本体，单独产生 `ValidationRecord` 和 `AdoptionRecord`，都引用 `proposal_id`。这样可以保留同一候选在不同版本 validator 下的历史记录，不会因更新状态破坏候选 digest。

`extraction` 是对既有契约的忠实抽取候选；`contract` 是新增或修改规格的候选，两者不能混用。`normalization` 提交带 source map 的语法转换方案；`search_plan` 可以提交有序约束、候选反例或 schema 扩展，但不能提交最终逻辑真值。

`obligation` 是把已确定的分析问题翻译成原生 det fn / harness 的候选，必须与 `proof` 区分：前者需要确认目标翻译忠实，后者只填写冻结目标下的证明体。`diagnostic` / `explanation` 仅为有来源的解释或路由建议，不是 `CheckEvidence`。

协议流：

```text
Gap / Evidence
    -> GenerationRequest
    -> RawResponse
    -> ProposalEnvelope
    -> ValidationRecord
    -> AdoptionRecord
    -> AcceptedOverrides / SearchPlan / ProofCandidate / Annotation
    -> StageOutcome / resumed mechanical stages
```

Provider 只返回响应及调用元数据。它不能拿到用户输入项目的写权限，也不能把模型自述的 “verified” 当成 verifier 结果。

### 8.3 不同候选有不同的 acceptance rule

| 候选 | 机械检查 | 采用规则 |
|---|---|---|
| 项目 profile / preparation | 配置 schema、已存在的路径与工具、允许的构建参数、依赖上下文、变更范围和 transformation manifest | 只能使用明确支持的构建能力；涉及 cfg / 语义上下文变化时显式采用，不自动安装工具或执行任意生成脚本。 |
| 目标发现 | snapshot / 源码范围、限定声明、selector 匹配、重复与歧义检查 | 可补充待分析目标，但缺少独立依据时不能宣称发现完整；无法确认身份的目标继续阻断。 |
| 契约抽取 / 语法 normalization | 源码范围与内容、目标身份、条款对应、变量绑定、前后态、类型及已支持的语义保持检查 | 只有具备足够忠实性证据才能作为原规格的抽取结果；JSON 合法或编译通过不够。 |
| 类型补全 | 源码定位与内容匹配、类型解析、限定 identity、codegen / typecheck | 能由源码重新确认的事实可以规则化采用；无法确认的语义需显式标为外部假设或拒绝。 |
| View / projection | 语法、类型、依赖、访问合法性、输出形状和 coverage diff | “编译通过”不等于投影合理；新增信息丢失或语义变化需显式批准。 |
| 等价策略 | 引用存在、类型正确、忽略维度 diff；可选关系性质审计 | 改变 `E_out` 即改变问题，必须显式采用并新建问题，不能作为成功修复覆盖旧结果。 |
| det fn / harness lowering | 原问题到原生公式的对应、输入 / 后态绑定、P / Q 双实例化、E_out 与上下文、语法 / 类型和 source map | 编译或证明通过不能替代翻译忠实性检查；未经确认的目标不能产生原规格结论。 |
| 搜索约束 / 反例候选 / schema 扩展 | 维度引用、类型、值域、父分支前提、后端 lowering 和 schema binding | 可以按策略自动采用为搜索建议，但每组候选仍需对同一问题实际求解；`unknown` 不算确认反例。 |
| Proof block / helper lemmas | 限定修改区域、禁止引入新的 trusted 假设、恢复原义务；S7 检查 det fn 与所有新增 helper | 可自动采用为待验证的 proof candidate；只有 S7 的有效证据能将其标为 verified。 |
| 诊断 / 报告解释 | 引用的源码、诊断和 evidence 是否真实存在；区分事实引用与假说 | 只附加解释或候选路由，不修改 solver status、verdict、统计、执行权限或语义采用决策。 |
| 生成的 contract | 契约抽取、类型检查、实现符合性 / 非空性 / 额外质量约束 | 属于 specgen 客户端；单凭 determinism 不能采用规格。 |

现有源码证据、type parse、codegen smoke、View syntax validator 都值得复用，但必须写清它们**没有证明什么**。尤其不能把第二个 LLM critic 的认可提升为验证结果。

对未知语法的 extraction 尤其要保守：源码片段能对应上、生成的 harness 能编译，并不证明 LLM 没有遗漏 requires、误解绑定或改变 `old` / 后态语义。默认不让忠实性未确认的候选产生对原规格的正式结论；用户显式选择探索模式时，可以分析该候选模型，但报告必须保留 `unverified_extraction`，将模型上的 SAT / UNSAT 与原规格上的 `inconclusive` / `not_evaluated` 分开。人工采用也不能抹掉“尚未机械确认”的事实。

同一原则适用于 LLM lowering：不忠实的 det fn 可能非常容易证明。无法确认原问题与生成义务的对应时保留 `unverified_lowering`，即使 S7 验证了该生成义务，也不能把结论直接归给原规格。

Proof 路径保留现有 agentic 实现中较好的边界：只提取候选 proof block，将其放回原始的 frozen obligation，再由工具重新运行 verifier。禁止候选修改前后条件、观测关系、可信依赖或新引入 `assume` / `admit` / unchecked external body 来换取通过；允许的既有引理和定义展开必须属于记录的上下文。

### 8.4 辅助流程的调用方式

无 LLM 主流程：

```text
analyze(source, config, accepted_overrides) -> report
```

可选的阶段级辅助流程：

```text
analyze_assisted(source, config, accepted_overrides, fallback_policy)
    -> execute mechanical stage
       -> produced artifact: continue to dependent stages
       -> registered gap / unresolved evidence:
          -> check enabled stage, capability and remaining budget
          -> bounded candidate generation
          -> mechanical validation + acceptance policy
          -> materialize accepted stage artifacts
          -> resume or rebuild affected stages
       -> other failure: report diagnostics, no LLM fallback
    -> append attempts, preserve baseline and produce report
```

控制器既能在首次运行中自动兜底，也能从已保存的 stage outcome / report 恢复，不要求用户每一步手动调用 `assist`。以下逐阶段覆盖，不是一个捕获所有异常的 `try/except -> LLM`：

| 阶段 / 触发条件 | LLM 候选兜底 | 恢复位置与不可替代的职责 |
|---|---|---|
| S0 项目配置无法映射到已有 profile | 从 manifest / 目录上下文提出 `project_profile` | 重新进行机械配置与环境解析；不能伪造工具存在、授权或安装依赖。 |
| S1 未识别声明、宏或目标布局 | 定位声明、限定名称、目标列表或 normalization 候选 | 重新确认 target identity / coverage；不能凭模型猜测解除用户 selector 的歧义。 |
| S2 模块、imports、include 或项目兼容性缺口 | preparation / normalization / 受支持的构建 profile 候选 | 在独立工作副本重新准备并记录 source map；不弱化契约、丢依赖或修改原项目。 |
| S3 `unsupported_syntax` / 类型或抽取缺口 | 结构化 extraction、类型补全或 normalization | 消费经确认的契约或重新抽取，失效 S4-S10 的相关产物；忠实性未确认时只允许显式探索。 |
| S4 无法得到合适的观测模型 | View、projection、equality policy 候选 | 重新解析观测关系；需要语义批准的变化不因自动兜底而跳过。 |
| S5 `lowering_gap` / 生成 harness 的语法或类型问题 | 公式替换、绑定、原生 det fn / harness 候选 | 确认翻译忠实性后从 S5 重建，再走 S6-S8；不能生成更容易证明但不同的目标。 |
| S6 无有效 proof 候选，或 S7 证明未闭合 | proof block、helper lemmas、case split、引理调用或修订候选 | 回到 S6，然后实际执行 S7；proof generation 本身不宣布 complete。 |
| S7-S8 verifier / solver 未决或复杂诊断 | 有来源的 diagnostic，或路由到 S2 / S5 / S6 / S9 的候选建议 | 编译、证明检查、SMT 解码、SAT / UNSAT 判断仍由机械实现负责；诊断建议不能补写缺失证据。 |
| S9 搜索停滞、缺维度或无法具体化反例 | 约束、搜索顺序、反例候选、projection / schema 扩展 | 已有 schema 内恢复 S9；新 schema 从 S5 重建，重新取 baseline；所有候选仍需求解。 |
| S10 难以解释 witness / 未决原因 | 引用已有证据的源码级解释、疑似缺约束位置或人工审查建议 | 只附加 annotation；新提出的赋值回 S9 当候选，不能混入已确认 witness 或更改 verdict / 统计。 |
| 跨阶段的 cache、持久化、恢复、清理 | 不由 LLM 兜底执行 | 原子提交、digest 校验、权限和资源所有权必须由机械流程保证。 |

`all_applicable` 的含义是启用当前后端已注册且具备相应检查边界的所有适用路由，不是对每个阶段都无条件调用一次模型。配置解析、目标列表、项目准备等无 target 的早期任务计入阶段与整次运行预算，不能绕过限制。

搜索场景中“现有策略找不到有效候选”不等于任何逻辑结论。LLM 可以承担候选生成和策略选择，但不能替代 verifier / solver；新的 projection 如果改变了观测关系，必须走 observation / policy 采用路径，不能伪装成普通搜索优化。

工具缺失、权限错误、内部异常、用户取消、无法消除的目标歧义，不自动触发模型修复。工具链可处理而生成 harness 出错时，可以根据实际诊断路由到 S2 / S5 / S6；不能把真实项目本身有错也默认为允许模型改源码。Solver 超时作为 `unknown` 时，可以在总预算仍有余量的情况下尝试候选或 proof；不能因局部超时而无限追加预算。后端没有对应 validator / materializer 能力时，候选仍为 unsupported，不让模型生成任意 Python 或 shell 代码去绕过适配器。

预算包括每阶段轮数、每目标和整次运行的请求数、单次调用时限及原有 verifier / search 限制；传输重试计入请求预算。对同一输入和同一候选重复失败要停止或换策略；候选被拒绝、预算耗尽、replay miss 都保留明确原因和已有 evidence，不写成成功。

LLM 项目配置、抽取、normalization、类型补全、观测、policy 或 lowering 若改变模型 / 上下文，重新建立 `problem_id`，按输入 digest 失效下游产物。已确认语义不变的语法改写、搜索顺序调整或 guarded schema 扩展保留 `problem_id`，记录新的执行配置 / harness；重编译后重新取得 baseline，不能继承旧 session 的约束。单纯 proof candidate 在目标及上下文不变时也保留 `problem_id`，增加新的 attempt。

### 8.5 运行模式与缓存语义

| 模式 | 行为 |
|---|---|
| 默认 `analyze` / fallback `off` | 不加载 live provider，不读取隐式 LLM cache；可消费用户明确指定的 accepted overrides。 |
| `analyze --llm-fallback live` | 外层控制器默认覆盖 `all_applicable` 路由，自动调用显式选定的 provider，按预算处理候选并恢复流程；用户可显式排除阶段。 |
| `analyze --llm-fallback replay` | 同样的自动兜底控制流，但只使用固定响应，不发出模型请求。 |
| `assist --mode live` | 对已有 run 的指定阶段 / 任务单独发起辅助，仍使用同一候选协议、预算和采用规则。 |
| `assist --mode replay` | 对已有 run 的指定任务重放；和自动 replay 一样要求 request digest 精确匹配，缺失即 `replay_miss`，不自动转 live。 |
| `--offline` | 禁止 provider 网络调用和构建依赖下载；仍允许使用已安装的本地 verifier / solver。 |

“本次没有调用 LLM”和“分析从未使用 LLM 生成的输入”分别记录为 runtime calls 与 upstream artifact provenance。

`--offline` 与 live assistance 配置冲突时在启动阶段明确拒绝；不能在离线模式下偷偷降级为一次在线兜底。自动触发不等于自动批准所有候选：涉及观测语义变化或忠实性未确认的产物，仍按第 8.3 节处理。

首版可以保留 Copilot 作为一个可选 provider，但不复制旧脚本默认的 `--allow-all-tools --allow-all-paths` 行为。若未来保留 agentic proof 实验，需要受限工作目录、输入白名单和单独权限策略；文本候选接口仍是主边界。

## 9. 语言、构建和 solver 三个扩展轴

### 9.1 概念接口

以下是接口形状，不是待直接复制的完整实现：

```text
ProjectAdapter.prepare(snapshot, build_profile) -> PreparedProject

LanguageBackend.discover(snapshot, build_context, selector) -> TargetManifest
LanguageBackend.extract(project, target) -> ContractAnalysis
LanguageBackend.resolve_observations(contract, policy, overrides) -> ObservationPlan
LanguageBackend.lower(problem, search_plan) -> ObligationArtifact
LanguageBackend.generate_proof(obligation, context, feedback, strategy) -> ProofGenerationResult
LanguageBackend.decode_queries(verifier_run, obligation) -> QueryBundle
LanguageBackend.validate_proposal(proposal, frozen_context) -> ValidationRecord
LanguageBackend.materialize_proposal(proposal, adoption, frozen_context) -> AcceptedOverrides
LanguageBackend.render_evidence(evidence, problem) -> RenderedEvidence

AnalysisStage.execute(inputs, accepted_overrides) -> StageOutcome
VerifierBackend.check(obligation, proof_candidate, project, limits) -> VerifierRun
QueryBackend.open_session(query_bundle, limits) -> QuerySession
QuerySession.check(query_request) -> CheckEvidence
ArtifactStore.read/write(versioned_artifact) -> ArtifactRef
```

`build_context` 包含项目适配器解析出的模块、依赖、cfg/features 和目标架构信息。发现阶段使用 snapshot 中的原始位置；后续 preparation 若改写文本，通过 source map 重新定位并确认同一个声明，不能继续沿用已经失效的行号。

`generate_proof` 是机械规则入口，不在内部请求模型；已有或 LLM 产生的 `ProofCandidate` 通过显式阶段输入进入 S6。S7 的 verifier run 由后端机械解码为 `ProofCheckEvidence`，生成与验证不能共享一个含糊的 `success` 标记。

`materialize_proposal` 只把获准候选转换为有阶段作用域的 profile / 目标 / 抽取 / normalization / 义务 / proof / 搜索等输入，不修改原源码或直接写 verdict。机械提取和经确认的 LLM 抽取通过同一 contract artifact 交给下游；不要求未知语法必须重新通过原先不支持它的 parser 才能使用，但不能因此绕开忠实性要求。

首版 `LanguageBackend` 可以是组合了几个小服务的 facade，不需要为每个函数引入独立类。接口的重点是职责和可替换边界，不是增加对象数量。

### 9.2 Verus 后端负责到底的内容

- tree-sitter-verus grammar、解析恢复、宏别名、属性契约和不同 declaration form；
- Verus 原生类型、trait / impl / generic context、cfg、alias 和 visibility；
- `View`、extensional equality、`Ghost`、`Tracked`、`PointsTo`、raw pointer 等语义；
- `old` / post-state substitution、reference 和 mutable-return 的支持边界；
- 原生 harness、guarded schema、机械 proof generation 策略和 proof / helper patch 渲染；
- Verus 命令、目标范围、Cargo 转发参数、日志格式与 SMT 名称映射；
- Verus-specific 候选检查与原生 witness 展示。

当前仍不支持的可变引用返回等情形，在迁移中保留为明确的 capability / target diagnostic，不以“统一接口”掩盖成已经支持。

### 9.3 项目适配器不等于语言后端

`nanvix` 不是 language，`verusage` 也不是 verifier。

Project adapter 只负责保留有效的项目上下文：模块、依赖、features、include 文件、构建参数和必要的工作副本。项目特殊路径、target 列表、CSV 格式、额外 imports 和兼容性修补放进 profile / importer。

每个 preparation transformation 记录：

```text
transform_id + version
input/output digest
changed source spans
reason
semantic impact
source map
```

语义影响至少区分文本规范化、已知定义展开、模型变化和未知影响。保留契约文本并不自动证明上下文语义没变；源码 patch 若影响 `Gamma`，也必须进入问题 identity，不能静默把“改写后的程序”当原程序。

### 9.4 未来 Dafny 不需要假装自己是 Verus

语言后端声明能力，例如：

- 支持哪些 contract declaration、state / reference 语义；
- 是否支持 concrete-input / abstract-input determinism；
- 是否能导出可重放查询；
- 是否支持 incremental refinement、SAT 证据或仅 proof checking；
- 哪些 predicate kind 和 proposal kind 可用。

未来 Dafny 可以提供自己的 AST、heap / frame 模型、证明义务、验证器结果和 witness renderer。如果它不能提供 Verus 式 guarded SMT session，选择已有接口的其他实现或返回 `unsupported_capability`，而不是改公共搜索让它理解 Dafny 语法。

不能保证“只换 parser 就能支持 Dafny”。可复用的是问题、编排、预算、证据和产物协议；语义建模与 lowering 必须由新语言实现。

## 10. 独立使用、默认值与 CLI

### 10.1 安装与依赖

- 使用 `src/` 布局和标准 Python packaging，延续当前 Python 3.11 基础，不更换实现语言。
- Verus parser / Z3 依赖放在 `[verus]` extra；provider-specific 依赖或外部 CLI 只在启用对应 provider 时需要。
- 核心模型、report / replay 和配置不应因未安装 tree-sitter-verus 或 Copilot 而无法导入。
- Verus 通过显式 toolchain 配置或 PATH 发现，不默认借用 `~/nanvix/toolchain/verus`。
- 记录 verifier、Z3、parser、stdlib source / compiled metadata、目标架构、cfg/features 和关键参数；不假设所有 Verus 版本都匹配一个全局 Z3 版本。

`doctor` 负责解释缺失和不兼容，不自动安装依赖。vstd 实验中“源码与已编译 metadata 必须匹配”的约束成为 toolchain contract，而不是某个实验 README 的注意事项。

### 10.2 观测策略的兼容性

当前 `EqualPolicy` 默认把 Err 合并，并忽略 raw pointer identity。新工具不把这个行为偷偷改成结构完全相等，也不继续用模糊的 “default”：

| 策略 | 用途 |
|---|---|
| `verus-observable-v1` | 将当前主要观测默认值显式版本化；报告 Err、pointer、View 和 opaque 处理。 |
| `verus-strict-v1` | 在后端支持的范围内使用更严格比较，保留无法比较的诊断。 |
| 自定义 / imported policy | 必须有 digest、来源、忽略项清单和显式选择。 |

迁移的基线使用明确 profile，比较时要求有效关系一致，不能只比较 policy 名称。LLM 不能因 baseline 难证明而自动切换策略。

### 10.3 配置示例

以下为未来的配置形状；现在尚无对应命令实现。示例是自包含文件集合，复杂项目应选择 Cargo / native adapter：

```toml
schema_version = 1

[project]
root = "."

[language]
name = "verus"

[targets]
include = ["src/**/*.rs"]
visibility = "public"

[build]
adapter = "verus.single_file"

[adapters.verus]
toolchain_root = "/opt/verus"

[analysis]
kind = "concrete_determinism"
observation_policy = "verus-observable-v1"

[analysis.proof_generation]
strategies = ["baseline", "rules", "accepted", "assisted"]
max_attempts = 3

[limits]
verifier_timeout_seconds = 120
solver_timeout_ms = 10000
max_search_rounds = 500
max_container_depth = 1
seed = 0

[artifacts]
output_dir = "./specdet-results"

[assistance]
mode = "off" # off | live | replay
stage_policy = "all_applicable"
excluded_stages = []
max_rounds_per_stage = 2
max_requests_per_target = 3
max_requests_per_run = 30
request_timeout_seconds = 300
```

所有相对路径相对于配置文件，而不是调用时的 cwd 或 `configs/` 的父目录。CLI 覆盖配置后生成一份 `effective_config`，其中不存在 `{nanvix}` 专属插值。

`[assistance].mode` 默认为 `off`，`--llm-fallback` 覆盖该字段；live 模式必须显式配置 provider，replay 模式必须指定固定响应来源。`all_applicable` 按第 8.4 节与后端能力展开为具体路由，并写入 `effective_config`；不再以 `extract/search` 两项白名单为默认。排除阶段与预算独立于开关，启用辅助不意味着所有失败都可兜底。

Proof generation 是主流程阶段，不由 assistance 开关决定是否存在。上例关闭 LLM 时仍执行 baseline / rules / 显式 accepted 策略，`assisted` 记录为未启用；开启辅助后才允许调用 provider。Proof attempts、模型请求及 verifier / search 资源分别有上限，不能互相重置来无限重试。

改为 workspace / native 模式只选择相应 build adapter 并配置其参数，不改变 analysis API。单文件执行方式要求文件确实自包含，不能对任意复杂项目悄悄采用 standalone 降级。

### 10.4 命令面

```text
specdet doctor --config specdet.toml
specdet discover --config specdet.toml
specdet analyze --config specdet.toml --target <qualified-selector>
specdet analyze --config specdet.toml --targets targets.json --overrides accepted.json
specdet analyze --config specdet.toml --llm-fallback live --provider copilot
specdet analyze --config specdet.toml --llm-fallback replay --responses <recordings>
specdet assist --run <run-dir> --task lower --provider copilot --mode live
specdet assist --run <run-dir> --task extract --provider copilot --mode live
specdet assist --run <run-dir> --task search --provider copilot --mode live
specdet assist --run <run-dir> --task proof_generation --provider copilot --mode live
specdet assist --run <run-dir> --task proof_generation --mode replay --responses <recordings>
specdet adopt --proposal <proposal.json> --validation <validation.json>
specdet replay --run <run-dir>
specdet report --run <run-dir>
```

这些都是设计示例，`--task` 对应已注册的适用路由，并不只支持示例列出的几项。`adopt` 必须执行相应 acceptance policy，不是一个能跳过验证的通用“信任按钮”。

`replay` 区分只重建报告和重新运行本地 verifier / solver；后者仍可能因资源限制产生不同结果，必须保存为新 attempt。配置或工具错误、已完成但 inconclusive、已确认 nondeterministic 应有文档化且可区分的机器结果；不能只靠一条终端字符串判断。

## 11. 产物、缓存、恢复与输入保护

### 11.1 持久化结构

```text
<output>/<run-id>/
  manifest.json
  effective-config.json
  toolchains.json
  targets.json
  events.jsonl
  summary.json
  targets/<target-id>/
    attempts/<attempt-id>/
      stage-manifest.json
      contract.json
      observation-plan.json
      problem.json
      search-plan.json
      proof-generation.json
      proof-candidate.json
      proof-check.json
      native/
      verification/
      queries/
      search-trace.jsonl
      report.json
  proposals/<proposal-id>/
    proposal.json
    validations/
    adoptions/
  generation/<request-id>/
    request.json
    response.txt
```

原生 `.rs` / `.smt2` 等放在 backend artifact 区域，不成为所有语言强制拥有的文件名。snapshot 可以是内容寻址存储的引用，但必须能恢复本次实际输入，而不只是指向后来可能变化的 source path。

### 11.2 三种 identity

| Identity | 包含 | 不应该混入 |
|---|---|---|
| `problem_id` | contract / model digest、问题种类、逻辑上下文、观测关系、采用的语义 override | 时间戳、临时目录、仅供展示的项目名称 |
| `execution_fingerprint` | problem、schema / lowering 版本、原生产物、toolchain、flags、seed、预算、查询顺序等 | 未声明的环境默认值 |
| `run_id` / `attempt_id` | 一次实际执行的唯一标识和归属 | 冒充语义相等性证明 |

Parser、normalization、view / policy 或 source context 改变时，按实际依赖失效对应阶段；不能只哈希目标函数文本而忽略依赖类型或 stdlib。

### 11.3 缓存不是可信来源

- 解析、观测、harness、查询、proposal 和最终 evidence 分不同 cache namespace。
- 固定快照只读；live 写入不能覆盖 pin。
- 按限定类型 / 目标 identity 及相关 source digest 查找，不保留 `_get_any_for_short` 的宽松采用语义。
- shape-based / 跨目标匹配只产生待重新验证的候选，不能复用为已证明结果。
- 过期响应可以展示，但必须产生 `stale_input`，不能隐式进入 accepted overrides。
- 负缓存、超时和 `unknown` 记录原预算，不当作永久逻辑结论。
- prompt / response 的访问范围显式配置；产物不得保存凭据或整个进程环境。

恢复以 stage manifest 中已经原子提交的完整产物为准。进程中断不写入成功状态；后续 stage 的输入 digest 不匹配时重新执行，不能仅因文件存在就跳过。

### 11.4 保护被分析项目

新工具不沿用 workspace runner 的“改原 proof 文件，最后 restore”模式：

- 源码输入只读，生成物只写独立工作区。
- 使用真实副本或写入隔离的 copy-on-write；不在指向输入文件的可写 hardlink 上修改。
- profile 中的构建脚本属于显式授权的项目执行，不代表工具在 OS 层已完成安全沙箱化。
- 失败时保留必要诊断；清理只作用于该次 run 拥有的具体路径。
- 并行 target 有独立 workdir、Z3 context 和输出空间；共享编译缓存需要明确锁或安全隔离。

这些约束也适用于 source-native 和 agentic 实验，不能因它们叫 “smoke script” 就绕开。

## 12. 迁移映射与实施顺序

### 12.1 旧模块到新职责

本表中 Python 包内的路径以旧 `spec_determinism/` 为基准；scripts / survey 工作流按名称指代。

| 旧实现 | 新位置 / 处理 |
|---|---|
| `extract/{extractor,attrs,type_registry,types}.py` | Verus frontend；只将确实中性的 identity / evidence 等结构提到 domain。 |
| `codegen/equal_policy.py`、`view/{prelude,impl_scanner,registry}.py` | Verus observations；registry 接收已解析、已批准的输入，不自己找 LLM cache。 |
| `codegen/gen_det.py` | Verus contract instantiation、obligation lowering、equality renderer；拆出不同纯转换。 |
| `extract/predicates.py`、`extract/narrow.py` | 中性谓词与搜索树 / 策略 + Verus dimension exporter / renderer。 |
| `schema_search/schemas.py` | 中性 search plan + Verus guarded-schema lowering / binding。 |
| `schema_search/search.py` | analysis search + Verus transcript decoder + Z3 session。 |
| `verus/verify.py`、`verus/workspace.py` | Verus execution / Cargo project adapter；不再原地注入用户文件。 |
| `verus/single_file.py` | source preparation、execution、pipeline stage 和 assistance 拆分，不整文件搬迁。 |
| `llm_type/{gaps,parse,apply,validator}.py` | 机械 gap / proposal codec / validator / immutable override application。 |
| `llm_proof/prover.py`、`agentic.py` 的证明生成与重验循环 | S6 proof generation、S7 proof checking、`analysis/completeness.py` 的有界反馈流程；provider 调用单独留在 assistance。 |
| LLM type/view/policy/proof runners 与 caches | assistance tasks、providers、proposal store；统一 provenance，保留各类 acceptance 差异。 |
| `classify.py` | 中性的 evidence-based verdict；项目 allowlist 和历史 bucket 移至 legacy importer / annotations。 |
| `corpus/*` | target batch API、manifest importer、legacy report reader；不能再拥有独立的分析链。 |
| `source_native_*` | 通用执行能力提入 adapter，项目特殊 patch / build 留在 profile。 |
| specgen feedback、vstd survey、Step 2 sweep | 调用新 API 的 client / problem workflow；实验 reward 和项目统计留在外层。 |

### 12.2 分阶段实施

| 阶段 | 交付 | 退出条件 |
|---|---|---|
| M0 固化迁移基线 | 架构、现有源码清单、真实工作区快照、代表性 fixture / artifact | 保留修改及未跟踪源码，区分历史文档和当前行为；迁移源已归档至新目录 `backups/`，旧工作区不动。 |
| M1 独立无 LLM 最小端到端流程 | 包、配置、extract -> det fn -> proof generation -> proof checking -> R0 -> report | 不依赖旧仓库或 Copilot，baseline / 机械证明策略走独立生成与验证契约。 |
| M2 搜索与产物协议 | 结构化维度、compile-once / replay、typed evidence、阶段缓存 | 复用当前搜索能力；SAT / UNKNOWN 切片、unsupported、恢复语义明确。 |
| M3 通用项目执行 | Cargo / native adapter、target manifest、旧 profile 迁移 | Nanvix / VeruSAGE 不需要核心中的项目分支；输入项目不会被原地改写。 |
| M4 候选与全适用阶段兜底 | manual / replay proposal 先行，覆盖第 8.4 节全部适用路由及 proof feedback，再接 live provider | 不限于 extract / search；逐阶段说明候选、检查与恢复边界，生成产物与最终逻辑证据分离。 |
| M5 外层工作流迁移 | abstract determinism、vstd / std specs、specgen feedback client | 共用同一 analysis API；实验口径和额外能力不会渗入 verifier / search。 |

M1-M5 是实施顺序，不代表所有历史实验入口都已迁移；具体交付范围见 README。Dafny 实现不在这些阶段内。

旧目录是迁移参考，不充当新工具的 runtime dependency。实现前已冻结当前源码 / 文档快照并记录工作区状态，包含必要的未跟踪源码；仅 `git archive HEAD` 会丢掉当前有效实现。大型实验产物仍留在旧目录，不盲目复制全部结果目录。

### 12.3 兼容行为与有意调整

先保存源码、有效观测关系、生成目标、Verus flags 和原始 `r0` 等迁移基线，不把已有误标的 bucket 当必须保留的正确行为。

需要显式记录的调整：

- `unknown` 不再因为 `permitted` / allowlist 变成 solver-confirmed incomplete；
- narrowed `unknown` 不能作为已确认 witness；
- 不再覆盖原始 R0；
- 缓存必须匹配 identity / digest，且命中不代表已批准；
- 不再隐式启用模型或修改输入源码；
- missing contract、trivial equality、unsupported state semantics 有独立诊断。

这些是协议 / 可信度层面的有意改变，不混进“只是移动代码”的提交。Legacy report 可以显示旧标签，但必须同时显示它没有哪些证据。

### 12.4 后续实施的验收场景

先迁移现有 extractor、gen_det、narrow、attrs、type registry、View scanner / resolver 的内嵌 self-tests，再补直接覆盖新边界的用例；不先替换整个测试工具链。

| 场景 | 应满足 |
|---|---|
| 无 Copilot / 无网络环境 | `analyze` 可以使用本地 Verus 运行，不发出 provider 请求。 |
| `all_applicable` 辅助策略 | 目标发现、准备、抽取、观测、lowering、proof generation、搜索和解释等已注册路由均可触发；实际展开与排除项写入有效配置。 |
| 机械 proof generation，无 LLM | 产生 baseline / 规则候选或明确 no-candidate；生成记录和 Verus 检查结果独立保存，不因模型关闭而跳过阶段接口。 |
| det fn 首次证明失败后获得有效 proof | 诊断反馈到 S6，生成 proof / helper 后再次执行 S7；完整通过才追加确定性证据，原始 R0 不变。 |
| Proof 候选有未证明 helper、unchecked 假设或只证明 narrowed slice | 不得报告全局 completeness；模型自述成功或 proof 文本生成成功均不够。 |
| LLM 生成了更弱但可证明的 det fn | 拒绝目标漂移；无法确认 lowering 忠实性时，只能保留生成义务上的结果，不能归给原规格。 |
| 项目准备 / 目标发现缺口 | 可请求 profile、imports 或定位候选；不自动修改原项目、忽略依赖、解除目标歧义或夸大发现覆盖率。 |
| LLM 报告解释与 raw evidence 冲突 | 保留机械 verdict / 统计，拒绝或标注不实解释；不能通过生成报告改变分析结论。 |
| 未支持语法，辅助关闭 / 开启 | 关闭时返回 extraction gap；开启且有预算时自动请求候选，采用后恢复受影响阶段，无需逐步手动 `assist`。 |
| LLM 抽取漏掉 requires、误解 `old`，或无法确认忠实性 | 拒绝错误候选；未确认候选不能产生原规格结论，显式探索模式下只报告候选模型的结果。 |
| 搜索策略停滞但仍有总预算 | 可自动请求约束 / schema / proof 候选，模型输出不替代实际求解证据。 |
| LLM 提出已有 schema 外的约束 | 通过后端检查后重建 harness / session 和 baseline；无法支持时记录拒绝原因，不引用不存在的 guard。 |
| 重复无效候选、预算耗尽、replay miss | 有界停止并保留原始 evidence；replay 不转 live，重试不绕过全局预算。 |
| 工具缺失、权限错误、内部异常 | 返回对应错误，不因辅助开关开启而自动交给模型处理。 |
| 输入来自无关临时项目 | 安装后的工具从非旧仓库 cwd 运行，无 `sys.path` 注入、Nanvix 路径或 VeruSAGE 目录假设。 |
| scalar 的紧 / 松规格 | 同一策略下分别保留确定性证明与 SAT 证据。 |
| baseline unknown，narrowing 也 unknown | 只有候选，无确认 witness。 |
| baseline unknown，某个 refinement sat | 追加该切片证据并得到非确定性结论。 |
| 某个 refinement unsat | 只排除切片，不宣布整个规格确定。 |
| 编译失败、solver 超时、缺少 guard binding | 各自产生明确诊断，不伪装成 SAT / proof pass。 |
| `&mut` 后态、泛型、同名 impl 方法、嵌套 View | 源定位及观测 / 状态语义保持；不支持的形态显式拒绝。 |
| 恒真 equality / 不可满足前提 | 相对证明结果与 vacuity / coverage 警告同时保留。 |
| LLM 改 policy 或目标后声称证明成功 | 不覆盖旧问题；proof acceptance 拒绝目标漂移。 |
| source / dependency / toolchain 改变 | 相关缓存失效，旧 proposal 不自动采用。 |
| 中断恢复、并行 targets | 原项目不变，完整产物原子提交，无共享 Z3 / workdir 污染。 |
| 非 Verus fake backend | 可用中性契约走通编排；用来检验边界，不实现或宣称支持 Dafny。 |

## 13. 首版不做什么

- 不实现 Dafny，也不建立跨语言通用 AST / theorem prover。
- 不重写现有类型抽取和 narrowing 算法，只在迁移时拆开实际耦合。
- 不新增 solver portfolio、通用插件市场、远程服务或分布式任务系统。
- 不把所有实验脚本无差别纳入公共 API。
- 不承诺模型输出、求解时间或不同版本工具的结果逐位一致。
- 不以“更多 complete”为优化目标；优先保证问题定义、证据和覆盖范围准确。
- 不修改或恢复旧工作区；必要的迁移修复、依赖环境和开发产物只写入新工具目录。

## 附录：现状依据

以下路径相对于旧目录 `/home/chentianyu/intent_formalization/spec-determinism/`。它们是设计证据，不是新工具将来的运行依赖。

| 依据 | 对本设计的影响 |
|---|---|
| `spec_determinism/config.py:1-104`；`configs/nanvix.toml` | `nanvix` 必填字段、专属路径插值和默认配置需要移出核心。 |
| `spec_determinism/extract/types.py:253-431` | `FunctionSpec` / `DetCheckSpec` 混合源语言语义、生成源码、Verus 配置和 projection。 |
| `spec_determinism/extract/extractor.py:1236-1386` | 当前前端已有 source-line disambiguation、泛型 / impl 上下文；应保留并规范 target identity。 |
| `spec_determinism/codegen/gen_det.py:528-870` | 当前实际生成双实例规格义务、比较返回值及 mutable post-state，并处理 closed definition 展开。 |
| `spec_determinism/extract/narrow.py:28-130`；`spec_determinism/extract/predicates.py:59-252` | 搜索树和 Protocol 可复用，但语义 predicate 与 Rust 路径 / 渲染仍耦合。 |
| `spec_determinism/schema_search/schemas.py:64-80,491-522,607-706` | `SchemaBinding` 持有 Rust 模板；schema rendering 与 proof block 注入同处一个模块。 |
| `spec_determinism/schema_search/search.py:36-200,244-356` | Verus transcript 解码、Z3 session 和搜索共存，SAT / UNKNOWN 都可进入保留候选的路径。 |
| `spec_determinism/view/registry.py:335-399` | L4 缓存查找按短名宽松读取，说明“没有 live LLM”仍可能有隐式生成输入。 |
| `spec_determinism/verus/verify.py:25-64` | 当前 workspace 注入会写原 proof 文件，再尝试恢复。 |
| `spec_determinism/verus/single_file.py:1398-1708` | 单文件入口包含 type completion、编译、搜索、LLM proof escalation，并会覆盖 `r0_z3`。 |
| `spec_determinism/llm_proof/prover.py:1-24,66-118,452-493`；`spec_determinism/verus/single_file.py:1606-1658` | 现有 proof loop 已生成 proof / helper 并交 Verus 重验；新架构显式拆出 S6 generation 与 S7 checking，而非仅在文档中提及 proof assistance。 |
| `spec_determinism/llm/copilot.py:38-119`；`spec_determinism/llm_type/runner.py:132-172` | Provider 传输可复用，但权限过宽，type runner 还借用了 proof agent 的调用入口。 |
| `spec_determinism/llm_type/validator.py:45-173`；`spec_determinism/llm_proof/agentic.py:451-550` | 源码证据 / codegen smoke 与“原始义务重新验证”的证明强度不同。 |
| `spec_determinism/classify.py`，`classify_ok`、`REAL_SAT_MANUAL_FNS` | 结果语义混入 permissive-OR 和项目人工规则，需与 solver evidence 分离。 |
| `spec_determinism/corpus/run_all.py:31-105`；`scripts/source_native_specgen_det_feedback.py:360-436` | 多种入口重复编排；原地注入和工作副本两种副作用策略并存。 |
| `vstd-survey/run_determinism.py:18-35,65-95,159-219` | 额外 runner 重复主链，已有行号定位和 mutable-reference return 的明确限制。 |
| `vstd-survey/experiments/tangruize-std-specs-2026-09-01/run_std_specs_determinism.py:1-32` | 当前额外脚本将 `assume_specification` 机械转换为普通函数 shim，提示前端契约形态需扩展。 |
| `docs/pipeline-2026-06-02.en.md`；`docs/unknown-handling-strategy-2026-05-15.md`；`docs/determinism-funnel-framework.md` | 保存 compile-once 与非 model-based witness 的动机，同时纠正文档之间不一致的结论口径。 |
| `docs/abstract-determinism-plan-2026-06-04.en.md`；`docs/step2-false-positives-rootcause-2026-06-09.en.md` | Input-view equivalence 与 output equality 是独立问题参数；验证失败有 proof gap、模型缺口和关系错误等不同原因。 |
| `vstd-survey/HANDOFF.md`；`vstd-survey/README.md` | 区分 contract uniqueness 与实现正确性，保留 toolchain / stdlib 匹配和 parser coverage 信息。 |

历史参考：上述 HEAD 中的根 `README.md`、`ARCHITECTURE.md` 和 `pyproject.toml`；本轮只读，没有恢复这些已删除文件。
