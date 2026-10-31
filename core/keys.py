"""Pure builders for every PromptGate partition key and sort key."""


def user_pk(owner: str) -> str:
    return f"U#{owner}"


def config_sk(cfg_id: str, n: int) -> str:
    return f"CFG#{cfg_id}#V#{n:04d}"


def suite_sk(suite_id: str, n: int) -> str:
    return f"SUITE#{suite_id}#V#{n:04d}"


def case_pk(owner: str, suite_id: str, n: int) -> str:
    return f"{user_pk(owner)}#SUITE#{suite_id}#V#{n:04d}"


def case_sk(index: int) -> str:
    return f"CASE#{index:04d}"


def run_sk(run_id: str) -> str:
    return f"RUN#{run_id}"


def result_pk(owner: str, run_id: str) -> str:
    return f"{user_pk(owner)}#RUN#{run_id}"


def result_sk(case_id: str) -> str:
    return f"RES#{case_id}"


def baseline_sk(cfg_id: str, suite_id: str, n: int) -> str:
    return f"BASE#{cfg_id}#{suite_id}#V#{n:04d}"


def fresh_sk(cfg_id: str, cfg_v: int, suite_id: str, suite_v: int) -> str:
    return f"FRESH#{cfg_id}#V#{cfg_v:04d}#{suite_id}#V#{suite_v:04d}"
