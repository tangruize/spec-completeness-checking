# spec-determinism-tool

[English](README.md) | **简体中文**

`specdet` 是一个独立的规格确定性分析工具。它检查的是：给定相同输入，契约是否只允许一个**观测等价类**的输出，而不是实现是否正确、规格是否足够强或所有行为是否终止。

Verus 是首个语言后端；核心编排不依赖 Verus AST、Z3 对象或 LLM provider。设计和迁移约束见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

面向 system proof agent 的简明用法见 [evidence guide](docs/agent-evidence.md)、[可复用 skill](.github/skills/specdet-evidence/SKILL.md) 和 [HFS/Nanvix/mimalloc/LRU profiles](examples/real_systems/README.md)。它们说明工具提供什么证据，区分原生证明、源码提取、手写模型和条件性结论，不代替 agent 判断规格是否足够。

## 安装与基本使用

需要 Python 3.11+。Verus 是外部工具，必须显式配置或放进 PATH；不默认查找 Nanvix / VeruSAGE。

安装 Verus extra 还需要 Git 和 C 编译器：Verus grammar 固定到公开仓库的具体 revision，不依赖当前不可用的 PyPI 包发布。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[verus]"

specdet --help
specdet doctor --verus /path/to/verus
specdet discover examples/basic/contracts.rs
specdet analyze --config examples/basic/specdet.toml \
  --verus /path/to/verus --out ./specdet-results
```

也可以使用配置文件：

```bash
specdet analyze --config examples/basic/specdet.toml \
  --verus /path/to/verus \
  --target 'contracts.rs:identity'
```

配置中的相对路径相对于配置文件，不依赖启动目录。`--target` 支持函数名、限定名、文件、`file:function@line` 或显式 glob；同名函数有歧义时必须指定位置。直接分析文件时会列出所有匹配的 exec 声明，无后置条件的声明明确报告 `no_contract`；示例配置用 `visibility = "public"` 排除未注解的 `main`。没有参数时只读取当前目录的 `specdet.toml`，不会悄悄扫描整个 cwd。

`verus.single_file` 用于自包含文件；`verus.native` 使用显式 `build.entrypoint` 保留模块上下文；`verus.cargo` 使用项目的 package / features / injection_file 配置。后两种模式必须具备正确的项目和工具链上下文，不会自动降级为单文件分析。

配置 `adapters.verus.executable` 或 `toolchain_root`；必要时指定 `rust_toolchain`，工具会通过 `rustup` 解析对应的运行库目录。应选择不修改输入的 verifier 启动器：发现 verifier 改写生成源码时，本工具拒绝把结果归结到冻结的分析目标。

## 流程和结果

```text
冻结输入 / 发现目标 / 准备工作副本
  -> extract
  -> observation plan
  -> 生成冻结的 det fn
  -> proof generation -> proof checking
  -> baseline query / schema search
  -> evidence-based report
