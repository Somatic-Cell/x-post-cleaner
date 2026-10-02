"""Synthetic UI fixtures. NO inference and NO external communication."""
from .domain import Assessment, Post, digest
from .store import Store
from .errors import AppError

DEMO_OWNER = "999999999999999999"
FIXTURES = [
    ("今日は何をしても自分を責めてしまう。ここではなく手元の日記に書いておこう。", "DELETE_CANDIDATE", ()),
    ("計算が収束しない原因を調べた。境界条件を修正して再実験する。", "KEEP", ()),
    ("もう無理ｗ……というセリフで笑ってしまった。", "NEEDS_CONTEXT", ()),
    ("今日の夕方の空がきれいだった。", "KEEP", ()),
    ("これだけでは伝わらないかもしれない。", "NEEDS_CONTEXT", ("reply_context_missing",)),
]


def seed_demo(store: Store) -> str:
    if store.get_meta("owner_id") is not None and store.get_meta("demo") != "true":
        raise AppError("refusing_to_seed_demo_into_real_database")
    store.set_meta("demo", "true")
    profile = store.add_profile({"provider": "demo", "version": "synthetic-ui-fixtures-v1"})
    posts = [Post(str(900000000000000001 + i), DEMO_OWNER,
                  f"2025-06-{10+i:02}T14:18:00.000000+00:00", text, flags)
             for i, (text, _, flags) in enumerate(FIXTURES)]
    store.ingest(DEMO_OWNER, posts)
    for post, (_, label, _) in zip(posts, FIXTURES):
        probs = {"DELETE_CANDIDATE": 0.025, "KEEP": 0.025, "NEEDS_CONTEXT": 0.025}
        probs[label] = 0.95
        store.save_assessment(post, profile, Assessment(label, probs, 0.8, "DEMO-NOT-A-MODEL",
                                                        digest(post.text), origin="demo"))
    return profile
