# 开发导读

## 本地验证

```bash
python -m pip install --editable .
python -m compileall -q memorydreaming examples tests
python -m unittest discover -s tests -q
git diff --check
```

原生测试会通过PATH运行Node.js和真实Python JSONL bridge；本地没有Node的部分测试会跳过。CI配置使用Node22，但远程工作流只有推送后才能获得实际结果。语义/tokenizer依赖是可选项，不为运行基础回归自动下载模型。

## 推荐阅读顺序

| 顺序 | 文件/目录 | 目的 |
|---|---|---|
| 1 | [models.py](../memorydreaming/models.py)、[来源模型](../memorydreaming/evidence.py) | 理解作用域、四类记忆、认识论状态、时间与支持组 |
| 2 | [schema.sql](../memorydreaming/storage/schema.sql)、领域schema、[database.py](../memorydreaming/storage/database.py) | 理解主数据、控制状态、事务和迁移 |
| 3 | [runtime.py](../memorydreaming/runtime.py)、[services/](../memorydreaming/services) | 理解资源所有者和活动/核心/变更/整理/维护边界 |
| 4 | [host.py](../memorydreaming/host.py)、[references.py](../memorydreaming/references.py) | 理解可信绑定、prepare/delivery、受控引用与当前可见性 |
| 5 | [retrieval/](../memorydreaming/retrieval) | 理解查询计划、五通道候选、融合、来源复核、衰减与预算 |
| 6 | [worker.py](../memorydreaming/worker.py)、[consolidation.py](../memorydreaming/consolidation.py) | 理解租约、事务外计算、退避与原子回执 |
| 7 | [sleep.py](../memorydreaming/sleep.py)、[life.py](../memorydreaming/life.py)、各角色领域服务 | 理解睡眠编排、角色连续性和审批边界 |
| 8 | [原生lib/](../integrations/deepseek-harness/lib)、[bridge.py](../memorydreaming/bridge.py) | 理解Cordis生命周期、窗口开关、请求预算和进程监管 |

`examples/host_lifecycle.py`是跨窗口最小示例；`examples/core_proposal.py`演示重启后的审批；`examples/initialize_role.py`演示由本地操作者明确初始化空角色。它们不把模型生成内容当作授权。

## 四类记忆的正文

| 类别 | 最小正文 |
|---|---|
| working | goal / open_loops / active_entities / checkpoint_note |
| episodic | what_happened / participants / context / outcome |
| semantic | subject / predicate / obj / scope_note |
| procedural | trigger / preconditions / steps / exceptions |

核心身份/关系是受控投影；自传、承诺、偏好与成长是有状态的角色投影；梦是独立虚构派生物。不要增加“第五类事实记忆”来容纳这些功能，也不要将热度作为真实性。

## 扩展契约

新增表必须覆盖模式版本、迁移、完整/范围快照、原子导入、遗忘传播与范围清除。慢网络/编码计算退出写事务，提交时重新验证代次/来源/租约。模型提议与可信审批保持分离，窗口仍默认关闭。

修复优先写能复现行为的最小回归；纯文档、注释和机械移动不需要镜像实现的测试。运行改动相关测试和必要全量回归，记录真实模型/GUI未覆盖的部分。完整职责与入口见[系统架构](system-architecture.md)。

## 版本与构建

引擎版本在`memorydreaming/__init__.py`和`pyproject.toml`；原生版本在`integrations/deepseek-harness/package.json`；数据库版本在`storage/database.py`。纯文档/示例改动不必提高引擎或schema版本。

提交拟发布修改后执行`python scripts/release.py --output dist/release`，生成wheel、tgz、离线ZIP和SHA256清单。`dist/`不提交Git，发布附件与源码分开；详见[安装发布](releasing.md)和[GitHub发布准备](github-publication.md)。