```

证明生成与证明检查是独立步骤。先保留 baseline，再使用机械策略、已提供的候选或显式开启的 LLM 辅助；失败诊断可以反馈到 proof generation。生成了 proof 文本、仅证明某个 narrowing 切片，或忽略了新增 helper 的证明，都不算全局确定性证明。

| Verdict | 含义 |
|---|---|
| `deterministic` | 原问题为 UNSAT，或同一冻结目标得到有效的完整证明。 |
| `nondeterministic` | 原问题 / 合法 refinement 有 SAT 证据，或存在经原规格确认的具体反例；不自动等于规格 bug。 |
| `inconclusive` | 未决定，包括 solver unknown、辅助未成功或翻译忠实性未确认。 |
| `not_evaluated` | 没有足够的有效分析证据，例如缺少契约或不支持的输入。 |

执行状态和 verdict 分开保存。原始 baseline 不被后续 proof 覆盖；未确认的搜索约束与 SAT-confirmed 切片分开记录。项目 allowlist、`permitted` 标注和 LLM 自评不能改变 solver 结论。

## 工具原生输出与用时

默认的人类可读输出逐项显示 verdict、原始 baseline、决定性证据或实际阻塞原因、观测排除项、未检查的范围、实际预算消耗和用时；重复的恢复性解析警告不会挤掉不同的关键诊断。stdout 最后一行汇报整次分析的墙钟时间。

`--compact-json` 提供版本化、限制展开量的证据摘要；`--json` 继续输出完整报告：

```bash
specdet analyze --config specdet.toml --verus /path/to/verus --compact-json
specdet report --run /path/to/evidence/RUN_ID --compact-json
specdet report --run /path/to/evidence/RUN_ID --json
```

`analyze`、`discover`、`report`、`replay` 和 `assist` 都支持 `--compact-json`，它与 `--json` 互斥。输出格式为 `specdet.summary.v1`，保留源码/问题身份、执行状态、语义结论、原始 baseline、证据与较小的见证值、覆盖边界、预算及 artifact 链接，不内嵌 verifier 日志或生成源码。长预览和额外诊断会明确标记截断或省略；总长度仍随所选目标数增加。完整报告和产物不截断，也没有增加 agent adapter 或模型调用。

`duration_ms` 是包含准备阶段、但不包含最后报告序列化和 CLI 展示的分析墙钟时间；每个目标另有用时。`resources` 统计该目标所有阶段和 revision 的累计消耗，包括 abstract 分析前的 concrete prerequisite。verifier/solver 的单次超时**不是整次运行的总时限**。可处理的失败和中断会显示本次调用用时，compact 模式还返回结构化状态与退出码（中断为 `130`）。读取旧报告时不会虚构缺失的时间或预算。`report` 展示原分析的用时和语义退出码；读取命令自身成功仍返回 `0`。字段和解读见 [evidence guide](docs/agent-evidence.md)。

## 真实欠约束函数与具体反例

0.3 增加有界的构造器搜索和源规格反例证书。它从冻结类型生成具体输入及两组输出，
并在独立工作副本中让真实 Verus 同时验证：

```text
P(input) && Q(input, output1) && Q(input, output2) && !E_out(output1, output2)
```

重放函数没有候选前提，也不调用目标实现来代替契约；向量、数组、tuple、struct
和 ghost 序列由显式值构造，泛型实例必须满足原有 bounds。相同输出或违反原
postcondition 的候选会被拒绝。`--counterexample-candidates 0` 可关闭这条路径；
默认最多尝试 16 个候选，耗尽不代表确定性。

原始完整 Verus SMT 可以仍然返回 `unknown`，即使具体见证已被确认。其背景装箱
公理涉及无限域，有限模型搜索可能无法给出 SAT；给定候选也不保证它变成 SAT。
因此结果明确分开：

```text
baseline.status = unknown
counterexample.kind = source_verified_constructive
counterexample.status = verified
verdict = nondeterministic
```

这不是伪造原始 SMT 的 SAT。反例 JSON 中保留具体绑定、泛型实例、原问题 ID、
证书摘要和可重放的 Rust harness。CLI 会显示实际反例和证书路径。

构造器的类型文本必须是单一合法类型，不能夹带语句或假设。当前泛型重放对
`self::` / `super::` 源码相对路径采取明确拒绝，避免移动到实例化模块后改变
原契约或 bounds 的含义；未支持的情况保持未决，不形成错误证书。

严格端到端用例来自三个原始 VeruSAGE 函数：

| 来源 | 欠约束 | 搜索得到并重放确认的见证形态 |
|---|---|---|
| `vest::init_vec_u8` | 只约束向量长度 | `n=1`，输出 `[0]` 与 `[1]` |
| `memory-allocator::CommitMask::next_run` | 不约束首段 / 最大段，允许空区间 | 同一 mask 和 `idx=0`，输出 `(0,0)` 与 `(1,0)` |
| `atmosphere::Array::new` | `wf()` 只约束 ghost sequence 长度 | `Array<bool,1>` 的 View 分别为 `[false]` 与 `[true]` |

测试不传入预设输出对；必须经完整 API 自动检测到 `nondeterministic`，生成非空
源级证书，并匹配原契约。只得到 UNKNOWN 而没有反例证书不能通过这些测试。
来源、原文件 SHA256 和唯一兼容性注解记录在
`tests/fixtures/incomplete/manifest.json`；没有修改原仓库或强化/弱化契约。

分析命令的退出码：`0` 为全部确定，`1` 为存在确认的非确定性，`2` 为配置 / 执行 / 支持范围问题，`3` 为尚有未决结果；执行错误优先于语义结论。读取报告本身成功时返回 `0`。

## 可选的阶段级 LLM 兜底

默认不调用模型，也不读取隐式的旧 LLM cache。

```bash
specdet analyze --config specdet.toml \
  --llm-fallback live --provider copilot --model MODEL

