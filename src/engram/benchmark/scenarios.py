"""Twenty multi-session scenarios for the memory benchmark.

Each scenario is a small script: write memories across sessions, then query.
The checks are deliberately substring-based so the benchmark can run without
an LLM judge; a production evaluation should add a judge and human spot checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import MemoryType


@dataclass(frozen=True)
class Turn:
    role: str
    content: str


@dataclass(frozen=True)
class Session:
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class Check:
    query: str
    expected_substrings: tuple[str, ...] = ()
    forbidden_substrings: tuple[str, ...] = ()
    expected_types: tuple[MemoryType, ...] = ()
    min_hits: int = 1
    max_hits: int | None = None
    token_budget: int = 600
    top_k: int = 8
    include_sensitive: bool = False


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    description: str
    user_id: str
    sessions: tuple[Session, ...] = field(default_factory=tuple)
    checks: tuple[Check, ...] = field(default_factory=tuple)


def _user(*messages: str) -> Session:
    return Session(turns=tuple(Turn(role="user", content=message) for message in messages))


SCENARIOS: tuple[Scenario, ...] = (
    # ------------------------------------------------------------------
    # Preference change
    # ------------------------------------------------------------------
    Scenario(
        id="pref_language_change",
        category="preference_change",
        description="User switches explanation language from Chinese to English.",
        user_id="u-pref-language",
        sessions=(
            _user("我偏好中文解释，记住这一点。"),
            _user("我改主意了：现在更偏好英文解释。"),
        ),
        checks=(
            Check(
                query="我的语言偏好是什么",
                expected_substrings=("英文",),
                forbidden_substrings=("中文",),
            ),
        ),
    ),
    Scenario(
        id="pref_location_change",
        category="preference_change",
        description="User switches internship city preference.",
        user_id="u-pref-location",
        sessions=(
            _user("我偏好杭州的实习，记住。"),
            _user("我改主意了：现在更偏好上海的实习。"),
        ),
        checks=(
            Check(
                query="我现在偏好哪个城市",
                expected_substrings=("上海",),
                forbidden_substrings=("杭州",),
            ),
        ),
    ),
    Scenario(
        id="pref_model_change",
        category="preference_change",
        description="User switches preferred recommendation model.",
        user_id="u-pref-model",
        sessions=(
            _user("我偏好 MMoE 模型，记住。"),
            _user("我改主意了：现在更偏好 DIN 模型。"),
        ),
        checks=(
            Check(
                query="我现在偏好哪个模型",
                expected_substrings=("DIN",),
                forbidden_substrings=("MMoE",),
            ),
        ),
    ),
    Scenario(
        id="pref_tool_change",
        category="preference_change",
        description="User switches implementation language preference.",
        user_id="u-pref-tool",
        sessions=(
            _user("我偏好用 Python 做实验，记住。"),
            _user("我改主意了：现在更偏好用 Go 做服务。"),
        ),
        checks=(
            Check(
                query="我现在偏好用什么语言",
                expected_substrings=("Go",),
                forbidden_substrings=("Python",),
            ),
        ),
    ),
    Scenario(
        id="pref_style_change",
        category="preference_change",
        description="User switches explanation verbosity preference.",
        user_id="u-pref-style",
        sessions=(
            _user("我偏好详细解释，记住。"),
            _user("我改主意了：现在更偏好简洁解释。"),
        ),
        checks=(
            Check(
                query="我的解释偏好",
                expected_substrings=("简洁",),
                forbidden_substrings=("详细",),
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Contradiction
    # ------------------------------------------------------------------
    Scenario(
        id="goal_conflict",
        category="contradiction",
        description="User states two different internship goals.",
        user_id="u-goal-conflict",
        sessions=(
            _user("我的目标是 2028 届搜广推实习，记住。"),
            _user("我的目标是 2028 届 Agent 实习，记住。"),
        ),
        checks=(
            Check(
                query="我的目标是什么",
                expected_substrings=("Agent",),
                forbidden_substrings=("搜广推",),
            ),
        ),
    ),
    Scenario(
        id="availability_conflict",
        category="contradiction",
        description="User changes weekly availability.",
        user_id="u-availability",
        sessions=(
            _user("我每周可以实习 4 天，记住。"),
            _user("我每周只能实习 2 天，记住。"),
        ),
        checks=(
            Check(
                query="我每周能实习几天",
                expected_substrings=("2 天",),
                forbidden_substrings=("4 天",),
            ),
        ),
    ),
    Scenario(
        id="salary_conflict",
        category="contradiction",
        description="User changes expected daily salary.",
        user_id="u-salary",
        sessions=(
            _user("我期望薪资是 300 元一天，记住。"),
            _user("我期望薪资是 250 元一天，记住。"),
        ),
        checks=(
            Check(
                query="我的期望薪资",
                expected_substrings=("250",),
                forbidden_substrings=("300",),
                include_sensitive=True,
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Long gap / updates
    # ------------------------------------------------------------------
    Scenario(
        id="project_deadline",
        category="long_gap",
        description="A deadline stated in session 1 is queried after a filler session.",
        user_id="u-deadline",
        sessions=(
            _user("记住：我的 MMoE 项目在 11 月 15 日截止。"),
            _user("最近进展一般。"),
            _user("我的项目截止日期是什么？"),
        ),
        checks=(
            Check(
                query="我的项目截止日期是什么",
                expected_substrings=("11 月 15 日",),
            ),
        ),
    ),
    Scenario(
        id="metric_update",
        category="long_gap",
        description="An experiment metric is updated and the old value must not be used.",
        user_id="u-metric",
        sessions=(
            _user("记住：KuaiRec 实验的 baseline AUC 是 0.72。"),
            _user("记住：最新实验 AUC 更新到 0.78。"),
        ),
        checks=(
            Check(
                query="最新实验的 AUC 是多少",
                expected_substrings=("0.78",),
                forbidden_substrings=("0.72",),
            ),
        ),
    ),
    Scenario(
        id="decision_update",
        category="long_gap",
        description="A technical decision is replaced by a newer one.",
        user_id="u-decision",
        sessions=(
            _user("决定用 FastAPI 做服务，记住。"),
            _user("决定改用 Go 做服务，记住。"),
        ),
        checks=(
            Check(
                query="服务用什么技术栈",
                expected_substrings=("Go",),
                forbidden_substrings=("FastAPI",),
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Task continuity
    # ------------------------------------------------------------------
    Scenario(
        id="experiment_resume",
        category="task_continuity",
        description="A user resumes an unfinished experiment.",
        user_id="u-experiment",
        sessions=(
            _user("记住：MMoE 实验已经跑了 baseline，还差消融实验。"),
            _user("继续上次的实验，下一步做什么？"),
        ),
        checks=(
            Check(
                query="上次实验还差什么",
                expected_substrings=("消融",),
            ),
        ),
    ),
    Scenario(
        id="debug_resume",
        category="task_continuity",
        description="A user resumes a debugging task.",
        user_id="u-debug",
        sessions=(
            _user("记住：Agent 的 tool call 在空检索时会死循环，下一步要加重试上限。"),
            _user("上次的 bug 怎么修？"),
        ),
        checks=(
            Check(
                query="Agent 的 tool call 死循环怎么修",
                expected_substrings=("重试上限",),
            ),
        ),
    ),
    Scenario(
        id="report_resume",
        category="task_continuity",
        description="A user resumes writing a technical report.",
        user_id="u-report",
        sessions=(
            _user("记住：技术报告还差评测章节。"),
            _user("报告还差什么？"),
        ),
        checks=(
            Check(
                query="报告还差什么",
                expected_substrings=("评测",),
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Privacy
    # ------------------------------------------------------------------
    Scenario(
        id="phone_privacy",
        category="privacy",
        description="A phone number is stored but must not be injected by default.",
        user_id="u-phone",
        sessions=(
            _user("记住：我的手机号是 13800138000。"),
        ),
        checks=(
            Check(
                query="我的手机号是多少",
                forbidden_substrings=("13800138000",),
                min_hits=0,
                max_hits=0,
            ),
        ),
    ),
    Scenario(
        id="secret_privacy",
        category="privacy",
        description="An API key must be rejected at write time.",
        user_id="u-secret",
        sessions=(
            _user("记住：我的 OpenAI API key 是 sk-abcdefghijklmnop123456。"),
        ),
        checks=(
            Check(
                query="我的 API key 是什么",
                forbidden_substrings=("sk-",),
                min_hits=0,
                max_hits=0,
            ),
        ),
    ),
    Scenario(
        id="address_privacy",
        category="privacy",
        description="A home address is stored as sensitive and not injected by default.",
        user_id="u-address",
        sessions=(
            _user("记住：我家住址是北京市朝阳区某某路 1 号。"),
        ),
        checks=(
            Check(
                query="我的住址在哪里",
                forbidden_substrings=("某某路",),
                min_hits=0,
                max_hits=0,
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Abstention
    # ------------------------------------------------------------------
    Scenario(
        id="unknown_topic",
        category="abstention",
        description="A query unrelated to stored memories should retrieve nothing.",
        user_id="u-unknown",
        sessions=(
            _user("我偏好中文解释，记住。"),
        ),
        checks=(
            Check(
                query="贝尔不等式是什么",
                min_hits=0,
                max_hits=0,
            ),
        ),
    ),
    Scenario(
        id="no_memory",
        category="abstention",
        description="A new user has no memories at all.",
        user_id="u-empty",
        sessions=(),
        checks=(
            Check(
                query="我的目标是什么",
                min_hits=0,
                max_hits=0,
            ),
        ),
    ),
    Scenario(
        id="future_topic",
        category="abstention",
        description="A query about a topic the user never mentioned should not hallucinate.",
        user_id="u-future",
        sessions=(
            _user("记住：我下周要交 MMoE 报告。"),
        ),
        checks=(
            Check(
                query="我的博士申请进度如何",
                forbidden_substrings=("博士",),
                min_hits=0,
                max_hits=0,
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Hard retrieval (distractor-heavy, added in Stage 2.5)
    # ------------------------------------------------------------------
    Scenario(
        id="hard_project_deadlines",
        category="hard_retrieval",
        description=(
            "Three projects have different deadlines. The query must select Beta "
            "and must not be confused by Alpha/Gamma (different subjects)."
        ),
        user_id="u-hard-deadlines",
        sessions=(
            _user("记住：项目 Alpha 在 11 月 15 日截止。"),
            _user("记住：项目 Beta 在 12 月 1 日截止。"),
            _user("记住：项目 Gamma 在 1 月 10 日截止。"),
        ),
        checks=(
            Check(
                query="项目 Beta 什么时候截止",
                expected_substrings=("12 月 1 日",),
                forbidden_substrings=("11 月 15 日", "1 月 10 日"),
                top_k=1,
            ),
        ),
    ),
    Scenario(
        id="hard_metric_table",
        category="hard_retrieval",
        description=(
            "Three models have different metrics. The query must select DIN/AUC "
            "and not return MMoE parameters or DeepFM latency."
        ),
        user_id="u-hard-metrics",
        sessions=(
            _user("记住：MMoE 的参数量是 3.6M。"),
            _user("记住：DIN 的 AUC 是 0.75。"),
            _user("记住：DeepFM 的延迟是 20ms。"),
        ),
        checks=(
            Check(
                query="DIN 的 AUC 是多少",
                expected_substrings=("0.75",),
                forbidden_substrings=("3.6M", "20ms"),
                top_k=1,
            ),
        ),
    ),
    Scenario(
        id="hard_preference_domains",
        category="hard_retrieval",
        description=(
            "Three preferences in different domains. The query asks about the "
            "language preference and must not return food or editor preferences."
        ),
        user_id="u-hard-preferences",
        sessions=(
            _user("我的语言偏好是中文解释，记住。"),
            _user("我偏好川菜，记住。"),
            _user("我偏好用 VS Code 写代码，记住。"),
        ),
        checks=(
            Check(
                query="我的语言偏好是什么",
                expected_substrings=("中文",),
                forbidden_substrings=("川菜", "VS Code"),
                top_k=1,
            ),
        ),
    ),
)


def scenario_ids() -> list[str]:
    return [scenario.id for scenario in SCENARIOS]
