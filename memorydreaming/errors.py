"""项目自定义异常。

为什么要自定义异常？
调用方需要区分「业务上的预期冲突」和「代码 bug」：
- 版本冲突（两个窗口同时改同一条活动）应当让宿主重新读取后再试，而不是崩掉；
- 找不到身份核心属于恢复失败，宿主应显示提示，而不是用临时身份覆盖原角色。

所以这里给每种可预期的失败一个明确的类型。
"""

from __future__ import annotations


class MemoryDreamingError(Exception):
    """所有本项目异常的基类，方便调用方一次性捕获。"""


class ScopeNotFound(MemoryDreamingError):
    """按 user_id + character_id 找不到对应的数据。

    注意：找不到「身份核心」并不一定是错误——新角色本来就没有。
    bootstrap 会把这种情况作为 recovery_warning 返回，而不是抛异常。
    """


class VersionConflict(MemoryDreamingError):
    """乐观并发冲突：传入的 expected_version 与库里当前版本不一致。

    架构文档第 6 节要求「并发窗口用核心版本号和活动检查点版本做条件更新；
    冲突时重新读取」。捕获这个异常就应当重新 bootstrap 再决策。
    """

    def __init__(self, expected: int, actual: int, what: str = "资源") -> None:
        self.expected = expected
        self.actual = actual
        self.what = what
        super().__init__(f"{what}版本冲突：期望 {expected}，实际 {actual}")


class InvalidQuery(MemoryDreamingError):
    """检索请求本身不合法，例如缺少 user_id。"""


class InvalidCorrection(MemoryDreamingError):
    """纠错请求不合法，例如 kind 既不是 fix 也不是 change。"""


class StorageError(MemoryDreamingError):
    """存储层无法完成操作，例如数据库文件不可写、结构版本不兼容。"""


class JobLeaseLost(MemoryDreamingError):
    """任务租约已过期或被另一 worker 接管，当前执行者必须放弃提交。"""


class MemorySuppressed(MemoryDreamingError):
    """候选或核心字段触及持久遗忘标记。"""


class SourceRevisionChanged(MemoryDreamingError):
    """计算期间发生遗忘，旧结果必须丢弃后重新计算。"""