specdet analyze --config specdet.toml \
  --llm-fallback replay --responses /path/to/recorded/generation
```

`all_applicable` 覆盖目标发现、项目准备、抽取、观测、lowering、proof generation、搜索和解释等已注册路由，不限于两个阶段，也不是每个阶段都无条件请求模型。每个候选都要经过所属后端的检查和采用规则。不能确认源码忠实性或需要语义批准的候选会保留为未采用产物，不因预算充足而自动获得信任。

抽象证明请求额外携带冻结的输入 View 关系、各输入的成对 / 共享绑定、View 的源码定义以及原输出关系。候选不能通过将 View 相等替换成具体输入相等来缩小问题；选择不同的 `abstract_inputs` 也属于需批准的语义变化。Concrete 和 abstract 的 proof 候选分别绑定各自的 problem ID，不能交叉复用为已证明结果。

```toml
[assistance]
mode = "off" # off | live | replay
stage_policy = "all_applicable"
excluded_stages = []
max_rounds_per_stage = 2
max_requests_per_target = 3
max_requests_per_run = 30
request_timeout_seconds = 300
```

Replay 只接受请求 digest 精确匹配的响应；缺失时报告 `replay_miss`，不会转为在线调用。`--offline` 禁止在线 provider 和 Cargo 下载，与 live 配置冲突时直接拒绝。

可以对已有运行的快照重新执行指定辅助任务：

```bash
specdet assist --run /path/to/run --task proof_generation \
  --mode replay --responses /path/to/recordings
```

Provider 只生成文本候选，没有输入项目的修改权限。诊断和解释只作为 annotation；实际编译、证明检查、SMT 解码、真值和统计仍由机械实现负责。

Copilot transport 使用独立的配置 / 工作目录，并关闭工具、MCP、hooks 和额外指令来源；认证使用环境令牌，不复制现有交互式登录配置。缺少必要隔离选项的 CLI 会被拒绝，而不是退回 unrestricted agent。也可以显式配置 `provider = "subprocess"`、参数数组 `command` 和 `text_only = true`，连接一个只从 stdin 读 prompt、向 stdout 返回候选 JSON 的客户端。

`specdet adopt --proposal ... --validation ... --out ...` 可以读取记录下来的候选与检查结果，保存显式采用记录；它不直接改源码，也不能把 `needs_approval` / `unverified` 变成通过。后续执行仍需按当前输入重新检查候选。

## 产物与重放

每次运行使用独立目录，保存配置、源码快照清单、目标列表、逐阶段产物、原始 verifier 日志、proof attempts、搜索 trace、候选采用记录和报告。JSON artifact 带 schema version、输入引用和 payload digest；写入使用原子替换。

```bash
specdet report --run /path/to/run --json
specdet replay --run /path/to/run
specdet replay --run /path/to/run --reexecute --verus /path/to/verus
```

默认 replay 读取既有报告，不运行工具。`--reexecute` 校验快照后创建新的运行，不改旧 evidence；它不是把部分旧文件当作成功检查点继续拼接。工具版本、预算或求解器行为改变时，新执行的结果可能不同。

## Abstract / input-view determinism

```bash
specdet analyze --config examples/abstract/specdet.toml --verus /path/to/verus
# 或覆盖普通配置：
specdet analyze --config specdet.toml --kind abstract_determinism --abstract-input self
```

默认先检查同一具体输入下的确定性；只有该结果已被证明，才进入成对输入的抽象检查：

```text
V(x1, x2) && P(x1) && P(x2) && Q(x1, y1) && Q(x2, y2)
    ==> E_out(y1, y2)
```

`V` 是选定输入的 source-backed View 等价关系。未选中的参数保持共享；每次运行都有自己的前态和后态，输出仍使用同一个 `E_out`。默认选择可解析的非恒等 View，也可配置参数子集：

```toml
[analysis]
kind = "abstract_determinism"

[analysis.abstract]
require_concrete = true
inputs = ["self"]
```

最终报告的 `concrete_result` 保留第一阶段完整证据，顶层报告对应 abstract 问题。Concrete 为 UNKNOWN 或非确定时，abstract 明确跳过；没有可抽象输入时是 `not_applicable`，不是抽象确定性证明。两个问题分别有 proof-attempt 上限，LLM 请求与搜索预算在同一目标内共享。

`--no-concrete-prerequisite` 可直接检查抽象公式，但此时失败可能仅仅来自普通规格欠约束，不能据此宣称发现了“隐藏表示依赖”。本实现没有证明 domain preservation，即 `V(x1,x2) ==> (P(x1) <==> P(x2))`；报告明确保留 `domain_preservation = "not_checked"`，不将两者混淆。

## Python API 与代码结构

```python
from pathlib import Path
from specdet.api import analyze
from specdet.config import load_config

summary, exit_code = analyze(load_config(Path("specdet.toml")))
```

高层 API 可以显式传入 `LanguageBackend` 和 session driver；机械 API 不会仅因为环境变量或配置字符串而偷偷构造 provider。

| 位置 | 职责 |
|---|---|
| `domain/`、`ports/` | 版本化数据契约、证据、候选和语言 / 辅助接口 |
| `analysis/`、`api.py` | 阶段状态机、proof loop、预算边界、批次编排和结论归并 |
| `adapters/verus/` | Verus 前端、观测、lowering、执行、候选检查与 query 接口 |
| `adapters/verus/native/` | 从旧实现迁移的机械 Verus 逻辑，作为后端私有实现 |
| `assistance/`、`providers/` | 外层候选控制器、显式调用、精确重放和采用记录 |
| `storage/`、`cli/` | 快照与产物；配置和命令装配 |

支持 concrete-input 和 abstract/input-view determinism；不实现 Dafny。其他语言通过 backend 接口扩展，不要求模拟 Verus 语法或 SMT 日志。当前不能自动解决所有宏展开、heap / reference 语义或源码翻译忠实性问题，遇到未覆盖情形应给出 gap / unsupported，而不是伪造完整性结论。历史 SpecGym、vstd 和各项目的实验 runner 没有作为新工具的隐式依赖搬进来；本轮源代码可追溯的 VeruSAGE 回归样例见 [`examples/verusage/`](examples/verusage/README.md)。

## 开发

```bash
python -m unittest discover -s tests
# 可选真实 Verus 集成，使用不改写输入的 verifier：
SPECDET_VERUS=/path/to/verus python -m unittest discover -s tests
```

测试包括无 LLM 核心、阶段恢复、候选 / 证据边界、配置和快照隔离，以及迁移后的机械模块。真实 verifier 用例未配置 `SPECDET_VERUS` 时跳过，不会下载 Verus 或调用在线模型。

## 0.2 架构 review

在逐项目回归预期全部匹配后进行 review，修正了三个会影响问题定义的边界错误：

| 问题 | 修正 |
|---|---|
| 由 `PResult` / `SResult` 名称猜测返回结构，可能漏比较成功载荷字段 | 移除名称特例，只按源码解析后的类型结构生成等价关系。 |
| `self_` 选择器别名覆盖真实同名参数，可能配对错误的输入 | 精确源码参数名优先；只有不存在同名参数时才兼容 receiver 别名。 |
| 将 raw pointer 的“视图”错误地当作指向值解引用 | 使用 vstd 的 pointer-data View，不把裸指针当作可读取内存。 |

每项都有回归用例，包含实际 Verus 的错误目标拒绝 / 正确目标验证。修正后的完整
测试集为 378 项通过，涵盖九个 VeruSAGE 快照的 54 个预期检查；这些检查只有
32 项是确定性证明，覆盖限制见回归报告。在线 LLM 生成能力未计入该结果。

## 旧实现与备份

旧目录 `/home/chentianyu/intent_formalization/spec-determinism/` 保留原样。新包不通过 `sys.path`、软链接或导入旧包运行。

本地迁移快照保存在 `backups/legacy-source-20260911T044355Z.tar.gz`，包含当前机械包、脚本、配置、相关文档与选定 vstd 入口；同时保存了工作区状态、源码 digest 和原 HEAD。大型历史实验结果仍保留在旧目录，没有盲目复制。`backups/` 不参与安装或发布。
